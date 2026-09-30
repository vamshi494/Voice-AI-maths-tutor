# backend/tests/test_voice_session.py
import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import pytest

from app.contracts.messages import ErrorNotice, QuestionDraft, VisionQuestion
from app.voice.images import process_image_bytes, reencode_image, register_image_stream_handler
from app.voice.providers import build_stt, build_tts, build_vad
from app.voice.session import build_agent_session, prewarm
from app.agents.adapter import StubLLM, TutorAgent


def _make_dummy_image_bytes(width: int = 1500, height: int = 2000) -> bytes:
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_voice_providers():
    vad = build_vad()
    assert vad is not None

    with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "fake-key"}):
        stt = build_stt()
        assert stt is not None

    with patch.dict("os.environ", {"ELEVEN_API_KEY": "fake-key"}):
        tts = build_tts()
        assert tts is not None


def test_prewarm():
    proc = MagicMock()
    proc.userdata = {}
    prewarm(proc)
    assert "vad" in proc.userdata


def test_reencode_image():
    raw_bytes = _make_dummy_image_bytes(1500, 2000)
    reencoded = reencode_image(raw_bytes)
    assert len(reencoded) > 0

    re_img = Image.open(io.BytesIO(reencoded))
    assert re_img.format == "JPEG"
    assert re_img.width <= 1024
    assert re_img.height <= 1024


@pytest.mark.asyncio
async def test_process_image_bytes_success():
    raw_bytes = _make_dummy_image_bytes(500, 500)
    mock_gateway = AsyncMock()
    mock_gateway.complete_json = AsyncMock(
        return_value=VisionQuestion(
            question_text="Find the roots of x^2 - 5x + 6 = 0",
            legible=True,
        )
    )

    result = await process_image_bytes(raw_bytes, gateway=mock_gateway, generation=2)
    assert isinstance(result, QuestionDraft)
    assert result.generation == 2
    assert "roots of x^2 - 5x + 6" in result.text


@pytest.mark.asyncio
async def test_process_image_bytes_illegible():
    raw_bytes = _make_dummy_image_bytes(500, 500)
    mock_gateway = AsyncMock()
    mock_gateway.complete_json = AsyncMock(
        return_value=VisionQuestion(
            question_text="",
            legible=False,
        )
    )

    result = await process_image_bytes(raw_bytes, gateway=mock_gateway, generation=3)
    assert isinstance(result, ErrorNotice)
    assert result.generation == 3
    assert "couldn't read that clearly" in result.message


@pytest.mark.asyncio
async def test_process_image_bytes_error_handling():
    raw_bytes = b"not-a-valid-image-bytes"
    mock_gateway = AsyncMock()

    result = await process_image_bytes(raw_bytes, gateway=mock_gateway, generation=1)
    assert isinstance(result, ErrorNotice)
    assert "couldn't read that clearly" in result.message


def test_register_image_stream_handler():
    mock_room = MagicMock()
    mock_mgr = MagicMock()
    mock_mgr.current_generation = 1

    register_image_stream_handler(mock_room, mock_mgr)
    mock_room.register_byte_stream_handler.assert_called_once()
    args, kwargs = mock_room.register_byte_stream_handler.call_args
    assert args[0] == "images"
    assert callable(args[1])


@pytest.mark.asyncio
async def test_agent_session_construction():
    mock_ctx = MagicMock()
    mock_ctx.proc.userdata = {}
    mock_mgr = MagicMock()

    session = build_agent_session(mock_ctx, mock_mgr)
    assert session is not None
    assert isinstance(session.llm, StubLLM)
    assert session.options.turn_handling["turn_detection"] == "vad"
    assert session.options.turn_handling["interruption"]["enabled"] is True
    assert session.options.turn_handling["interruption"]["discard_audio_if_uninterruptible"] is False
    assert session.options.turn_handling["preemptive_generation"]["enabled"] is False


def test_set_speed_reaches_elevenlabs_tts(monkeypatch):
    """set_speed reaches the ElevenLabs TTS voice settings, so the speed button changes the audio."""
    from livekit.plugins import elevenlabs
    from app.state_machine.manager import StateMachineManager
    import app.voice.providers as providers
    from app.voice.providers import apply_tts_speed

    # PRIMARY_TTS is a module global set by build_tts(); isolate this test from earlier ones.
    monkeypatch.setattr(providers, "PRIMARY_TTS", None)
    tts = elevenlabs.TTS(api_key="offline-test-key", voice_id="v")
    session = MagicMock()
    session.tts = tts
    mgr = StateMachineManager(session=session)
    assert mgr.set_speed(1.2) is True
    assert mgr.state.tts_speed == 1.2
    assert tts._opts.voice_settings.speed == 1.2
    assert apply_tts_speed(object(), 0.8) is False        # a TTS without speed support


@pytest.mark.asyncio
async def test_failed_ocr_never_shows_provider_errors_to_the_student():
    """A failed OCR must not surface raw provider text in the ErrorNotice, such as the
    OpenRouter 403 "Key limit exceeded ... https://openrouter.ai/workspaces/..." (the vision
    fallback model is on OpenRouter). No system/provider jargon reaches the student."""
    raw_bytes = _make_dummy_image_bytes(500, 500)
    mock_gateway = AsyncMock()
    mock_gateway.complete_json = AsyncMock(return_value=None)
    mock_gateway.last_error = ("OpenRouter error (403): Key limit exceeded (total limit). "
                               "Manage it using https://openrouter.ai/workspaces/default/keys/x")
    result = await process_image_bytes(raw_bytes, gateway=mock_gateway, generation=1)
    assert isinstance(result, ErrorNotice)
    for leak in ("OpenRouter", "403", "http", "Key limit", "error ("):
        assert leak not in result.message
    assert "type" in result.message.lower() or "photo" in result.message.lower()

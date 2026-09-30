# backend/tests/test_audio_degraded.py
"""TTS fallback adapter and caption pacing with FEATURE_CAPTIONS enabled. Runs offline."""
import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.contracts.agent_state import ConvState
from app.prompts.registry import RESUME_BRIDGE_LINE
from app.state_machine.states import ConvEvent
from tests.fakes import settle


class TTSError(Exception):
    pass


def _tts_error() -> SimpleNamespace:
    return SimpleNamespace(error=TTSError("boom"), source=None)


def _caption_rig(monkeypatch):
    from tests.test_lifecycle_scenarios import _rig

    monkeypatch.setattr(settings, "FEATURE_CAPTIONS", True)
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    return _rig(monkeypatch)


def _feed_progress(mgr, completed: list[int]):
    from app.contracts.messages import StepProgress

    st = mgr.state
    for idx in completed:
        step = st.steps_sent[idx]
        mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=idx,
            event="completed", completed_by="caption", started_up_to=idx, heard_up_to=idx,
            drawn_op_ids=[op.op_id for op in step.ops]))


@pytest.mark.asyncio
async def test_tts_error_enters_caption_mode(monkeypatch):
    from tests.test_lifecycle_scenarios import _start_lesson

    r = _caption_rig(monkeypatch)
    await _start_lesson(r)
    r.mgr.on_session_error(_tts_error())
    await settle()

    assert r.mgr.state.audio_mode == "captions"
    status = r.rec.of("audio_status")[-1]
    assert status.mode == "captions" and status.reason == "tts_unavailable"


@pytest.mark.asyncio
async def test_midutterance_tts_failure_not_natural_end(monkeypatch):
    from tests.test_lifecycle_scenarios import _start_lesson

    r = _caption_rig(monkeypatch)
    h = await _start_lesson(r)
    token = r.mgr._speech_seq
    gen, turn = r.mgr.state.generation, r.mgr.state.active_turn_id

    r.mgr.on_session_error(_tts_error())
    r.mgr._on_speech_done(h, gen, turn, token)      # the failed handle finishes: ignored
    await settle()

    assert r.rec.of("turn_ended") == [], "a failed utterance is not a natural end"


@pytest.mark.asyncio
async def test_caption_mode_reaches_agent_speaking(monkeypatch):
    r = _caption_rig(monkeypatch)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="teach me")
    await settle()
    assert r.mgr.state.conv_state == ConvState.GRAPH_RUNNING

    async def hold(_run, _src, timeout):
        await asyncio.sleep(3600)

    monkeypatch.setattr(r.mgr, "_wait_heard", hold)
    r.mgr.on_session_error(_tts_error())
    await settle()

    assert r.mgr.state.audio_mode == "captions"
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING


@pytest.mark.asyncio
async def test_caption_pacer_waits_for_progress(monkeypatch):
    r = _caption_rig(monkeypatch)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="teach me")
    await settle()

    calls: list[int] = []

    async def wait_for_heard(run, src, timeout):
        calls.append(src)
        while run.heard_upto < src:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(r.mgr, "_wait_heard", wait_for_heard)
    r.mgr.on_session_error(_tts_error())
    await settle()
    assert calls == [0], "the pacer waits for step 0 to be heard"

    _feed_progress(r.mgr, [0])
    await settle()
    assert calls == [0, 1], "only after step 0 is heard does it pace step 1"


@pytest.mark.asyncio
async def test_caption_pacer_timeout_advances(monkeypatch):
    r = _caption_rig(monkeypatch)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="teach me")
    await settle()

    calls: list[int] = []

    async def instant(run, src, timeout):
        calls.append(src)          # simulate the per-step timeout elapsing

    monkeypatch.setattr(r.mgr, "_wait_heard", instant)
    r.mgr.on_session_error(_tts_error())
    await settle()

    assert calls == [0, 1, 2, 3]
    assert [e.status for e in r.rec.of("turn_ended")] == ["complete"]


@pytest.mark.asyncio
async def test_doubt_in_caption_mode(monkeypatch):
    from tests.test_lifecycle_scenarios import _start_lesson

    r = _caption_rig(monkeypatch)
    await _start_lesson(r)
    r.mgr.state.audio_mode = "captions"
    handles_before = len(r.session.handles)

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    await settle()

    assert r.mgr.state.conv_state in (ConvState.TASK_CORRECTING, ConvState.AGENT_SPEAKING)
    assert r.mgr.state.doubt_awaiting_resolution
    assert len(r.session.handles) == handles_before, "captions never call session.say"


@pytest.mark.asyncio
async def test_resume_in_caption_mode(monkeypatch):
    from tests.test_lifecycle_scenarios import _start_lesson

    r = _caption_rig(monkeypatch)
    await _start_lesson(r)
    r.mgr.state.audio_mode = "captions"
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    await settle()
    handles_before = len(r.session.handles)

    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()

    assert r.mgr.state.audio_mode == "captions"
    assert len(r.session.handles) == handles_before
    assert any(RESUME_BRIDGE_LINE in n.message for n in r.rec.of("notice"))


@pytest.mark.asyncio
async def test_new_lesson_retries_voice(monkeypatch):
    from tests.test_lifecycle_scenarios import _start_lesson

    r = _caption_rig(monkeypatch)
    await _start_lesson(r)
    r.mgr.state.audio_mode = "captions"
    handles_before = len(r.session.handles)

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="new lesson")
    await settle()

    assert r.mgr.state.audio_mode == "voice"
    assert len(r.session.handles) == handles_before + 1, "a new lesson speaks again"


class _FakeCapabilities:
    streaming = True
    aligned_transcript = False


class _FakeTTS:
    provider = "?"
    num_channels = 1
    sample_rate = 24000
    capabilities = _FakeCapabilities()

    def __init__(self, **kw):
        self.options: dict = {}

    def update_options(self, **kw):
        self.options = kw

    def on(self, *args, **kwargs):
        return None

    def off(self, *args, **kwargs):
        return None


def _install_fake_tts(monkeypatch):
    import livekit.plugins.deepgram as dg
    import livekit.plugins.elevenlabs as el
    import app.voice.providers as providers

    class ElTTS(_FakeTTS):
        provider = "elevenlabs"

    class DgTTS(_FakeTTS):
        provider = "deepgram"

    monkeypatch.setattr(el, "TTS", ElTTS)
    monkeypatch.setattr(dg, "TTS", DgTTS)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "k")
    monkeypatch.setattr(providers, "PRIMARY_TTS", None)
    return providers


def test_fallback_adapter_built_with_both_keys(monkeypatch):
    from livekit.agents.tts import FallbackAdapter

    providers = _install_fake_tts(monkeypatch)
    monkeypatch.setattr(settings, "TTS_PROVIDER", "elevenlabs")

    tts = providers.build_tts()

    assert isinstance(tts, FallbackAdapter)
    assert providers.PRIMARY_TTS.provider == "elevenlabs"


def test_speed_applies_through_adapter(monkeypatch):
    providers = _install_fake_tts(monkeypatch)
    monkeypatch.setattr(settings, "TTS_PROVIDER", "elevenlabs")
    tts = providers.build_tts()

    assert providers.apply_tts_speed(tts, 1.2) is True
    assert providers.PRIMARY_TTS.options["voice_settings"].speed == 1.2

    monkeypatch.setattr(settings, "TTS_PROVIDER", "deepgram")
    monkeypatch.setattr(providers, "PRIMARY_TTS", None)
    tts2 = providers.build_tts()
    assert providers.PRIMARY_TTS.provider == "deepgram"
    assert providers.apply_tts_speed(tts2, 1.2) is False

import os
from typing import Any
from app.config import settings
from app.observability import log_event

# Ensure environment variables are populated for any SDK internal lookups
if settings.ELEVENLABS_API_KEY:
    os.environ["ELEVEN_API_KEY"] = settings.ELEVENLABS_API_KEY
    os.environ["ELEVENLABS_API_KEY"] = settings.ELEVENLABS_API_KEY
if settings.DEEPGRAM_API_KEY:
    os.environ["DEEPGRAM_API_KEY"] = settings.DEEPGRAM_API_KEY


def _key(value: str | None) -> dict[str, str]:
    """Pass api_key ONLY when we have one. An explicit api_key=None counts as "given" to the
    LiveKit plugins, which then skip their environment-variable fallback and raise -- so a
    key supplied via the process environment (Docker / secret manager) yielded stt=None."""
    return {"api_key": value} if value else {}


# ElevenLabs defaults, restated because update_options replaces the whole VoiceSettings.
ELEVEN_STABILITY = 0.5
ELEVEN_SIMILARITY = 0.75


PRIMARY_TTS: Any = None   # the first provider of build_tts(); speed resolves through it


def apply_tts_speed(tts: Any, speed: float) -> bool:
    """Apply the student's speed choice (0.8 / 1.0 / 1.2) to the live TTS.

    Takes effect from the next synthesized utterance. Resolves the adapter's PRIMARY provider:
    ElevenLabs supports speed via VoiceSettings (0.7..1.2); a Deepgram primary has no
    speed option and reports False. Returns whether the speed was applied.
    """
    target = PRIMARY_TTS if PRIMARY_TTS is not None else tts
    if target is None or not hasattr(target, "update_options"):
        log_event("tts_speed_unsupported", tts=type(target).__name__)
        return False
    try:
        from livekit.plugins import elevenlabs
        if isinstance(target, elevenlabs.TTS):
            target.update_options(voice_settings=elevenlabs.VoiceSettings(
                stability=ELEVEN_STABILITY, similarity_boost=ELEVEN_SIMILARITY,
                speed=max(0.7, min(1.2, float(speed)))))
            return True
    except Exception as e:
        log_event("tts_speed_apply_failed", error=str(e))
        return False
    log_event("tts_speed_unsupported", tts=type(target).__name__)
    return False


def build_stt() -> Any:
    """Build the Deepgram STT provider."""
    try:
        from livekit.plugins import deepgram
        return deepgram.STT(
            **_key(settings.DEEPGRAM_API_KEY),
            model=settings.DEEPGRAM_MODEL,
            language=settings.DEEPGRAM_LANGUAGE,
            interim_results=True,
        )
    except Exception as e:
        log_event("stt_build_failed", error=str(e))
        return None


def _ensure_http_context() -> None:
    try:
        from livekit.agents.utils import http_context
        if http_context._ContextVar.get(None) is None:
            http_context._new_session_ctx()
    except Exception:
        pass


def _build_deepgram_tts() -> Any:
    from livekit.plugins import deepgram
    return deepgram.TTS(**_key(settings.DEEPGRAM_API_KEY))


def _build_elevenlabs_tts() -> Any:
    from livekit.plugins import elevenlabs
    return elevenlabs.TTS(
        **_key(settings.ELEVENLABS_API_KEY),
        voice_id=settings.ELEVENLABS_VOICE_ID,
        model=settings.ELEVENLABS_MODEL,
        enable_ssml_parsing=True,
    )


def build_tts() -> Any:
    """Build TTS: the configured primary plus the other provider as fallback when keyed.

    Sets the module global PRIMARY_TTS to the primary instance (speed resolves through it).
    """
    global PRIMARY_TTS
    _ensure_http_context()
    tts_provider = getattr(settings, "TTS_PROVIDER", "elevenlabs").lower()

    primary = None
    if tts_provider == "deepgram" and settings.DEEPGRAM_API_KEY:
        try:
            primary = _build_deepgram_tts()
        except Exception as e:
            log_event("deepgram_tts_build_failed", error=str(e))
    if primary is None:
        try:
            primary = _build_elevenlabs_tts()
        except Exception as e:
            log_event("elevenlabs_tts_build_failed", error=str(e))
    if primary is None and settings.DEEPGRAM_API_KEY:
        try:
            log_event("falling_back_to_deepgram_tts")
            primary = _build_deepgram_tts()
        except Exception as fallback_err:
            log_event("deepgram_tts_fallback_failed", error=str(fallback_err))
    if primary is None:
        PRIMARY_TTS = None
        return None

    primary_is_elevenlabs = "elevenlabs" in type(primary).__module__
    secondary = None
    try:
        if primary_is_elevenlabs and settings.DEEPGRAM_API_KEY:
            secondary = _build_deepgram_tts()
        elif not primary_is_elevenlabs and settings.ELEVENLABS_API_KEY:
            secondary = _build_elevenlabs_tts()
    except Exception as e:
        log_event("secondary_tts_build_failed", error=str(e))

    PRIMARY_TTS = primary
    if secondary is not None:
        from livekit.agents.tts import FallbackAdapter
        return FallbackAdapter([primary, secondary])
    return primary


def build_vad() -> Any:
    """Build the Silero VAD provider (prewarmed once per worker)."""
    try:
        from livekit.plugins import silero
        return silero.VAD.load(
            min_speech_duration=settings.VAD_MIN_SPEECH_DURATION,
            min_silence_duration=settings.VAD_MIN_SILENCE_DURATION,
        )
    except Exception as e:
        log_event("vad_build_failed", error=str(e))
        return None

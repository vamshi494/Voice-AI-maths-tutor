# app/voice/session.py
"""LiveKit AgentSession configuration and entrypoint.

- Prewarmed Silero VAD
- Deepgram STT + ElevenLabs TTS with SSML parsing
- allow_interruptions=False, discard_audio_if_uninterruptible=False
- use_tts_aligned_transcript=True
- TutorAgent(mgr) with StopResponse
"""
from typing import Any
from livekit.agents import (
    AgentSession,
    JobProcess,
    JobContext,
    room_io,
)
from app.agents.adapter import StubLLM, TutorAgent
from app.voice.providers import build_stt, build_tts, build_vad
from app.voice.images import register_image_stream_handler
from app.transport import register_rpcs, register_report_stream_handler
from app.observability import log_event
from app.config import settings


def prewarm(proc: JobProcess) -> None:
    """Prewarm Silero VAD model and LangGraph pipeline once per worker process."""
    try:
        proc.userdata["vad"] = build_vad()
        log_event("silero_vad_prewarmed")
    except Exception as e:
        log_event("prewarm_failed", error=str(e))
    try:
        # Preload LangGraph, langchain_core, and pydantic.v1 during worker initialization
        # so the event loop thread is not stalled during turn execution.
        import app.agents.graph as _graph_mod
        proc.userdata["graph_warmed"] = True
        log_event("agent_graph_prewarmed")
    except Exception as e:
        log_event("graph_prewarm_failed", error=str(e))


def build_agent_session(ctx: JobContext, manager: Any) -> AgentSession:
    """Construct the configured AgentSession for this job."""
    vad_instance = ctx.proc.userdata.get("vad") if hasattr(ctx, "proc") and hasattr(ctx.proc, "userdata") else build_vad()
    if settings.INTERRUPT_MIN_WORDS < 3:
        log_event("min_words_below_invariant", value=settings.INTERRUPT_MIN_WORDS)

    session = AgentSession(
        vad=vad_instance,
        stt=build_stt(),
        tts=build_tts(),
        llm=StubLLM(),
        turn_handling={
            "turn_detection": "vad",
            # Interrupt behavior, checked against livekit-agents 1.8.2 (agent_activity.py):
            #  * enabled=False -> on_end_of_turn skips user input while the tutor speaks, so
            #    the classifier never hears anything said mid-answer.
            #  * enabled=True with min_words=0 -> _interrupt_by_audio_activity cuts TTS on
            #    every VAD blip: every "hmm" stops the tutor.
            #  * min_words=N: an utterance shorter than N words neither interrupts audio nor
            #    ends a turn, so backchannels are inaudible to the pipeline and the tutor keeps
            #    talking. N or more words cuts audio and reaches on_user_turn_completed; the
            #    manager replays the step if the classifier then says backchannel/no-op
            #    affirmation. INTERRUPT_MIN_WORDS supplies N and is never below 3.
            "interruption": {
                "enabled": True,
                "discard_audio_if_uninterruptible": False,
                "min_words": settings.INTERRUPT_MIN_WORDS,
                "resume_false_interruption": True,
                "false_interruption_timeout": 1.0,
            },
            "preemptive_generation": {
                "enabled": False,
            },
        },
        use_tts_aligned_transcript=True,
    )
    return session


async def start_session(ctx: JobContext, manager: Any) -> AgentSession:
    """Start LiveKit session with TutorAgent and synchronized text output."""
    session = build_agent_session(ctx, manager)
    agent = TutorAgent(manager)

    room_options = room_io.RoomOptions(
        text_output=room_io.TextOutputOptions(sync_transcription=True)
    )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_options,
    )
    if ctx.room:
        register_image_stream_handler(ctx.room, manager)
        if ctx.room.local_participant:
            register_rpcs(ctx.room.local_participant, manager)
        register_report_stream_handler(ctx.room, manager)
    log_event("livekit_session_started", room=ctx.room.name if ctx.room else "unknown")
    return session

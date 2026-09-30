# backend/tests/test_adapter_stop_response.py
"""LiveKit must never auto-reply to a student turn (offline).

livekit-agents 1.8.2 agent_activity.py `_user_turn_completed_task` ignores the RETURN value of
`Agent.on_user_turn_completed`; only a RAISED StopResponse skips the reply:

    try:
        await self._agent.on_user_turn_completed(temp_mutable_chat_ctx, new_message=user_message)
    except StopResponse:
        return  # ignore this turn

Otherwise it schedules a reply through `llm_node`: a committed student turn ("Continue.") would
make LiveKit re-speak the ACTIVE run from step 0. TutorAgent raises StopResponse to prevent
that.
"""
import pytest
from livekit.agents import StopResponse

from app.agents.adapter import TutorAgent
from app.state_machine.states import ConvEvent
from tests.fakes import settle
from tests.test_lifecycle_scenarios import _rig, _start_lesson


async def _livekit_user_turn_completed(agent: TutorAgent, text: str) -> list[str] | None:
    """The livekit-agents 1.8.2 control flow around on_user_turn_completed (see module doc)."""
    class Msg:
        text_content = text

    try:
        await agent.on_user_turn_completed(None, new_message=Msg())
    except StopResponse:
        return None                                  # no reply scheduled
    return [chunk async for chunk in agent.llm_node(None)]   # LiveKit's reply pipeline


@pytest.mark.asyncio
async def test_user_turn_never_triggers_a_livekit_reply(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await settle()
    agent = TutorAgent(r.mgr)
    r.labels.append("affirmation")

    reply = await _livekit_user_turn_completed(agent, "Continue.")
    await settle()

    assert reply is None, f"LiveKit generated a reply that re-speaks the lesson: {reply!r}"


@pytest.mark.asyncio
async def test_llm_node_never_replays_the_active_run(monkeypatch):
    """Defence in depth: if anything ever calls generate_reply, llm_node must not re-speak the
    active run from step 0 — the manager's own say() consumer owns lesson speech."""
    r = _rig(monkeypatch)
    await _start_lesson(r)
    await settle()
    agent = TutorAgent(r.mgr)
    chunks = [c async for c in agent.llm_node(None)]
    assert chunks == []

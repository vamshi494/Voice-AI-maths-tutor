# app/agents/adapter.py
"""TutorAgent adapter for LiveKit Agents.

- lesson speech is spoken by the manager's own session.say(<run consumer>); llm_node yields nothing
- on_user_turn_completed RAISES StopResponse: never produces an automatic reply
"""
from collections.abc import AsyncIterator
from typing import Any
from livekit.agents import Agent, StopResponse, llm
from app.observability import log_event
from app.state_machine.states import ConvEvent


class StubLLM(llm.LLM):
    """Stub LLM never called directly because llm_node drives generation from graph."""

    def __init__(self):
        super().__init__()

    def chat(self, *args, **kwargs):
        raise NotImplementedError("StubLLM.chat should never be called")


class TutorAgent(Agent):
    def __init__(self, manager: Any):
        super().__init__(instructions="You are Vamshi, an empathetic CBSE math tutor.")
        self.manager = manager

    async def llm_node(self, chat_ctx: Any = None, *args, **kwargs) -> AsyncIterator[str]:
        """LiveKit's reply pipeline. It must never speak lesson content.

        The manager speaks every run through session.say(<stream consumer>) from the right cursor.
        This node does not consume a run, so a LiveKit-initiated reply cannot re-speak the whole
        page over an already-drawn board. Nothing here.
        """
        log_event("llm_node_reply_suppressed")
        return
        yield  # pragma: no cover — makes this an async generator

    async def on_user_turn_completed(self, turn: Any, new_message: Any) -> None:
        """Invoked when user finishes speaking.

        LiveKit never produces an automatic reply. livekit-agents only
        honours a RAISED StopResponse (agent_activity._user_turn_completed_task: `except
        StopResponse: return`); a returned one is ignored and a reply is generated through
        llm_node. Our classifier in StateMachineManager handles the meaning of user turns.
        """
        log_event("on_user_turn_completed_intercepted")
        text = ""
        if hasattr(new_message, "text_content") and new_message.text_content:
            text = new_message.text_content
        elif hasattr(new_message, "text") and new_message.text:
            text = new_message.text
        elif hasattr(new_message, "content") and new_message.content:
            text = str(new_message.content)
        elif isinstance(new_message, str):
            text = new_message

        log_event("user_speech_transcript", text=text)
        if hasattr(self.manager, "handle_user_speech"):
            await self.manager.handle_user_speech(text)
        else:
            self.manager.fire(ConvEvent.USER_TURN_DONE, text=text)

        raise StopResponse()

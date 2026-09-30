# app/state_machine/turn_stream.py
"""TurnStream: step buffer shared by the graph producer and speech consumers."""
import asyncio

from app.contracts.agent_state import TurnRequest
from app.contracts.board_ops import Step


class TurnStream:
    """Step buffer shared by the graph producer and any number of speech consumers."""

    def __init__(self, req: TurnRequest) -> None:
        self.req = req
        self.items: list[tuple[Step, str]] = []
        self.done = False
        self.failed = False
        self.partial = False
        self._changed = asyncio.Event()

    def push(self, step: Step, tts: str) -> None:
        self.items.append((step, tts))
        self._changed.set()

    def finish(self, failed: bool = False, partial: bool = False) -> None:
        self.done = True
        self.failed = failed
        self.partial = partial
        self._changed.set()

    async def wait_change(self) -> None:
        await self._changed.wait()
        self._changed.clear()

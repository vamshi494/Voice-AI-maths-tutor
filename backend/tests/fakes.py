# backend/tests/fakes.py
"""Deterministic fakes that model LiveKit speech semantics.

These fakes behave like livekit.agents SpeechHandle, so tests can express "the tutor's
utterance finished while the student was talking" or "an interrupted handle's callback
fired into the next turn" — the cases that wedge the live lifecycle:
  * say(text | AsyncIterable[str]) consumes the source chunk by chunk
  * a handle finishes naturally when the source is exhausted (optionally held open)
  * interrupt() cancels playback, sets interrupted=True, then fires done-callbacks
"""
import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.contracts.board_ops import BoardOp, Step
from app.contracts.messages import StepEvt
from app.transport import send_event
from app.tutor.stream_parser import normalize_words


class FakeSpeechHandle:
    _next_id = 0

    def __init__(self, source: Any, allow_interruptions: bool) -> None:
        FakeSpeechHandle._next_id += 1
        self.id = f"speech_{FakeSpeechHandle._next_id}"
        self.source = source
        self.allow_interruptions = allow_interruptions
        self.spoken: list[str] = []
        self.interrupted = False
        self._done = False
        self._callbacks: list[Any] = []
        self._task: asyncio.Task | None = None
        self.release = asyncio.Event()          # tests set this to let a held handle finish
        self._finished_event = asyncio.Event()

    def done(self) -> bool:
        return self._done

    def add_done_callback(self, cb: Any) -> None:
        if self._done:
            asyncio.get_running_loop().call_soon(cb, self)
        else:
            self._callbacks.append(cb)

    def interrupt(self, force: bool = False) -> "FakeSpeechHandle":
        if not self._done:
            self.interrupted = True
            if self._task and not self._task.done():
                self._task.cancel()
            self._finish()
        return self

    async def wait_for_playout(self) -> None:
        await self._finished_event.wait()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._finished_event.set()
        for cb in list(self._callbacks):
            cb(self)


class FakeSession:
    def __init__(self, hold: bool = False, chunk_delay: float = 0.0) -> None:
        self.hold = hold                  # keep handles "playing" until handle.release is set
        self.chunk_delay = chunk_delay
        self.handles: list[FakeSpeechHandle] = []

    @property
    def said_text(self) -> list[str]:
        return ["".join(h.spoken) for h in self.handles]

    def say(self, text: Any, *, allow_interruptions: bool = True, add_to_chat_ctx: bool = True) -> FakeSpeechHandle:
        h = FakeSpeechHandle(text, allow_interruptions)
        self.handles.append(h)
        h._task = asyncio.get_running_loop().create_task(self._play(h))
        return h

    async def _play(self, h: FakeSpeechHandle) -> None:
        try:
            if isinstance(h.source, str):
                h.spoken.append(h.source)
                await asyncio.sleep(self.chunk_delay)
            else:
                async for chunk in h.source:
                    h.spoken.append(chunk)
                    await asyncio.sleep(self.chunk_delay)
            if self.hold:
                await h.release.wait()
        except asyncio.CancelledError:
            return
        h._finish()

    def interrupt(self, force: bool = False) -> None:
        for h in self.handles:
            if not h.done():
                h.interrupt(force=force)

    def latest(self) -> FakeSpeechHandle:
        return self.handles[-1]


def make_step(turn_id: str, generation: int, idx: int, text: str, write: str | None = None) -> Step:
    ops = []
    if write is not None:
        ops.append(BoardOp(op_id=f"{turn_id}:{idx}:0", kind="WRITE", at_word=0, text=write, row_id=f"w{idx + 1}"))
    return Step(turn_id=turn_id, generation=generation, step_index=idx, spoken_text=text,
                words=normalize_words(text), ops=ops)


def scripted_producer(scripts: dict[str, list[str]], delay: float = 0.0, fail: bool = False,
                      fail_after: int | None = None):
    """Replacement for app.agents.graph.iter_turn_steps: yields scripted steps per turn kind
    (the manager publishes StepEvt and records steps_sent).
    `fail_after=N` yields N steps then raises (mid-stream LLM failure)."""

    async def _iter(request, graph=None, row_tracker=None, agent_state=None, history=None,
                    stored_turns=None, gw=None, run_ctx=None, **kw) -> AsyncIterator[tuple[Step, str]]:
        if fail:
            raise RuntimeError("scripted producer failure")
        for i, text in enumerate(scripts.get(request.kind, [])):
            if fail_after is not None and i >= fail_after:
                raise RuntimeError("scripted producer failure")
            if delay:
                await asyncio.sleep(delay)
            if run_ctx is not None:
                if run_ctx.is_cancelled():
                    return
            elif agent_state is not None and agent_state.generation != request.generation:
                return
            step = make_step(request.turn_id, request.generation, i, text, write=f"row {i}")
            yield step, text
    return _iter


class EventRecorder:
    """Captures every server event sent through send_event (manager + graph)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, evt: Any, room: Any = None) -> str:
        self.events.append(evt)
        return "recorded"

    def of(self, type_name: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "type", None) == type_name]


async def settle(cycles: int = 12) -> None:
    """Let spawned tasks run to quiescence. Mixes zero-sleeps (task hand-offs) with short real
    sleeps so thread-backed I/O (aiosqlite persistence) can complete between hand-offs."""
    for _ in range(cycles):
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)

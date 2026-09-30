# backend/tests/test_memory.py
"""Memory tests: the dialogue window, the MEMORY prompt block and the summarizer."""
import asyncio

import pytest

from app.config import settings
from app.contracts.agent_state import ConvState
from app.contracts.memory import SessionMemory, SummaryOut
from app.memory.service import (
    HEARD_MAX_CHARS,
    LESSON_SO_FAR_MAX,
    MEMORY_CONTEXT_MAX_CHARS,
    ROLLING_SUMMARY_MAX,
    ROWS_MAX_CHARS,
    MemoryService,
)
from app.memory.summarizer import summarize
from tests.fakes import settle


class FakeGateway:
    """Offline gateway for the summarizer; an optional gate blocks the call."""

    def __init__(self, out: SummaryOut | None = None, gate: asyncio.Event | None = None) -> None:
        self.out = out
        self.gate = gate
        self.calls = 0

    async def complete_json(self, **kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.out


def test_context_sections_order():
    svc = MemoryService(SessionMemory(rolling_summary="Learnt BPT.",
                                      page_summaries={"p1": "BPT statement"}))
    block = svc.context_block("p1", [{"row_id": "w1", "text": "DE || BC"}], "we said this")
    assert block.index("LESSON SO FAR:") < block.index("PAGES:")
    assert block.index("PAGES:") < block.index("ON THE BOARD NOW:")
    assert block.index("ON THE BOARD NOW:") < block.index("JUST SAID:")
    assert "DE || BC" in block and "we said this" in block
    # Empty sections are omitted entirely.
    assert MemoryService().context_block("p1", [], "") == ""


def test_context_budget():
    svc = MemoryService()
    svc.lesson_id = "L1"
    svc.apply_summary("L1", "p_old", SummaryOut(page_summary="s" * 900, rolling_summary="r" * 1500))
    for i in range(20):
        svc.apply_summary("L1", f"p{i}", SummaryOut(page_summary="q" * 500))
    rows = [{"row_id": f"w{i}", "text": "x" * 60} for i in range(60)]
    heard = "start " + ("m" * 4000) + " end-marker"

    block = svc.context_block("p0", rows, heard)

    assert len(block) <= MEMORY_CONTEXT_MAX_CHARS
    assert len(svc.rolling_summary) <= ROLLING_SUMMARY_MAX
    sections = dict(part.split(":\n", 1) for part in block.split("\n\n"))
    assert len(sections["LESSON SO FAR"]) <= LESSON_SO_FAR_MAX
    assert len(sections["ON THE BOARD NOW"]) <= ROWS_MAX_CHARS
    assert len(sections["JUST SAID"]) <= HEARD_MAX_CHARS
    assert sections["JUST SAID"].endswith("end-marker"), "heard text keeps its end"


@pytest.mark.asyncio
async def test_history_uses_heard_steps(monkeypatch):
    """The memory window records only the steps the student heard; the
    ConversationTurn history is replaced while FEATURE_MEMORY is on."""
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch, scripts={"lesson": ["one alpha", "two beta", "three gamma", "four delta"],
                                   "doubt": ["doubt answer"], "resume": ["resumed"]})
    h = await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)
    h.release.set()
    await settle()

    assert r.mgr.state.conv_state == ConvState.IDLE
    dialogue = r.mgr.memory.dialogue
    assert dialogue and dialogue[-1].kind == "lesson"
    assert "one alpha" in dialogue[-1].tutor and "two beta" in dialogue[-1].tutor
    assert "three gamma" not in dialogue[-1].tutor
    assert dialogue[-1].student == "In triangle ABC find AC"
    assert r.mgr.state.history == [], "history is replaced by the MemoryService"
    assert [m["role"] for m in r.mgr.memory.chat_messages()] == ["user", "assistant"]


# ---------------------------------------------------------------------------------------------
# The background summarizer

@pytest.mark.asyncio
async def test_summarizer_never_blocks_turn(monkeypatch):
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    gate = asyncio.Event()
    gw = FakeGateway(out=SummaryOut(page_summary="done", rolling_summary="topic"), gate=gate)
    r.mgr.gateway = gw

    h = await _start_lesson(r)
    h.release.set()
    await settle()

    # The turn ends while the summary call is still blocked on its gate.
    assert r.mgr.state.conv_state == ConvState.IDLE
    assert r.rec.of("turn_ended")[-1].status == "complete"
    assert gw.calls == 1
    assert r.mgr.memory.page_summaries == {}

    gate.set()
    await settle()
    assert r.mgr.memory.page_summaries[r.mgr.state.page.page_id] == "done"


@pytest.mark.asyncio
async def test_summary_race_scoped(monkeypatch):
    """A summary that finishes after the next lesson started is dropped."""
    from app.state_machine.states import ConvEvent
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    gate = asyncio.Event()
    gw = FakeGateway(out=SummaryOut(page_summary="stale", rolling_summary="stale"), gate=gate)
    r.mgr.gateway = gw

    h = await _start_lesson(r, "question A")
    page_a = r.mgr.state.page.page_id
    h.release.set()
    await settle()
    assert gw.calls == 1                       # summary A is in flight, blocked

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="question B")
    await settle()
    page_b = r.mgr.state.page.page_id
    assert page_b != page_a

    gate.set()
    await settle()
    assert page_a not in r.mgr.memory.page_summaries
    assert all("stale" not in s for s in r.mgr.memory.page_summaries.values())


@pytest.mark.asyncio
async def test_summary_trimmed():
    gw = FakeGateway(out=SummaryOut(page_summary="p" * 500, rolling_summary="r" * 1200))
    out = await summarize(page_title="BPT", rows=[{"row_id": "w1", "text": "DE || BC"}],
                          heard_text="we proved it", doubts="why?", previous="", gw=gw)
    assert gw.calls == 1 and out is not None

    svc = MemoryService()
    svc.lesson_id = "L1"
    svc.apply_summary("L1", "p1", out)
    assert len(svc.page_summaries["p1"]) == 400
    assert len(svc.rolling_summary) == 900


def test_context_includes_page_summaries():
    svc = MemoryService()
    svc.lesson_id = "L1"
    svc.apply_summary("L1", "p1", SummaryOut(page_summary="BPT: DE || BC ⇒ AD/DB = AE/EC",
                                             rolling_summary="Learnt BPT."))
    block = svc.context_block("p1", [], "")
    assert "LESSON SO FAR:" in block and "Learnt BPT." in block
    assert "PAGES:" in block and "DE || BC" in block

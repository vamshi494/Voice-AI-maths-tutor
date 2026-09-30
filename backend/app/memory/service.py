# app/memory/service.py
"""MemoryService: the session's dialogue window and the MEMORY prompt block.

With `FEATURE_MEMORY` off the manager keeps its `AgentState.history` internals; with it
on, `on_turn_closed` is the single append point and `context_block` feeds the teaching
prompt. Section budgets are the fixed constants below.
"""
from app.contracts.memory import Exchange, SessionMemory, SummaryOut

HISTORY_EXCHANGES = 3                 # dialogue window (exchanges)
ROLLING_SUMMARY_MAX = 900
PAGE_SUMMARY_MAX = 400
LESSON_SO_FAR_MAX = 900
PAGES_MAX_CHARS = 2400
ROWS_MAX_CHARS = 1500
HEARD_MAX_CHARS = 1500                # keep the end
MEMORY_CONTEXT_MAX_CHARS = 7200


class MemoryService:
    def __init__(self, doc: SessionMemory | None = None) -> None:
        self.rolling_summary: str = doc.rolling_summary if doc is not None else ""
        self.page_summaries: dict[str, str] = dict(doc.page_summaries) if doc is not None else {}
        self.dialogue: list[Exchange] = list(doc.dialogue) if doc is not None else []
        # Set by the manager: summaries generated for an older lesson are dropped.
        self.lesson_id: str | None = None

    def on_turn_closed(self, kind: str, student_text: str | None, heard_text: str) -> None:
        """Append the closed turn; the window keeps the last `HISTORY_EXCHANGES`."""
        self.dialogue.append(Exchange(
            kind=kind if kind in ("lesson", "doubt", "resume") else "lesson",
            student=student_text or "",
            tutor=heard_text or "",
        ))
        if len(self.dialogue) > HISTORY_EXCHANGES:
            self.dialogue = self.dialogue[-HISTORY_EXCHANGES:]

    def chat_messages(self) -> list[dict[str, str]]:
        """The dialogue window as role dicts (teaching.py accepts them as-is)."""
        messages: list[dict[str, str]] = []
        for ex in self.dialogue[-HISTORY_EXCHANGES:]:
            if ex.student:
                messages.append({"role": "user", "content": ex.student})
            if ex.tutor:
                messages.append({"role": "assistant", "content": ex.tutor})
        return messages

    def context_block(self, page_id: str | None, rows: list[dict[str, str]],
                      heard_page_text: str) -> str:
        """The `MEMORY:` block: section budgets, empty sections omitted."""
        sections: list[str] = []
        if self.rolling_summary:
            sections.append("LESSON SO FAR:\n" + self.rolling_summary[:LESSON_SO_FAR_MAX])
        if self.page_summaries:
            lines: list[str] = []
            used = 0
            for pid, summary in reversed(list(self.page_summaries.items())):
                line = f"- {pid}: {summary}"
                if used + len(line) > PAGES_MAX_CHARS:
                    break
                lines.append(line)
                used += len(line)
            if lines:
                sections.append("PAGES:\n" + "\n".join(lines))
        if rows:
            listing = "\n".join(f"{r.get('row_id', '')}: {r.get('text', '')}" for r in rows)
            sections.append("ON THE BOARD NOW:\n" + listing[-ROWS_MAX_CHARS:])
        if heard_page_text:
            sections.append("JUST SAID:\n" + heard_page_text[-HEARD_MAX_CHARS:])
        block = "\n\n".join(sections)
        if len(block) > MEMORY_CONTEXT_MAX_CHARS:
            block = block[-MEMORY_CONTEXT_MAX_CHARS:]
        return block

    def apply_summary(self, lesson_id: str | None, page_id: str, out: SummaryOut) -> None:
        """Store a summarizer result; ignored when `lesson_id` is stale."""
        if lesson_id != self.lesson_id:
            return
        page = (out.page_summary or "").strip()
        rolling = (out.rolling_summary or "").strip()
        if page:
            self.page_summaries[page_id] = page[:PAGE_SUMMARY_MAX]
        if rolling:
            self.rolling_summary = rolling[:ROLLING_SUMMARY_MAX]

    def to_doc(self) -> SessionMemory:
        return SessionMemory(
            rolling_summary=self.rolling_summary,
            page_summaries=dict(self.page_summaries),
            dialogue=list(self.dialogue),
        )

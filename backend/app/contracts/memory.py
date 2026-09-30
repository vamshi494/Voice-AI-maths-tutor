# app/contracts/memory.py
"""Memory and board-state documents."""
from typing import Literal

from .base import CamelModel, LLMModel


class Exchange(CamelModel):
    """One closed turn as it enters the conversation window."""
    kind: Literal["lesson", "doubt", "resume"]
    student: str = ""
    tutor: str = ""


class SessionMemory(CamelModel):
    """The session's memory document (`board_state.state.memory`)."""
    rolling_summary: str = ""
    page_summaries: dict[str, str] = {}
    dialogue: list[Exchange] = []


class BoardStateDoc(CamelModel):
    """The persisted board state. `paused_lesson` is a PausedLesson dump
    (snake_case keys); load it with `PausedLesson.model_validate`."""
    version: Literal[2] = 2
    board_id: str
    memory: SessionMemory = SessionMemory()
    paused_lesson: dict | None = None
    lesson_plan: dict | None = None
    lesson_id: str | None = None
    current_page_id: str | None = None
    last_generation: int = 0


class SummaryOut(LLMModel):
    """`memory.summary.v1` result; trimmed to 400/900 by MemoryService."""
    page_summary: str = ""
    rolling_summary: str = ""

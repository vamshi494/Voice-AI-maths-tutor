# app/contracts/board_ops.py
from typing import Literal
from pydantic import Field
from .base import CamelModel, WireInModel

OpKind = Literal["WRITE", "PAUSE", "FOCUS", "EMPHASIZE", "ANNOTATE", "PAGE_BREAK"]
LESSON_ALLOWED_OPS: frozenset[str] = frozenset({"WRITE", "PAUSE", "FOCUS", "EMPHASIZE", "ANNOTATE", "PAGE_BREAK"})
DOUBT_ALLOWED_OPS: frozenset[str] = frozenset({"WRITE", "PAUSE", "FOCUS", "EMPHASIZE"})   # PAGE_BREAK is suppressed for doubts
PROMPT_FORBIDDEN_TAGS = (  # tags the parser drops from the model stream (no board-op equivalent)
    "DRAW_", "LABEL", "DIMENSION", "ARROW", "UNDERLINE", "CIRCLE_AROUND",
    "HIGHLIGHT", "SCRIBBLE", "ERASE", "CLEAR", "TYPE", "FRAME", "POINT",
)


class BoardOp(CamelModel):
    op_id: str                              # "{turn_id}:{step}:{n}" — idempotency key
    kind: OpKind
    at_word: int                            # spoken-word index within the step where the op fires
    text: str | None = None                 # WRITE
    row_id: str | None = None               # WRITE: server-assigned "wN" for the current page
    duration_ms: int | None = Field(None, ge=0, le=3000)   # PAUSE (clamped; ElevenLabs break)
    entity_id: str | None = None            # FOCUS (anchor or reveal-group id) / ANNOTATE (deferred id)
    focus_mode: Literal["outline", "spotlight", "pulse"] | None = None
    emphasize_row_id: str | None = None     # EMPHASIZE resolved to "wN"
    page_title: str | None = None           # PAGE_BREAK: optional title for the new page


class Step(CamelModel):
    turn_id: str
    generation: int
    step_index: int
    spoken_text: str                        # exactly what is sent to TTS (tags removed)
    words: list[str]                        # normalized spoken words
    ops: list[BoardOp]
    source_step_index: int | None = None    # index inside the page's run
    is_last: bool | None = None             # True on the final step of a completed run


class DoubtMark(WireInModel):               # client-resolved Mark & Ask target
    gesture: Literal["circle", "underline", "strike", "scribble", "point"]
    target_kind: Literal["work", "diagram", "empty"]
    text: str | None = None                 # row text or entity labels (≤240 chars)
    row_id: str | None = None               # "wN" when target_kind == "work"
    entity_id: str | None = None            # anchor id when target_kind == "diagram"

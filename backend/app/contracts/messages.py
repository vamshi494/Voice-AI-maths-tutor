# app/contracts/messages.py
from typing import Any, Literal
from pydantic import Field
from .base import CamelModel, WireInModel
from .board_ops import BoardOp, Step, DoubtMark
from .diagram import (
    DeferredAnnotation,
    DiagramAnchor,
    DiagramCommand,
    DiagramReveal,
    LabelFact,
    VerifiedDiagram,
)

# ---- server -> client, text stream topic "tutor.events" (every event carries generation) ----


class EvtBase(CamelModel):
    generation: int
    # Per-session monotonic sequence number stamped by transport.send_event. The client
    # de-duplicates on it (events may arrive on the packet path AND the text-stream path).
    seq: int | None = None
    # 8 hex chars, new per worker job; a new epoch means a new sequence book.
    epoch: str | None = None


class TurnStarted(EvtBase):
    type: Literal["turn_started"] = "turn_started"
    turn_id: str
    kind: Literal["lesson", "doubt", "resume"]
    page_id: str                        # server-owned ROOT id for this turn; the client derives sub-page ids on wrap
    new_page: bool                      # True -> client turns to a fresh page before drawing
    lesson_id: str | None = None        # id of the lesson this turn belongs to
    page_index: int | None = None       # chapter page number
    page_title: str | None = None       # chapter page title
    source_run_id: str | None = None    # set when steps are re-published from a parked run


class DiagramCommit(EvtBase):
    type: Literal["diagram_commit"] = "diagram_commit"
    turn_id: str
    page_id: str
    diagram: VerifiedDiagram            # client animates reveals in order, withholds deferred
    reference: bool = False             # late figure: reveal everything at the next step start


class Rect(CamelModel):                # same field names as DiagramAnchor, so the two shapes convert directly
    x: float
    y: float
    width: float
    height: float


class Block(CamelModel):               # one laid-out block of a page commit
    id: str
    role: Literal["figure", "table", "text"]
    rect: Rect
    sticky: bool = False
    commands: list[DiagramCommand] = []
    anchors: list[DiagramAnchor] = []
    reveals: list[DiagramReveal] = []
    deferred_annotations: list[DeferredAnnotation] | None = None
    label_glossary: dict[str, LabelFact] | None = None
    alias_map: dict[str, str] | None = None
    namespace: str = ""
    text_lines: list[str] | None = None     # table: first line = header; cells separated by " | "
    revealed_ids: list[str] = []             # reveal groups / deferred annotations already shown


class PageCommit(EvtBase):             # idempotent by (pageId, commitId)
    type: Literal["page_commit"] = "page_commit"
    turn_id: str
    page_id: str
    commit_id: str
    work_rect: Rect | None                   # None = figure-only page
    blocks: list[Block]
    carried_ids: list[str] = []
    reference: bool = False


class StepEvt(EvtBase):
    type: Literal["step"] = "step"
    step: Step


class TurnEnded(EvtBase):
    type: Literal["turn_ended"] = "turn_ended"
    turn_id: str
    status: Literal["complete", "partial"]
    visual_status: Literal["validated", "text_only", "retry_required"]
    page_only: bool = False                         # a non-last chapter page ended


class Aside(EvtBase):
    """Brackets tutor speech that is not a step: greeting, redirect, bridge, filler, ….

    The client suspends its word matcher between start and end, so aside words can never
    advance a step.
    """
    type: Literal["aside"] = "aside"
    phase: Literal["start", "end"]
    kind: Literal["greeting", "welcome", "redirect", "goodbye", "bridge", "filler"]


class TurnCancelled(EvtBase):
    type: Literal["turn_cancelled"] = "turn_cancelled"
    turn_id: str
    keep_board: bool = True             # keep the board visible on the client


class ReplayFromStep(EvtBase):
    type: Literal["replay_from_step"] = "replay_from_step"
    turn_id: str
    step_index: int                     # client resets its sync matcher to this step


class PageRestore(EvtBase):
    type: Literal["page_restore"] = "page_restore"
    page_id: str
    diagram: VerifiedDiagram | None
    ops: list[dict[str, Any]]           # stored BoardOps of that page, drawn instantly (no animation)


class SnapshotSubPage(CamelModel):
    sub_id: str
    ops: list[BoardOp]                  # acked ops only


class SnapshotPage(CamelModel):
    page_id: str
    title: str | None = None
    commit: PageCommit | None = None              # multi-block page commit (used on restore)
    diagram: VerifiedDiagram | None = None        # legacy single figure
    sub_pages: list[SnapshotSubPage] = Field(default_factory=list)   # the last one is shown


class PageHeader(CamelModel):
    page_id: str
    index: int
    title: str


class BoardSnapshot(EvtBase):
    type: Literal["board_snapshot"] = "board_snapshot"
    current: SnapshotPage
    stack: list[PageHeader] = Field(default_factory=list)            # earlier pages
    active_turn: TurnStarted | None = None
    pending_steps: list[Step] = Field(default_factory=list)          # from the heard cursor
    can_continue_lesson: bool = False


class ConvStateEvt(EvtBase):
    type: Literal["conv_state"] = "conv_state"
    state: str
    doubt_awaiting_resolution: bool
    can_continue_lesson: bool
    holds: list[str] = Field(default_factory=list)
    audio_mode: Literal["voice", "captions"] = "voice"
    lesson_id: str | None = None
    page_index: int | None = None
    page_count: int | None = None


class QuestionDraft(EvtBase):          # photo -> text, for the student to confirm or edit
    type: Literal["question_draft"] = "question_draft"
    text: str


from pydantic import ConfigDict, Field, model_validator


class VisionQuestion(CamelModel):
    question_text: str = Field(default="", alias="questionText")
    legible: bool = True

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _extract_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            text = (
                data.get("questionText")
                or data.get("question_text")
                or data.get("text")
                or data.get("transcription")
                or ""
            )
            return {
                "question_text": str(text).strip(),
                "legible": data.get("legible", True),
            }
        return data


class ErrorNotice(EvtBase):            # user-safe, never technical
    type: Literal["notice"] = "notice"
    message: str


class AudioStatus(EvtBase):
    type: Literal["audio_status"] = "audio_status"
    mode: Literal["voice", "captions"]
    reason: str | None = None


class SessionEnded(EvtBase):
    """The job ended this session for good; the client shows a blocking message."""
    type: Literal["session_ended"] = "session_ended"
    reason: Literal["opened_elsewhere", "ended"]


class LessonPlanEvt(EvtBase):           # the lesson plan announced when a chapter starts
    type: Literal["lesson_plan"] = "lesson_plan"
    lesson_id: str
    title: str
    pages: list[PageHeader]


# ---- client -> server, text stream topic "tutor.report" ----


class StepAck(WireInModel):
    type: Literal["step_ack"] = "step_ack"
    turn_id: str
    generation: int
    step_index: int
    drawn_op_ids: list[str]


class StepProgress(WireInModel):
    """Cumulative step progress; step_ack is still accepted."""
    type: Literal["step_progress"] = "step_progress"
    turn_id: str
    generation: int
    step_index: int                                           # turn-relative
    event: Literal["started", "completed", "final"]           # final: sent on turn_ended / turn_cancelled
    completed_by: Literal["words", "stall", "flush", "caption"] | None = None
    drawn_op_ids: list[str] = Field(default_factory=list)
    started_up_to: int = -1                                   # cumulative
    heard_up_to: int = -1                                     # cumulative contiguous heard
    first_word_ms: float | None = None
    last_word_ms: float | None = None
    early_ops: int = 0
    lookahead_jumps: int = 0


class ResyncRequest(WireInModel):
    type: Literal["resync_request"] = "resync_request"
    epoch: str | None = None
    last_seq: int
    reason: Literal["gap", "reconnected", "epoch"] = "gap"
    want_snapshot: bool = False


class PageTurned(WireInModel):
    type: Literal["page_turned"] = "page_turned"
    root_page_id: str
    from_sub_id: str
    to_sub_id: str
    cause: Literal["overflow", "page_break"]
    at_op_id: str | None = None             # overflow: the WRITE op that did not fit


class BoardRow(WireInModel):
    row_id: str
    text: str


class BoardReport(WireInModel):
    type: Literal["board_report"] = "board_report"
    page_id: str
    rows: list[BoardRow]
    rows_remaining: int                 # capacity left in the work column on this page


# ---- client -> server, RPC (method name -> payload) ----


class RpcSubmitQuestion(WireInModel):
    text: str = Field(min_length=1, max_length=2000)
    intent: Literal["new", "auto", "topic"] = "auto"


class RpcSubmitDoubt(WireInModel):
    typed_text: str = ""
    marks: list[DoubtMark] = Field(default_factory=list, max_length=6)


class RpcContinueLesson(WireInModel):
    pass


class RpcHold(WireInModel):
    reason: Literal["marker", "user_pause", "audio_blocked"]


class RpcMarkerArmed(WireInModel):
    pass


class RpcMarkerDisarmed(WireInModel):
    pass


class RpcSetSpeed(WireInModel):
    speed: Literal[0.8, 1.0, 1.2]


class RpcEndSession(WireInModel):
    pass


class RpcAck(CamelModel):
    ok: bool
    reason: Literal["not_now", "nothing_to_continue", "session_ended", "speed_unsupported"] | None = None

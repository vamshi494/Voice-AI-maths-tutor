# app/contracts/agent_state.py
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field
from .turn_plan import TurnPlan
from .diagram import VerifiedDiagram
from .board_ops import Step, DoubtMark


class ConvState(str, Enum):
    IDLE = "IDLE"
    GRAPH_RUNNING = "GRAPH_RUNNING"
    AGENT_SPEAKING = "AGENT_SPEAKING"
    INTERRUPT_DETECTED = "INTERRUPT_DETECTED"
    INTERRUPT_CLASSIFYING = "INTERRUPT_CLASSIFYING"
    INTERRUPT_IGNORED = "INTERRUPT_IGNORED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_CORRECTING = "TASK_CORRECTING"
    TASK_REDIRECTED = "TASK_REDIRECTED"
    TASK_CANCELLED = "TASK_CANCELLED"
    RESUMING = "RESUMING"


TurnKind = Literal["lesson", "doubt", "resume"]
VisualStatus = Literal["validated", "text_only", "retry_required"]


class PageRecord(BaseModel):
    """The live record of what the current page was taught from."""
    board_id: str
    page_id: str
    lesson_question: str
    turn_plan: TurnPlan | None = None
    solver_projection: dict[str, Any] | None = None      # {quantity_id: exact str}
    diagram: VerifiedDiagram | None = None
    visual_status: VisualStatus = "text_only"
    figure_drawn: bool = False
    turn_kind: TurnKind = "lesson"
    turn_id: str | None = None
    continues_board: bool = False
    saved: bool = False
    lesson_completed: bool = False             # set when the lesson (or its resume) finished speaking


class PausedLesson(BaseModel):
    """Always the ORIGINAL lesson, never a doubt."""
    board_id: str
    page_id: str
    lesson_question: str
    turn_plan: TurnPlan | None = None
    solver_projection: dict[str, Any] | None = None
    diagram: VerifiedDiagram | None = None
    figure_drawn: bool = False
    lesson_turn_id: str = ""
    last_acked_step_index: int = -1
    lesson_completed: bool = False             # True -> no "Continue lesson" (nothing left to teach)
    heard_steps_text: list[str] = Field(default_factory=list)   # spoken_text of heard steps, oldest first
    # Run/park fields
    lesson_id: str | None = None
    page_index: int = 0
    run_id: str | None = None
    resume_cursor: int = 0                     # source step index of the first un-heard step
    # Chapter fields
    completed_page_ids: list[str] = Field(default_factory=list)
    lesson_plan: dict | None = None            # LessonPlan.model_dump(by_alias=True)


class TurnRequest(BaseModel):
    """What the manager asks the graph to run (one per llm_node call)."""
    kind: TurnKind
    generation: int
    turn_id: str
    question: str                              # lesson: exact student question; doubt: lesson question
    student_text: str | None = None            # lesson: the question; doubt: accumulated doubt text; resume: None
    intent: Literal["new", "auto", "topic"] = "auto"   # from submit_question; read by the outline gate
    doubt_prompt: str | None = None            # built by build_marked_doubt_prompt
    marks: list[DoubtMark] = Field(default_factory=list)
    requires_new_figure: bool = False
    plan: Any | None = None
    diagram: Any | None = None
    namespace: str | None = None
    # Run/park fields
    run_id: str | None = None                  # set -> resume activates the parked run (no producer)
    page_index: int = 0
    lesson_id: str | None = None
    # Chapter fields (in-process): the page intent this run teaches and the chapter size.
    page_plan: Any | None = None               # PagePlan of a chapter page; set -> graph route "page"
    page_count: int = 0                        # number of pages in the chapter plan
    page_titles: list[str] = Field(default_factory=list)   # chapter page titles (page prompt)
    # Carried sticky blocks: [(BlockIntent, compiled RenderScene)] injected by the
    # runner, plus the reveal ids already shown on earlier pages (from the ledger).
    carried_stickies: Any | None = None
    carried_revealed: dict[str, list[str]] = Field(default_factory=dict)


class ConversationTurn(BaseModel):
    role: Literal["student", "tutor"]
    content: str


class RowMirror(BaseModel):
    page_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)   # [{row_id, text}] from board_report
    rows_remaining: int = 99
    next_row_number: int = 1                                  # server-side wN allocator


class AgentState(BaseModel):
    session_id: str
    user_id: str
    board_id: str
    generation: int = 0                          # increments per turn; stale callbacks from older turns are dropped
    conv_state: ConvState = ConvState.IDLE
    page: PageRecord | None = None               # live record of the page being taught
    paused_lesson: PausedLesson | None = None    # parked lesson kept for resume; never overwrite with a doubt
    active_turn_id: str | None = None
    active_turn_kind: TurnKind | None = None
    steps_sent: list[Step] = Field(default_factory=list)   # current turn's steps (for replay/flush)
    last_acked_step_index: int = -1              # from step_ack
    heard_step_index: int = -1                   # active turn, turn-relative
    published_upto: int = -1                     # active turn, turn-relative
    acked_op_ids: set[str] = Field(default_factory=set)
    current_step_index: int = 0                  # step handed to TTS (not heard)
    pending_request: TurnRequest | None = None
    pending_doubt: TurnRequest | None = None     # a doubt waiting to settle before it runs
    interrupt_transcript: str = ""               # accumulated during INTERRUPT_CLASSIFYING
    doubt_awaiting_resolution: bool = False
    marker_armed: bool = False
    holds: set[str] = Field(default_factory=set)   # "marker" | "user_pause" | "audio_blocked"
    audio_mode: Literal["voice", "captions"] = "voice"
    tts_speed: float = 1.0
    redirect_cursor: int = 0
    history: list[ConversationTurn] = Field(default_factory=list)
    rows: RowMirror | None = None
    lesson_topic: str = ""                       # short label for classifiers/redirect (from TurnPlan law_ids/question)
    # Run/page fields
    lesson_id: str | None = None
    page_index: int = 0
    has_next_page: bool = False                  # chapter page boundary (rule 29 vs 3)

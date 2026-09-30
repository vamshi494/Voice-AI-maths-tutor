# app/state_machine/manager.py
"""StateMachineManager: owns AgentState, the ConvState table, the turn producer and speech.

1. SERIALIZED EVENTS. handle_event runs under one asyncio.Lock. An independent task per event
   would let two events arriving together both read the same stale state across awaits, with
   the last writer winning.

2. PRODUCER / CONSUMER TURNS. The LangGraph pipeline runs as its own task (the producer)
   filling a TurnStream buffer. Speech is a consumer: session.say(<async iterator over the
   buffer from index k>). A graph run INSIDE llm_node would cancel generation on any
   interruption (marker pause, a >=3-word utterance), and "replay" could only re-speak the
   steps produced so far -- a paused 12-step lesson would end at step 5.

3. BOUND SPEECH CALLBACKS. Every speech handle's done-callback is bound to
   (generation, turn_id, speech token). A superseded or deliberately interrupted handle
   cannot fire SPEECH_ENDED into the NEXT turn and persist that new turn as
   "complete", send TurnEnded and go IDLE while the new turn is still planning.

4. NO DEAD ENDS. Speech that finishes during an interrupt window is remembered (rows 24/25)
   and resolved when classification returns; resume() returns the state it actually reached.
"""
import asyncio
import re
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from app.config import settings
from app.agents.nodes.outline import outline_lesson, wants_outline
from app.contracts.agent_state import (
    AgentState,
    ConversationTurn,
    ConvState,
    PageRecord,
    PausedLesson,
    TurnRequest,
)
from app.contracts.board_ops import BoardOp, DoubtMark, Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.messages import (
    Aside, AudioStatus, BoardSnapshot, ConvStateEvt, ErrorNotice, LessonPlanEvt, PageHeader,
    PageTurned, ReplayFromStep, SessionEnded, SnapshotPage, SnapshotSubPage, TurnCancelled,
    TurnEnded, TurnStarted,
)
from app.contracts.memory import BoardStateDoc
from app.gateway.groq_client import GroqGateway
from app.memory.service import MemoryService
from app.memory.summarizer import summarize
from app.observability import log_event, logger
from app.persistence import board_state as board_state_mod
from app.persistence import db as dbmod
from app.persistence.repo import get_stored_ops_for_page, persist_turn_from_state
from app.prompts.registry import BOARD_PREP_LINE, RESUME_BRIDGE_LINE
from app.state_machine.classifier import classify_figure_need, classify_interrupt
from app.state_machine.lesson_runner import LessonRunner, PageRun
from app.state_machine.page_ledger import PageLedger
from app.state_machine.states import TRANSITIONS, ConvEvent
from app.state_machine.turn_stream import TurnStream
from app.transport import send_event
from app.tutor.board_rows import BoardRowTracker, RowIdAllocator
from app.tutor.doubt_prompt import build_marked_doubt_prompt

REDIRECT_TEMPLATES = [
    "Let's keep our focus on {topic} for now. We can talk about that another time.",
    "That's not part of {topic}, so let's come back to it later.",
    "Let's stay with {topic}. We were right in the middle of it.",
]
FALLBACK_LINE = "Sorry, I couldn't work that one out. Could you ask it again, maybe in a different way?"
PARTIAL_PAUSE_LINE = "Let me pause here. Say continue when you're ready."
GOODBYE_LINE = "Okay, let's stop here. See you next time!"
# An explicit "carry on" cue: in IDLE with a resumable checkpoint it resumes like the Continue
# button (rule 9 would otherwise be a silent no-op); a bare "okay"/"yes" stays a no-op.
CONTINUE_CUE_RE = re.compile(
    r"\b(continue|go on|carry on|keep going|next page|move on|proceed|let'?s go on)\b")
RETRACTION_RE = re.compile(
    r"\b(never ?mind|forget it|got it|i see|i understand|understood|no doubt|it'?s fine|carry on|continue|go on)\b")
TURN_STATES = (ConvState.AGENT_SPEAKING, ConvState.GRAPH_RUNNING, ConvState.RESUMING, ConvState.TASK_CORRECTING)
AsideKind = Literal["greeting", "welcome", "redirect", "goodbye", "bridge", "filler"]


class StateMachineManager:
    def __init__(
        self,
        state: AgentState | None = None,
        session: Any = None,
        gateway: GroqGateway | None = None,
    ) -> None:
        self.state = state or AgentState(
            session_id=str(uuid4()),
            user_id="anonymous",
            board_id=f"b_{uuid4().hex[:8]}",
        )
        self.row_tracker = BoardRowTracker()
        self.row_ids = RowIdAllocator()
        self.stored_turns: list[Any] = []
        self.session = session
        self.room: Any = None
        self.gateway = gateway or GroqGateway()
        self.turn_task: asyncio.Task[Any] | None = None     # the active run's producer
        self.speech: Any = None
        self._stream: TurnStream | None = None
        self.lesson_runner = LessonRunner(self)
        self.ledger = PageLedger()
        self.memory = MemoryService()
        self._saved_turns: set[str] = set()
        self._prev_state: ConvState = ConvState.IDLE
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()          # strong refs: un-referenced tasks can be GC'd
        self._speech_seq = 0
        self._deliberate: set[int] = set()                   # speech tokens we interrupted on purpose
        self._turn_speech_started = False
        self._speech_finished = False                        # current turn's speech ended naturally
        self._speech_cut = False                             # LiveKit cut it (student barge-in)
        self._paused_mid_turn = False
        self._unpaused = asyncio.Event()
        self._unpaused.set()
        self._interrupt_seq = 0
        self._doubt_fig_counter = 0
        self._tts_failed: set[int] = set()                   # speech tokens whose TTS failed
        self._job_started = monotonic()
        self._turn_marks: dict[str, float] = {}              # marks of the active turn
        # Board lease: the job id owns the board; main replaces it with ctx.job.id.
        self.lease_owner: str = uuid4().hex
        self.lease_lost = False
        self._lease_task: asyncio.Task[Any] | None = None
        self._board_dirty = False
        self._dirty_seq = 0                                   # bumped by every write-point mark
        self._flush_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[Any] | None = None    # throttled mid-turn flush
        self._last_flush = 0.0
        self._doubt_marks: list[DoubtMark] = []
        self._doubt_typed = ""
        self._first_doubt_input = 0.0
        self._last_doubt_input = 0.0
        self._page_doubts: list[str] = []                    # doubt text heard on this page
        self._outline_task: asyncio.Task[Any] | None = None  # outline runs beside the lesson
        self.active_pointer_target: dict[str, Any] | None = None
        self.active_marks: list[Any] = []

    # ------------------------------------------------------------------ helpers
    def set_pointer_context(self, target: dict[str, Any] | None = None, marks: list[Any] | None = None) -> None:
        self.active_pointer_target = target
        if marks is not None:
            self.active_marks = marks

    @property
    def current_generation(self) -> int:
        return self.state.generation

    def _mark(self, name: str, run: PageRun | None = None) -> None:
        """Server marks: ms since job start, logged once per run where applicable."""
        if run is not None:
            if name in run.marked:
                return
            run.marked.add(name)
        ms = round((monotonic() - self._job_started) * 1000.0, 1)
        self._turn_marks.setdefault(name, ms)          # persisted with the turn
        log_event("turn_mark", mark=name, ms=ms)

    def turn_saved(self, turn_id: str) -> bool:
        return turn_id in self._saved_turns

    def mark_turn_saved(self, turn_id: str) -> None:
        self._saved_turns.add(turn_id)

    def can_continue(self) -> bool:
        pl = self.state.paused_lesson
        return pl is not None and not pl.lesson_completed

    def _page_title(self, index: int | None) -> str | None:
        """Title of a chapter page for TurnStarted/lesson_plan; None outside a chapter."""
        plan = self.lesson_runner.chapter_plan
        if plan is None or index is None or not (0 <= index < len(plan.pages)):
            return None
        return plan.pages[index].title or f"Page {index + 1}"

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def fire(self, event: ConvEvent, **kwargs: Any) -> None:
        """Schedule event handling (serialized by the manager lock)."""
        try:
            self._spawn(self.handle_event(event, **kwargs))
        except RuntimeError:
            asyncio.run(self.handle_event(event, **kwargs))

    async def handle_user_speech(self, text: str) -> None:
        await self.handle_event(ConvEvent.USER_TURN_DONE, text=text)

    async def emit_conv_state(self) -> None:
        st = self.state
        plan = self.lesson_runner.chapter_plan
        await send_event(ConvStateEvt(
            generation=st.generation,
            state=st.conv_state.value,
            doubt_awaiting_resolution=st.doubt_awaiting_resolution,
            can_continue_lesson=self.can_continue(),
            holds=sorted(st.holds),
            audio_mode=st.audio_mode,
            lesson_id=st.lesson_id,
            page_index=st.page_index,
            page_count=len(plan.pages) if plan is not None else None,
        ))

    # ------------------------------------------------------------------ dispatcher
    async def handle_event_ex(self, event: ConvEvent, **kwargs: Any) -> tuple[ConvState, bool]:
        """Serialized event handling; the bool says a transition rule actually ran.

        False = stale event dropped, illegal transition or no matching predicate; RPC handlers
        report that to the client as `RpcAck(ok=false, reason="not_now")`.
        """
        async with self._lock:
            return await self._handle_event_locked_ex(event, **kwargs)

    async def handle_event(self, event: ConvEvent, **kwargs: Any) -> ConvState:
        return (await self.handle_event_ex(event, **kwargs))[0]

    def _is_stale_event(self, event: ConvEvent, kwargs: dict[str, Any]) -> bool:
        st = self.state
        gen = kwargs.get("generation")
        if gen is not None and gen != st.generation:
            return True
        turn_id = kwargs.get("turn_id")
        if event == ConvEvent.SPEECH_ENDED and turn_id is not None and turn_id != st.active_turn_id:
            return True
        seq = kwargs.get("interrupt_seq")
        return seq is not None and seq != self._interrupt_seq

    async def _handle_event_locked_ex(self, event: ConvEvent, **kwargs: Any) -> tuple[ConvState, bool]:
        curr = self.state.conv_state
        if curr == ConvState.TASK_CANCELLED:
            log_event("ignored_in_terminal_state", event=event.value)
            return curr, False
        if self._is_stale_event(event, kwargs):
            log_event("stale_event_dropped", event=event.value, state=curr.value)
            return curr, False

        rules = TRANSITIONS.get((curr, event), [])
        rule = next((r for r in rules if r.predicate is None or r.predicate(self.state)), None)
        if rule is None:
            log_event("illegal_transition" if not rules else "no_matching_predicate",
                      from_state=curr.value, event=event.value)
            return curr, False

        target: ConvState | None = rule.to_state
        rid = rule.rule_id
        st = self.state

        if rid == 1 or rid == 14:
            q_text = (kwargs.get("text") or kwargs.get("question") or st.interrupt_transcript or "").strip()
            if not q_text:
                log_event("empty_question_ignored")
                target = self._prev_state if curr == ConvState.INTERRUPT_CLASSIFYING else curr
            else:
                await self.supersede("new_question")
                if st.lesson_id:
                    self.lesson_runner.discard_lesson(st.lesson_id)   # rule 1/14 discards the lesson
                st.paused_lesson = None
                st.audio_mode = "voice"                               # a new lesson retries voice
                await self.run_turn(TurnRequest(kind="lesson", generation=st.generation,
                                                turn_id=str(uuid4()), question=q_text,
                                                student_text=q_text,
                                                intent=kwargs.get("intent", "auto")))
                target = ConvState.GRAPH_RUNNING

        elif rid == 2:
            self._turn_speech_started = True
            target = ConvState.TASK_REDIRECTED if curr == ConvState.TASK_REDIRECTED else ConvState.AGENT_SPEAKING

        elif rid == 3:
            await self._complete_turn()

        elif rid == 29:
            # The page's speech ended; finish the page and spawn the gap timer OUTSIDE
            # the lock (the handler must return so VAD/RPC events can cancel the advance).
            await self._complete_turn(page_only=True)
            target = ConvState.GRAPH_RUNNING
            self.lesson_runner.spawn_advance()

        elif rid == 4:
            self.lesson_runner.cancel_gap()      # a barge-in cancels a running page gap
            self._prev_state = curr
            self._interrupt_seq += 1
            st.interrupt_transcript = ""

        elif rid == 5:
            if curr == ConvState.IDLE:           # STT final without a VAD start
                self._prev_state = ConvState.IDLE
                self._interrupt_seq += 1
                st.interrupt_transcript = ""
            self._append_transcript(kwargs.get("text", ""))
            self._schedule_classification()

        elif rid == 6:
            self._append_transcript(kwargs.get("text", ""))
            if self._prev_state == ConvState.IDLE:
                self._interrupt_seq += 1
                self._schedule_classification()
            return curr, True

        elif rid in (7, 9):
            log_event("interrupt_resolved_noop", rule=rid, prev_state=self._prev_state.value)
            target = await self._return_from_interrupt()

        elif rid == 8:
            target = await self.resume()

        elif rid in (10, 11):
            if rid == 11:
                await self.begin_doubt("marks", typed_text=kwargs.get("typed_text", ""), marks=kwargs.get("marks", []))
            else:
                await self.begin_doubt("voice")

        elif rid == 30:
            await self.amend_question(st.interrupt_transcript)

        elif rid == 12:
            target = await self._handle_doubt_settled()

        elif rid == 13:
            self._turn_speech_started = True
            st.doubt_awaiting_resolution = True

        elif rid == 15:
            await self.off_topic()

        elif rid == 16:
            target = await self._after_redirect()

        elif rid in (17, 18):
            await self.end_session()
            target = ConvState.TASK_CANCELLED

        elif rid == 19:
            target = await self.resume()

        elif rid == 20:
            self._turn_speech_started = True
            st.doubt_awaiting_resolution = False

        elif rid == 21:
            await self._add_hold(kwargs.get("reason", "marker"))
            target = curr

        elif rid == 22:
            await self._remove_hold(kwargs.get("reason", "marker"))
            target = curr

        elif rid == 24:
            self._speech_finished = True
            target = curr

        elif rid == 25:
            self._turn_speech_started = True
            target = curr

        elif rid == 31:
            # Typed text with a lesson on the board: classify exactly like speech.
            text = (kwargs.get("text") or "").strip()
            if curr in (ConvState.INTERRUPT_DETECTED, ConvState.INTERRUPT_CLASSIFYING):
                self._append_transcript(text)
            else:
                self._prev_state = curr
                st.interrupt_transcript = text
            self._interrupt_seq += 1
            self._schedule_classification(skip_settle=True, input_mode="typed")
            target = ConvState.INTERRUPT_CLASSIFYING

        elif rid == 26:
            # Speech during the settle joins the doubt and re-arms the quiet timer.
            self._append_transcript(kwargs.get("text", ""))
            self._rearm_settle()
            target = ConvState.TASK_PAUSED

        elif rid == 27:
            self._rearm_settle()
            target = ConvState.TASK_PAUSED

        elif rid == 28:
            # A second mark/doubt merges into the pending one (dedupe on the target key).
            for m in kwargs.get("marks", []) or []:
                dm = m if isinstance(m, DoubtMark) else DoubtMark.model_validate(m)
                key = (dm.target_kind, dm.row_id, dm.entity_id)
                if all((x.target_kind, x.row_id, x.entity_id) != key for x in self._doubt_marks):
                    self._doubt_marks.append(dm)
            typed = (kwargs.get("typed_text") or "").strip()
            if typed:
                self._doubt_typed = f"{self._doubt_typed} {typed}".strip()
            self._rearm_settle()
            target = ConvState.TASK_PAUSED

        elif rid == 32:
            # While a doubt settles, typed text joins that doubt and re-arms the timer.
            self._append_transcript(kwargs.get("text", ""))
            self._rearm_settle()
            target = ConvState.TASK_PAUSED

        if target is not None:
            st.conv_state = target
        log_event("conv_transition", from_state=curr.value, event=event.value,
                  to_state=st.conv_state.value, rule_id=rid)
        await self.emit_conv_state()
        return st.conv_state, True

    def _append_transcript(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            # Keep the separator: joining with a space avoids "waitwhy is that".
            self.state.interrupt_transcript = f"{self.state.interrupt_transcript} {text}".strip()

    # ------------------------------------------------------------------ turn lifecycle
    async def supersede(self, reason: str, keep_board: bool = True) -> None:
        """Generation bump, cancel the active run, interrupt speech, persist partial."""
        st = self.state
        # An outline that outlives its lesson must never start a chapter over a doubt or a
        # new question. Never cancel the task we are running inside (chapter start).
        if (self._outline_task is not None and not self._outline_task.done()
                and self._outline_task is not asyncio.current_task()):
            self._outline_task.cancel()
            log_event("outline_cancelled", reason=reason)
        self.lesson_runner.cancel_gap()          # a new turn/supersede kills a page gap
        old_turn = st.active_turn_id
        st.generation += 1
        run = self.lesson_runner.active
        if run is not None and run.mode != "parked":
            self.lesson_runner.discard_run(run.run_id)
        elif run is None and self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.lesson_runner.active = None
        self._interrupt_speech(f"supersede:{reason}")
        if self.session and hasattr(self.session, "interrupt"):
            try:
                self.session.interrupt(force=True)
            except Exception:
                pass
        if old_turn:
            await send_event(TurnCancelled(generation=st.generation, turn_id=old_turn, keep_board=keep_board))
            if not self.turn_saved(old_turn):
                self._close_history(st.pending_request, self._heard_steps())
                note = "the lesson paused here for a doubt" if reason == "doubt" else None
                try:
                    await self._persist_turn(status="partial", paused_note=note)
                except Exception as e:
                    logger.warning(f"partial persist failed: {e}")
                self.mark_turn_saved(old_turn)
        st.steps_sent.clear()
        st.pending_request = None
        st.pending_doubt = None
        self._stream = None
        self._paused_mid_turn = False

    async def run_turn(self, req: TurnRequest) -> None:
        """Start a turn: page bookkeeping, TurnStarted, PageRun producer, speech consumer."""
        st = self.state
        st.pending_request = req
        st.active_turn_id = req.turn_id
        st.active_turn_kind = req.kind
        st.steps_sent = []
        st.last_acked_step_index = -1
        st.heard_step_index = -1
        st.published_upto = -1
        st.acked_op_ids = set()
        st.current_step_index = 0
        self._turn_marks = {}
        self._turn_speech_started = False
        self._speech_finished = False
        self._speech_cut = False
        self.row_tracker.current_turn_write_row_ids = []

        # Resume / page-advance activation: an existing (parked) run is re-published
        # from its heard cursor; no producer is spawned.
        run = self.lesson_runner.run_for(req.run_id)
        if run is not None and run.mode != "discarded":
            st.page = run.ctx.page_record
            st.page_index = int(run.page_index or 0)
            st.has_next_page = self.lesson_runner.has_next_page()
            await send_event(TurnStarted(generation=st.generation, turn_id=req.turn_id, kind=req.kind,
                                         page_id=run.page_id, new_page=False,
                                         lesson_id=run.lesson_id, page_index=run.page_index,
                                         page_title=self._page_title(run.page_index),
                                         source_run_id=run.run_id))
            cursor = self.lesson_runner.resume_cursor(run) if req.kind == "resume" else 0
            # Refresh the run's tracker from the ledger — rows acked before the pause (or
            # while a doubt was active) may have arrived after the run was created.
            tracker = run.ctx.row_tracker
            if tracker is not None:
                tracker.record_board_report(
                    run.page_id, self.ledger.rows_for_prompt(run.page_id), tracker.rows_remaining)
            await self.lesson_runner.publisher.activate(run, cursor, req.turn_id, st.generation)
            self.lesson_runner.active = run
            self._stream = run.stream
            self.turn_task = run.task
            if req.kind == "resume":
                gen, tid = st.generation, req.turn_id
                # The bridge runs first; step speech starts only from its done-callback, after
                # aside{end} is recorded, so the first step word meets a live matcher.
                await self.speak_aside(RESUME_BRIDGE_LINE, "bridge",
                                       on_done=lambda: self._start_speech_if_current(run, 0, gen, tid))
            else:
                self._start_speech(run, 0)
            return

        # A new LESSON always starts on a fresh page, so a second question never lands
        # on top of the first question's board. New-figure doubts get a fresh page.
        new_page = req.kind == "lesson" or req.requires_new_figure or st.page is None
        if new_page:
            if req.page_plan is not None and req.lesson_id:
                # Chapter pages have deterministic ids ({lesson_id}_p{i}) so the lesson_plan
                # event, persistence rows and the notes drawer all agree.
                page_id = f"{req.lesson_id}_p{int(req.page_index or 0)}"
            else:
                page_id = f"p_{req.turn_id[:8]}"
            st.page = PageRecord(board_id=st.board_id, page_id=page_id, lesson_question=req.question,
                                 turn_kind=req.kind, turn_id=req.turn_id,
                                 continues_board=req.kind != "lesson")
            self.row_tracker.record_board_report(page_id, [], 99)
            self._page_doubts = []
        else:
            page_id = st.page.page_id

        if req.kind == "lesson" and new_page:
            if not req.lesson_id:
                req.lesson_id = f"L_{req.turn_id[:6]}"
            st.lesson_id = req.lesson_id
            st.page_index = int(req.page_index or 0)
            self.memory.lesson_id = st.lesson_id
        elif req.lesson_id is None:
            req.lesson_id = st.lesson_id
        req.page_index = st.page_index
        if req.page_plan is not None:
            # A chapter page knows how many pages follow it: rule 29 vs rule 3 (states.py).
            st.has_next_page = int(req.page_index or 0) + 1 < int(req.page_count or 0)
        else:
            st.has_next_page = False

        self._mark("turn_started")
        await send_event(TurnStarted(generation=st.generation, turn_id=req.turn_id, kind=req.kind,
                                     page_id=page_id, new_page=new_page,
                                     lesson_id=st.lesson_id, page_index=st.page_index,
                                     page_title=self._page_title(st.page_index)))

        run = self.lesson_runner.new_run(req, st.page)
        self.lesson_runner.active = run
        self._stream = run.stream
        self.turn_task = run.task
        self._start_speech(run, 0)

        # The outline runs concurrently with the lesson pipeline. Started after the
        # first page run so its job can never re-enter run_turn mid-construction; it switches to
        # chapter mode only before any step/speech was published (see `_outline_job`).
        if (req.kind == "lesson" and req.page_plan is None and settings.FEATURE_CHAPTERS
                and self.lesson_runner.chapter_plan is None
                and wants_outline(req.question, req.intent)):
            self._start_outline(req)

    # ------------------------------------------------------------------ outline / chapter
    def _start_outline(self, req: TurnRequest) -> None:
        if self._outline_task is not None and not self._outline_task.done():
            log_event("outline_already_running", turn_id=req.turn_id)
            return
        gen = self.state.generation
        summary = self.memory.rolling_summary if settings.FEATURE_MEMORY else ""
        self._outline_task = self._spawn(self._outline_job(req, gen, summary))

    async def _outline_job(self, req: TurnRequest, gen: int, memory_summary: str) -> None:
        """Outline concurrently with the lesson; switch to a chapter only pre-speech, pre-steps."""
        try:
            plan = await outline_lesson(req.question, memory_summary, self.gateway)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # outline_lesson already swallows; belt and braces
            log_event("outline_failed", error=f"{type(e).__name__}: {e}")
            plan = None
        if plan is None or plan.scope != "topic" or not plan.pages:
            log_event("outline_fallback_problem", pages=len(plan.pages) if plan else 0,
                      scope=plan.scope if plan else None)
            return
        async with self._lock:
            st = self.state
            if gen != st.generation:
                log_event("outline_stale_ignored", gen=gen, current=st.generation)
                return
            if st.active_turn_kind != "lesson" or st.page is None:
                log_event("outline_late_ignored", reason="no_lesson")
                return
            if st.published_upto >= 0 or self._turn_speech_started:
                # The single-page lesson already reached the student: never cut it mid-sentence.
                log_event("outline_late_ignored", reason="lesson_started")
                return
            await self._start_chapter(plan, req)

    async def _start_chapter(self, plan: Any, req: TurnRequest) -> None:
        """Discard the provisional single-page lesson and teach page 0 of the outline."""
        st = self.state
        await self.supersede("chapter")
        if st.lesson_id:
            self.lesson_runner.discard_lesson(st.lesson_id)
        st.paused_lesson = None
        lesson_id = st.lesson_id or f"L_{req.turn_id[:6]}"
        st.lesson_id = lesson_id
        self.memory.lesson_id = lesson_id
        self.lesson_runner.start_chapter(plan, lesson_id)
        log_event("chapter_started", lesson_id=lesson_id, pages=len(plan.pages))
        await send_event(LessonPlanEvt(
            generation=st.generation,
            lesson_id=lesson_id,
            title=plan.title or req.question,
            pages=[PageHeader(page_id=f"{lesson_id}_p{i}", index=i,
                              title=p.title or f"Page {i + 1}")
                   for i, p in enumerate(plan.pages)],
        ))
        page_req = self.lesson_runner.page_request(0)
        if page_req is None:
            log_event("chapter_empty_ignored", lesson_id=lesson_id)
            return
        await self.run_turn(page_req)

    async def _consume(self, run: PageRun, start: int, token: int) -> AsyncIterator[str]:
        """Speech source: the run's stream from turn-relative `start`, waiting for the producer."""
        i = start
        prep_spoken = False
        consumer_started = monotonic()
        while True:
            if token != self._speech_seq:
                return                                   # a newer speech replaced this one
            if run.publish_from + i < len(run.stream.items):
                if self.state.marker_armed:
                    await self._unpaused.wait()          # pause gate (marker picked up)
                    if token != self._speech_seq:
                        return
                _step, tts = run.stream.items[run.publish_from + i]
                self.state.current_step_index = i
                i += 1
                yield tts + " "
                continue
            if run.stream.done:
                if not run.stream.items and run.stream.failed and start == 0:
                    yield FALLBACK_LINE
                elif run.stream.failed and run.stream.items:
                    yield PARTIAL_PAUSE_LINE          # after the last step of a cut-off stream
                return
            # The lesson's board-prep line, spoken once if the figure/plan takes long.
            if (not prep_spoken and start == 0 and run.kind == "lesson"
                    and not run.stream.items):
                remaining = (consumer_started + settings.BOARD_PREP_LINE_AFTER_MS / 1000.0
                             - monotonic())
                if remaining <= 0:
                    prep_spoken = True
                    yield BOARD_PREP_LINE + " "
                    continue
                try:
                    await asyncio.wait_for(run.stream.wait_change(), remaining)
                except asyncio.TimeoutError:
                    pass
                continue
            await run.stream.wait_change()

    def _start_speech(self, run: PageRun, start: int) -> None:
        self._speech_seq += 1
        token = self._speech_seq
        gen, turn_id = self.state.generation, self.state.active_turn_id
        self._speech_finished = False
        self._speech_cut = False
        self._mark("first_audio")
        if self.state.audio_mode == "captions":
            # No agent audio: the caption pacer advances the step clock at reading speed.
            self._spawn(self._caption_pacer(run, start, token, gen, turn_id))
            return
        if not self.session:
            return
        handle = self.session.say(self._consume(run, start, token),
                                  allow_interruptions=True, add_to_chat_ctx=False)
        self.speech = handle
        if hasattr(handle, "add_done_callback"):
            handle.add_done_callback(lambda h: self._on_speech_done(h, gen, turn_id, token))

    def _start_speech_if_current(self, run: PageRun, start: int, gen: int, turn_id: str) -> None:
        """Aside done-callback: start the step speech only for the still-current turn."""
        if self.state.generation == gen and self.state.active_turn_id == turn_id:
            self._start_speech(run, start)

    async def _wait_heard(self, run: PageRun, src: int, timeout: float) -> None:
        """Caption pacing: wait until the step at source index `src` was heard, or timeout."""
        deadline = monotonic() + timeout
        while run.heard_upto < src and monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _caption_pacer(self, run: PageRun, start: int, token: int, gen: int, turn_id: str) -> None:
        """Caption mode advances one step per reading-time once the student heard it."""
        self.fire(ConvEvent.SPEECH_STARTED)          # no agent audio -> LiveKit never emits it
        i = start
        while token == self._speech_seq:
            if run.publish_from + i < len(run.stream.items):
                await self._unpaused.wait()
                step, _tts = run.stream.items[run.publish_from + i]
                est = (len(step.words) / settings.CAPTION_WPS
                       / max(0.5, self.state.tts_speed) * 1.5 + 2.0)
                await self._wait_heard(run, run.publish_from + i, timeout=est)
                i += 1
                continue
            if run.stream.done:
                break
            await run.stream.wait_change()
        if token == self._speech_seq:
            self.fire(ConvEvent.SPEECH_ENDED, generation=gen, turn_id=turn_id)

    def on_session_error(self, ev: Any) -> None:
        """An unrecoverable TTS error switches the turn to caption mode."""
        err = getattr(ev, "error", None)
        source = type(getattr(ev, "source", None))
        is_tts = (type(err).__name__ == "TTSError"
                  or "tts" in (getattr(source, "__module__", "") or "").lower())
        if is_tts and not getattr(err, "recoverable", False) and settings.FEATURE_CAPTIONS:
            self._tts_failed.add(self._speech_seq)
            self.state.audio_mode = "captions"
            self._spawn(self._enter_captions())

    async def _enter_captions(self) -> None:
        # Voice/speech transitions serialize through the manager lock.
        async with self._lock:
            await send_event(AudioStatus(generation=self.state.generation, mode="captions",
                                         reason="tts_unavailable"))
            log_event("audio_mode_captions")
            await self.replay_from_step(self._replay_index())

    async def speak_aside(self, text: str, kind: AsideKind, on_done: Any = None) -> None:
        """Bracket non-step tutor speech with aside{start}/aside{end}.

        Never awaits playout (this runs inside the manager lock); step speech that must follow
        an aside is started from `on_done`, after the end event is sent.
        """
        gen = self.state.generation
        await send_event(Aside(generation=gen, phase="start", kind=kind))
        if self.session is None or getattr(self.state, "audio_mode", "voice") == "captions":
            await send_event(ErrorNotice(generation=gen, message=text))
            await send_event(Aside(generation=gen, phase="end", kind=kind))
            if on_done:
                on_done()
            return
        handle = self.session.say(text, allow_interruptions=False, add_to_chat_ctx=False)

        async def _end_aside() -> None:
            await send_event(Aside(generation=gen, phase="end", kind=kind))
            # A superseded aside must not start the speech of a cancelled turn.
            if on_done and self.state.generation == gen:
                on_done()

        def _done(_h: Any) -> None:
            self._spawn(_end_aside())

        if hasattr(handle, "add_done_callback"):
            handle.add_done_callback(_done)

    def _on_speech_done(self, handle: Any, gen: int, turn_id: str | None, token: int) -> None:
        if token != self._speech_seq or gen != self.state.generation or turn_id != self.state.active_turn_id:
            log_event("speech_done_stale_ignored", token=token, current=self._speech_seq)
            return
        if token in self._deliberate:
            return                                       # our own pause / redirect / supersede
        if token in self._tts_failed:
            return                                       # its TTS failed: captions take over
        if getattr(handle, "interrupted", False) is True:
            self._speech_cut = True                      # LiveKit cut it: student barge-in
            log_event("speech_cut_by_user", turn_id=turn_id)
            return
        self._speech_finished = True
        self.fire(ConvEvent.SPEECH_ENDED, generation=gen, turn_id=turn_id)

    def _interrupt_speech(self, reason: str) -> None:
        self._deliberate.add(self._speech_seq)
        # Bound the set: tokens older than the last 50 can never be checked again.
        cutoff = self._speech_seq - 50
        if len(self._deliberate) > 50:
            self._deliberate = {tok for tok in self._deliberate if tok >= cutoff}
        handle = self.speech
        if handle is not None:
            try:
                handle.interrupt(force=True)
            except TypeError:
                try:
                    handle.interrupt()
                except Exception:
                    pass
            except Exception:
                pass
        log_event("speech_interrupted", reason=reason)

    def _replay_index(self) -> int:
        run = self.lesson_runner.active
        if run is None:
            n = len(self._stream.items) if self._stream else len(self.state.steps_sent)
            idx = self.state.last_acked_step_index + 1
            return max(0, min(idx, max(0, n - 1)))
        idx = run.heard_upto + 1 - run.publish_from
        return max(0, min(idx, max(0, run.published_upto - run.publish_from)))

    def _heard_steps(self) -> list[Step]:
        """Steps the student actually heard: the run's contiguous heard source prefix
        (`run.stream.items[:run.heard_upto+1]`), including a re-published run's prefix."""
        run = self.lesson_runner.active
        if run is not None and run.heard_upto >= 0:
            return [s for s, _tts in run.stream.items[: run.heard_upto + 1]]
        return self.state.steps_sent[: self.state.heard_step_index + 1]

    def _close_history(self, req: TurnRequest | None, heard_steps: list[Step]) -> None:
        """The closed turn enters memory or falls back to ConversationTurn history."""
        if settings.FEATURE_MEMORY:
            kind = self.state.active_turn_kind or (
                self.state.page.turn_kind if self.state.page else "lesson")
            self.memory.on_turn_closed(
                kind,
                req.student_text if req is not None else None,
                " ".join(s.spoken_text for s in heard_steps),
            )
            return
        if req is None or req.student_text is None:
            return
        st = self.state
        st.history.append(ConversationTurn(role="student", content=req.student_text))
        st.history.append(ConversationTurn(role="tutor", content=" ".join(s.spoken_text for s in heard_steps)))

    def _write_chapter_checkpoint(self, page_index: int, run_id: str | None = None) -> None:
        """A resumable checkpoint at a chapter page (second prefetch failure, doubt in the
        gap, mid-chapter doubt). The plan travels in `lesson_plan` so a reload can rebuild
        page titles and page count."""
        st = self.state
        plan = self.lesson_runner.chapter_plan
        if plan is None:
            return
        lesson_id = st.lesson_id or "L_chapter"
        page_id = f"{lesson_id}_p{page_index}"
        pl = st.paused_lesson or PausedLesson(
            board_id=st.board_id, page_id=page_id,
            lesson_question=plan.title or self._page_title(page_index) or "")
        pl.board_id = st.board_id
        pl.page_id = page_id
        pl.lesson_question = plan.title or pl.lesson_question
        pl.lesson_id = lesson_id
        pl.page_index = page_index
        pl.run_id = run_id
        pl.resume_cursor = 0
        pl.lesson_completed = False
        pl.completed_page_ids = [f"{lesson_id}_p{i}" for i in range(page_index)]
        pl.lesson_plan = plan.model_dump(by_alias=True)
        st.paused_lesson = pl

    def _chapter_next_page_run(self, pl: PausedLesson) -> PageRun | None:
        """The parked (or newly started) run of the page after the checkpoint's page.

        None when the checkpoint is not a chapter one or the lesson's last page was reached.
        """
        if not pl.lesson_plan or not pl.lesson_id:
            return None
        try:
            from app.contracts.lesson import LessonPlan
            pages = LessonPlan.model_validate(pl.lesson_plan).pages
        except Exception as e:
            log_event("chapter_plan_restore_failed", error=f"{type(e).__name__}: {e}")
            return None
        nxt = int(pl.page_index or 0) + 1
        if nxt >= len(pages):
            return None
        run = self.lesson_runner.runs.get(f"{pl.lesson_id}:p{nxt}")
        if run is not None and run.mode != "discarded":
            return run
        return self.lesson_runner._start_page_run(nxt, parked=True)

    def _write_partial_checkpoint(self) -> None:
        """A lesson cut after at least one step stays resumable."""
        pl = self._partial_checkpoint()
        if pl is not None:
            self.state.paused_lesson = pl

    def _live_checkpoint(self) -> PausedLesson | None:
        """What a reload should resume while a lesson is STILL playing. Persisted with the
        board document only; the live job's own state is not paused."""
        st = self.state
        run = self.lesson_runner.active
        if (st.page is None or run is None or run.kind not in ("lesson", "resume")
                or not st.active_turn_id or self.turn_saved(st.active_turn_id)
                or run.heard_upto < 0):
            return None
        return self._partial_checkpoint()

    def _partial_checkpoint(self) -> PausedLesson | None:
        st = self.state
        page = st.page
        if page is None:
            return None
        run = self.lesson_runner.active
        plan = self.lesson_runner.chapter_plan
        return PausedLesson(
            board_id=st.board_id,
            page_id=page.page_id,
            lesson_question=page.lesson_question,
            turn_plan=page.turn_plan,
            solver_projection=page.solver_projection,
            diagram=page.diagram,
            figure_drawn=page.figure_drawn,
            lesson_turn_id=page.turn_id or st.active_turn_id or "",
            last_acked_step_index=st.last_acked_step_index,
            lesson_completed=False,
            heard_steps_text=[s.spoken_text for s in self._heard_steps()],
            lesson_id=st.lesson_id,
            page_index=st.page_index,
            run_id=run.run_id if run is not None else None,
            resume_cursor=(run.heard_upto + 1) if run is not None else 0,
            # A chapter checkpoint carries the plan (page titles survive a reload) and
            # which pages are done, so a resume knows whether a page follows.
            completed_page_ids=[f"{st.lesson_id}_p{i}" for i in range(st.page_index)],
            lesson_plan=plan.model_dump(by_alias=True) if plan is not None else None,
        )

    async def _persist_turn(self, *, status: Literal["complete", "partial"],
                            paused_note: str | None = None) -> bool:
        """Persist the turn; with FEATURE_MEMORY its marks/KPIs become a turn_metrics row."""
        marks = self._turn_marks if settings.FEATURE_MEMORY else None
        kpis = self._turn_kpis() if settings.FEATURE_MEMORY else None
        # One DB writer at a time: the mid-turn board flush and the turn row must not
        # interleave their transactions. Never nested with the flush, so no deadlock.
        async with self._flush_lock:
            return await persist_turn_from_state(self.state, status=status, paused_note=paused_note,
                                                 marks=marks, kpis=kpis)

    async def _complete_turn(self, page_only: bool = False) -> None:
        st = self.state
        turn_id = st.active_turn_id
        if not turn_id or self.turn_saved(turn_id):
            return
        self._close_history(st.pending_request, self._heard_steps())
        kind = st.active_turn_kind
        status = "partial" if self._stream is not None and self._stream.failed else "complete"
        if status == "complete" and not page_only and st.page is not None and kind in ("lesson", "resume"):
            st.page.lesson_completed = True
        if status == "complete" and not page_only and kind == "resume":
            st.paused_lesson = None                      # resumed lesson reached its end
        elif status == "partial":
            if kind == "lesson" and st.steps_sent:
                self._write_partial_checkpoint()
            # kind lesson with 0 steps: no checkpoint (the fallback line asks again)
            # kind resume: keep the existing checkpoint unchanged; doubt: untouched
        try:
            await self._persist_turn(status=status)
        except Exception as e:
            logger.warning(f"persist failed: {e}")
        self._mark("turn_end")
        log_event("turn_kpis", turn_id=turn_id, **self._turn_kpis())
        self.mark_turn_saved(turn_id)
        await send_event(TurnEnded(generation=st.generation, turn_id=turn_id, status=status,
                                   visual_status=st.page.visual_status if st.page else "text_only",
                                   page_only=page_only))
        self._spawn_summary()
        await self._persist_board_state()

    def _spawn_summary(self) -> None:
        """Page/turn summary: spawned off the speaking path, never awaited here."""
        if not settings.FEATURE_MEMORY:
            return
        page = self.state.page
        if page is None:
            return
        self._spawn(self._summarize_job(
            lesson_id=self.state.lesson_id,
            page_id=page.page_id,
            title=page.lesson_question or page.page_id,
            rows=self.ledger.rows_for_prompt(page.page_id),
            heard_text=" ".join(s.spoken_text for s in self._heard_steps()),
            doubts="\n".join(self._page_doubts),
            previous=self.memory.rolling_summary,
        ))

    async def _summarize_job(self, *, lesson_id: str | None, page_id: str, title: str,
                             rows: list[dict[str, str]], heard_text: str, doubts: str,
                             previous: str) -> None:
        try:
            out = await summarize(page_title=title, rows=rows, heard_text=heard_text,
                                  doubts=doubts, previous=previous, gw=self.gateway)
        except asyncio.CancelledError:
            raise                              # shutdown: dropped with the task
        except Exception as e:
            log_event("summary_failed", error=str(e))
            return
        if out is not None:
            self.memory.apply_summary(lesson_id, page_id, out)
            await self._persist_board_state()

    # ------------------------------------------------------------------ board persistence
    async def _persist_board_state(self) -> None:
        """A write point (turn end, park, shutdown, summary): mark then flush."""
        self._mark_board_dirty()
        await self._flush_board_state()

    def _mark_board_dirty(self) -> None:
        self._board_dirty = True
        self._dirty_seq += 1

    def _schedule_board_flush(self) -> None:
        """Heard progress is a write point too, throttled to BOARD_FLUSH_THROTTLE_MS.

        Writing board_state only at turn end / park / shutdown would leave a refresh in the
        middle of a 16-step lesson with state NULL, no turns and no pages: the new job would
        greet a blank board while the old job's flush seconds later is refused (lease lost)."""
        if not settings.FEATURE_MEMORY or self.lease_lost:
            return
        self._mark_board_dirty()
        if self._flush_task is not None and not self._flush_task.done():
            return                                   # the pending flush will carry this state
        delay = max(0.0, self._last_flush + settings.BOARD_FLUSH_THROTTLE_MS / 1000.0 - monotonic())
        self._flush_task = self._spawn(self._delayed_flush(delay))

    async def _delayed_flush(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._flush_board_state()

    async def _flush_board_state(self) -> None:
        """Save the document and the ledger pages; a failure retries on the next write point.

        Serialized: a background summary flush and a turn-end flush must not interleave
        (the loser could clear `_board_dirty` over the winner's fresher state).
        """
        async with self._flush_lock:
            if not settings.FEATURE_MEMORY or not self._board_dirty or self.lease_lost:
                return
            st = self.state
            page = st.page
            self._last_flush = monotonic()
            seq = self._dirty_seq          # the state this write captures
            try:
                paused = st.paused_lesson or self._live_checkpoint()
                doc = BoardStateDoc(
                    board_id=st.board_id,
                    memory=self.memory.to_doc(),
                    paused_lesson=(paused.model_dump(mode="json")
                                   if paused is not None else None),
                    lesson_id=st.lesson_id,
                    current_page_id=page.page_id if page is not None else None,
                    last_generation=st.generation,
                )
                diagrams = {page.page_id: page.diagram} if page is not None else {}
                commits: dict[str, Any] = {}
                run = self.lesson_runner.active
                if (run is not None
                        and getattr(run.ctx, "page_commit", None) is not None):
                    commits[run.page_id] = run.ctx.page_commit
                pages = self.ledger.to_pages(board_id=st.board_id, lesson_id=st.lesson_id,
                                             page_index=st.page_index, diagrams=diagrams,
                                             commits=commits)
                async with dbmod.async_session() as session:
                    await board_state_mod.save_board_state(session, doc, self.lease_owner)
                    if pages:
                        await board_state_mod.save_pages(session, st.board_id, st.lesson_id or "",
                                                         pages, self.lease_owner)
                # Only a write that captured the LATEST mark may clear the flag: a mark made while
                # this save awaited the DB (a turn end behind the lock) must still be written.
                if self._dirty_seq == seq:
                    self._board_dirty = False
            except board_state_mod.LeaseLost:
                await self._on_lease_lost()
            except Exception as e:
                log_event("board_state_persist_failed", error=str(e))

    def _turn_kpis(self) -> dict[str, Any]:
        """Per-turn ratios over steps with a completed report."""
        run = self.lesson_runner.active
        counts = run.completed_by if run is not None else {}
        completed = sum(counts.values())

        def ratio(name: str) -> float:
            return round(counts.get(name, 0) / completed, 3) if completed else 0.0

        return {
            "steps": len(self.state.steps_sent),
            "heardSteps": self.state.heard_step_index + 1,
            "stallRatio": ratio("stall"),
            "flushRatio": ratio("flush"),
            "wordMatchRatio": ratio("words"),
            "earlyOps": run.early_ops if run is not None else 0,
            "audioMode": self.state.audio_mode,
        }

    async def _return_from_interrupt(self) -> ConvState:
        """Backchannel / no-op affirmation: go back to where we were, repairing speech."""
        prev = self._prev_state
        if prev in TURN_STATES and self._turn_speech_started:
            prev = ConvState.AGENT_SPEAKING
        if self.lesson_runner.advance_pending:
            # The page's speech ended and the gap was cancelled by this barge-in. The
            # page turn already completed (turn_ended{page_only}); resume the gap and return to
            # GRAPH_RUNNING (rule 29's target) instead of falling through to IDLE.
            self.lesson_runner.spawn_advance()
            return ConvState.GRAPH_RUNNING
        if prev == ConvState.AGENT_SPEAKING or (prev in TURN_STATES and self._stream is not None):
            if self._speech_finished:
                return await self._finish_remembered_speech()
            if self._speech_cut and self._stream is not None:
                await self.replay_from_step(self._replay_index())
        elif (prev == ConvState.IDLE and self.can_continue()
              and CONTINUE_CUE_RE.search((self.state.interrupt_transcript or "").lower())):
            log_event("voice_continue_resumes")
            return await self.resume()
        return prev

    async def _finish_remembered_speech(self) -> ConvState:
        """The turn's speech ended inside an interrupt window (rule 24) and the interrupt turned
        out to be a no-op. Apply what SPEECH_ENDED would have done: rule 29 at a chapter page
        boundary (page-only end + gap), else rule 3. A plain _complete_turn() -> IDLE would end
        a chapter in the middle (lesson_completed, turn_ended{pageOnly:false}) and strand every
        later "continue"."""
        if self.state.has_next_page and self.lesson_runner.chapter_plan is not None:
            await self._complete_turn(page_only=True)
            self.lesson_runner.spawn_advance()
            return ConvState.GRAPH_RUNNING
        await self._complete_turn()
        return ConvState.IDLE

    async def _after_redirect(self) -> ConvState:
        prev = self._prev_state
        if self._stream is None or not self.state.active_turn_id:
            return ConvState.IDLE if prev in TURN_STATES else prev
        if self._speech_finished:
            return await self._finish_remembered_speech()
        await self.replay_from_step(self._replay_index())
        return ConvState.AGENT_SPEAKING if self._turn_speech_started else prev

    # ------------------------------------------------------------------ holds
    async def _add_hold(self, reason: str) -> None:
        st = self.state
        if reason in st.holds:
            return
        first = not st.holds
        st.holds.add(reason)
        st.marker_armed = bool(st.holds)
        self._unpaused.clear()               # consumers wait before speaking their next step
        if first and st.conv_state == ConvState.AGENT_SPEAKING and self._stream is not None:
            self._interrupt_speech(f"hold:{reason}")
            self._paused_mid_turn = True

    async def _remove_hold(self, reason: str) -> None:
        st = self.state
        if reason not in st.holds:
            return
        st.holds.discard(reason)
        st.marker_armed = bool(st.holds)
        if not st.holds:
            self._unpaused.set()
            if self._paused_mid_turn:
                self._paused_mid_turn = False
                await self.replay_from_step(self._replay_index())

    # ------------------------------------------------------------------ doubt amendment
    async def amend_question(self, extra: str) -> None:
        """Rule 30: a doubt before the first heard step rewrites the lesson question."""
        page = self.state.page
        q = f"{page.lesson_question if page is not None else ''}\nStudent added: {extra.strip()}"
        old_lesson = self.state.lesson_id
        await self.supersede("amend")
        # The amended lesson replaces the old one; its chapter plan and runs
        # (prefetched pages included) must not keep generating in the dark.
        if old_lesson:
            self.lesson_runner.discard_lesson(old_lesson)
        self.state.paused_lesson = None
        await self.run_turn(TurnRequest(kind="lesson", generation=self.state.generation,
                                        turn_id=str(uuid4()), question=q,
                                        student_text=extra.strip()))

    # ------------------------------------------------------------------ doubts
    async def begin_doubt(self, source: str, typed_text: str = "", marks: list[Any] | None = None) -> None:
        st = self.state
        page = st.page
        # Park the lesson/resume run first: its producer keeps generating and its stream
        # is what a later resume activates. Flag off = cancel inside supersede.
        parked: PageRun | None = None
        if settings.FEATURE_PARKED_RESUME:
            active = self.lesson_runner.active
            if active is not None and active.kind in ("lesson", "resume"):
                parked = self.lesson_runner.park_active()
        else:
            # Flag off: the lesson cannot be activated again; its prefetched pages would keep
            # generating in the dark. The checkpoint still allows a fallback resume.
            self.lesson_runner.discard_prefetch_runs()
        # Anti-overwrite rule: snapshot only if nothing is paused yet or the
        # ACTIVE TURN is the lesson/resume. A doubt turn never writes the checkpoint.
        if page is not None and (st.paused_lesson is None or st.active_turn_kind in ("lesson", "resume")):
            heard_steps_text = [s.spoken_text for s in self._heard_steps()]
            run = parked or self.lesson_runner.active
            cursor = (run.heard_upto + 1) if run is not None else st.last_acked_step_index + 1
            run_id = run.run_id if run is not None else None
            check_page_index = st.page_index
            # A doubt during the page gap belongs to page i+1, whose prefetched
            # run stays parked; resume activates THAT page from its start.
            if self.lesson_runner.advance_pending and st.lesson_id:
                pending_index = self.lesson_runner.page_index
                pending = self.lesson_runner.runs.get(f"{st.lesson_id}:p{pending_index}")
                if pending is not None and pending.mode == "parked":
                    run = pending
                    run_id = pending.run_id
                    cursor = 0
                    check_page_index = pending_index
            if st.paused_lesson is not None and st.active_turn_kind == "resume":
                # A resume refreshes progress only; question, plan and figure stay the
                # lesson's.
                st.paused_lesson.last_acked_step_index = st.last_acked_step_index
                st.paused_lesson.heard_steps_text = heard_steps_text
                st.paused_lesson.run_id = run_id
                st.paused_lesson.resume_cursor = cursor
            else:
                plan = self.lesson_runner.chapter_plan
                st.paused_lesson = PausedLesson(
                    board_id=st.board_id,
                    page_id=(f"{st.lesson_id}_p{check_page_index}" if plan is not None
                             and st.lesson_id else page.page_id),
                    lesson_question=page.lesson_question,
                    turn_plan=page.turn_plan,
                    solver_projection=page.solver_projection,
                    diagram=page.diagram,
                    figure_drawn=page.figure_drawn,
                    lesson_turn_id=page.turn_id or st.active_turn_id or "",
                    last_acked_step_index=st.last_acked_step_index,
                    # Use the page's flag, not conv_state: a voice doubt arrives in
                    # INTERRUPT_CLASSIFYING, so a state check would report every lesson
                    # unfinished and always offer "Continue lesson".
                    lesson_completed=page.lesson_completed,
                    heard_steps_text=heard_steps_text,
                    lesson_id=st.lesson_id,
                    page_index=check_page_index,
                    run_id=run_id,
                    resume_cursor=cursor,
                    completed_page_ids=[f"{st.lesson_id}_p{i}" for i in range(check_page_index)],
                    lesson_plan=plan.model_dump(by_alias=True) if plan is not None else None,
                )

        interrupted_handle = self.speech
        await self.supersede("doubt")
        await self._persist_board_state()          # park write point
        st.holds = set()                     # a doubt clears every hold
        st.marker_armed = False
        self._unpaused.set()

        doubt_text = (typed_text or st.interrupt_transcript or "").strip()
        marks_list = list(marks or self.active_marks or [])
        if not marks_list and self.active_pointer_target:
            t = self.active_pointer_target
            is_work = t.get("kind") == "work"
            marks_list = [DoubtMark(gesture="point", target_kind="work" if is_work else "diagram",
                                    row_id=t.get("id") if is_work else None,
                                    entity_id=None if is_work else t.get("id"),
                                    text=t.get("text") or t.get("id"))]
        marks_list = [m if isinstance(m, DoubtMark) else DoubtMark.model_validate(m) for m in marks_list]
        self.active_pointer_target = None
        self.active_marks = []

        lesson_q = st.paused_lesson.lesson_question if st.paused_lesson else None
        # The prompt is built at settle time from everything that accumulates meanwhile.
        self._doubt_marks = marks_list
        self._doubt_typed = (typed_text or "").strip()
        self._first_doubt_input = self._last_doubt_input = monotonic()
        self._spawn(self._settle_loop(st.generation, interrupted_handle))

    def _rearm_settle(self) -> None:
        self._last_doubt_input = monotonic()

    async def _wait_handle_done(self, handle: Any, timeout: float) -> None:
        """Wait until the interrupted audio stops OR the timeout, whichever is first."""
        if handle is not None and hasattr(handle, "add_done_callback") and hasattr(handle, "done"):
            try:
                if not handle.done():
                    fut = asyncio.get_running_loop().create_future()
                    handle.add_done_callback(lambda _h: fut.done() or fut.set_result(None))
                    await asyncio.wait_for(fut, timeout)
            except (asyncio.TimeoutError, Exception):
                pass
        else:
            await asyncio.sleep(min(timeout, 0.05))

    async def _settle_loop(self, generation: int, handle: Any = None) -> None:
        """Wait for a quiet period after the last doubt input, bounded by SETTLE_CAP_MS."""
        await self._wait_handle_done(handle, timeout=settings.DOUBT_SETTLE_TIMEOUT_MS / 1000.0)
        while True:
            now = monotonic()
            quiet_left = self._last_doubt_input + settings.DOUBT_SETTLE_TIMEOUT_MS / 1000.0 - now
            cap_left = self._first_doubt_input + settings.SETTLE_CAP_MS / 1000.0 - now
            if quiet_left <= 0 or cap_left <= 0:
                break
            await asyncio.sleep(min(quiet_left, cap_left, 0.1))
            if generation != self.state.generation:
                return
        self.fire(ConvEvent.DOUBT_SETTLED, generation=generation)

    async def _handle_doubt_settled(self) -> ConvState:
        st = self.state
        text = (self._doubt_typed + " " + st.interrupt_transcript).strip()
        marks = list(self._doubt_marks)
        st.interrupt_transcript = ""
        self._doubt_marks = []
        self._doubt_typed = ""
        # Retraction: a short "never mind"/"got it" with no marks resumes or idles.
        if not marks and len(text.split()) <= 6 and RETRACTION_RE.search(text.lower()):
            st.doubt_awaiting_resolution = False
            log_event("doubt_retracted", text=text)
            if self.can_continue():
                return await self.resume()
            return ConvState.IDLE

        lesson_q = st.paused_lesson.lesson_question if st.paused_lesson else None
        if text:
            self._page_doubts.append(text)               # summarizer doubt input
        req = TurnRequest(
            kind="doubt",
            generation=st.generation,
            turn_id=str(uuid4()),
            question=lesson_q or text,
            student_text=text,
            doubt_prompt=build_marked_doubt_prompt(marks=marks, doubt=text, lesson_question=lesson_q),
            marks=marks,
        )
        board_diagram = st.page.diagram if st.page and st.page.diagram else (
            st.paused_lesson.diagram if st.paused_lesson else None)
        if board_diagram:
            glossary = board_diagram.label_glossary or {}
            lines = []
            for a in board_diagram.anchors:
                fact = glossary.get(a.id)
                extra = f" ({fact.title} = {fact.value})" if fact and fact.value else ""
                lines.append(f"{a.id}: {', '.join(a.labels) or a.id}{extra}")
            anchors_str = "\n".join(lines)
        else:
            anchors_str = "none"
        marks_ref = "; ".join(f"{m.gesture} {m.target_kind} {m.row_id or m.entity_id or ''} {m.text or ''}".strip()
                              for m in req.marks) or "none"
        doubt_only = st.interrupt_transcript or ""
        for m in req.marks:
            doubt_only = doubt_only or (m.text or "")
        try:
            requires_new_figure = await classify_figure_need(
                on_board_entities=anchors_str, marked_reference=marks_ref,
                doubt_text=(req.doubt_prompt or doubt_only), gateway=self.gateway)
        except Exception as e:
            log_event("figure_need_failed", error=str(e))
            requires_new_figure = False
        if req.generation != st.generation:              # superseded while classifying
            return st.conv_state
        req.requires_new_figure = bool(requires_new_figure)
        if req.requires_new_figure:
            self._doubt_fig_counter += 1
            req.namespace = f"d{self._doubt_fig_counter}_"
        await self.run_turn(req)
        return ConvState.TASK_CORRECTING

    # ------------------------------------------------------------------ resume / redirect / replay / end
    async def resume(self) -> ConvState:
        st = self.state
        if not self.can_continue():
            st.doubt_awaiting_resolution = False
            log_event("resume_nothing_to_continue")
            return ConvState.IDLE
        pl = st.paused_lesson
        assert pl is not None
        # A reload/rehydrate loses the runner's chapter; rebuild it from the checkpoint
        # so page titles, prefetch and the next-page decision survive the job boundary.
        if self.lesson_runner.chapter_plan is None and pl.lesson_plan and pl.lesson_id:
            try:
                from app.contracts.lesson import LessonPlan
                self.lesson_runner.chapter_plan = LessonPlan.model_validate(pl.lesson_plan)
                self.lesson_runner.lesson_id = pl.lesson_id
                self.lesson_runner.page_index = int(pl.page_index or 0)
                log_event("chapter_plan_restored", lesson_id=pl.lesson_id,
                          page_index=pl.page_index)
            except Exception as e:
                log_event("chapter_plan_restore_failed", error=f"{type(e).__name__}: {e}")
        run = self.lesson_runner.run_for(pl.run_id) if settings.FEATURE_PARKED_RESUME else None
        activate = False
        if run is not None and run.mode != "discarded":
            cursor = self.lesson_runner.resume_cursor(run)
            if run.production == "done" and cursor >= len(run.stream.items):
                nxt = self._chapter_next_page_run(pl)
                if nxt is not None:
                    # This page is fully taught: the chapter continues on the next page, whose
                    # prefetched run was kept parked.
                    log_event("chapter_resume_next_page", page_index=nxt.page_index)
                    run = nxt
                    pl.run_id = nxt.run_id
                    pl.page_index = nxt.page_index
                    pl.page_id = nxt.page_id
                    pl.resume_cursor = 0
                    self.lesson_runner.page_index = nxt.page_index
                    activate = True
                else:
                    # Nothing left to teach anywhere: the lesson is complete.
                    pl.lesson_completed = True
                    st.doubt_awaiting_resolution = False
                    log_event("resume_nothing_left", run_id=run.run_id)
                    return ConvState.IDLE
            else:
                # failed with nothing left -> regenerate; otherwise replay the remaining steps
                activate = not (run.production == "failed" and cursor >= len(run.stream.items))
        if not activate and self._chapter_page_not_started(pl):
            # A chapter checkpoint at a page that was never taught (chapter stop, doubt in
            # the gap after its parked run was lost, reload). The generic resume regeneration
            # would teach it without its page plan (no blocks, resume.v3), stamp the stale page
            # index and end the chapter after it; start the page's own run instead.
            fresh = self.lesson_runner._start_page_run(int(pl.page_index or 0), parked=True)
            if fresh is not None:
                log_event("chapter_resume_page_restart", page_index=pl.page_index)
                run = fresh
                pl.run_id = fresh.run_id
                self.lesson_runner.page_index = int(pl.page_index or 0)
                activate = True
        await self.supersede("resume")
        if st.page is None or st.page.page_id != pl.page_id:
            # The ledger is the server's copy of the board: acked ops of every sub-page,
            # in order. The DB is only a fallback when this job never saw the page.
            snap = self.ledger.snapshot_page(pl.page_id)
            if not any(sp.ops for sp in snap.sub_pages):
                try:
                    from app.persistence.db import async_session
                    async with async_session() as session:
                        db_ops = await get_stored_ops_for_page(session, pl.page_id)
                    if db_ops:
                        snap = SnapshotPage(
                            page_id=pl.page_id,
                            sub_pages=[SnapshotSubPage(sub_id=pl.page_id,
                                                       ops=[BoardOp.model_validate(o) for o in db_ops])],
                        )
                except Exception as e:
                    log_event("page_ops_db_unavailable", error=str(e))
            snap.diagram = pl.diagram
            await send_event(BoardSnapshot(generation=st.generation, current=snap,
                                           can_continue_lesson=self.can_continue()))
            st.page = PageRecord(board_id=st.board_id, page_id=pl.page_id, lesson_question=pl.lesson_question,
                                 turn_plan=pl.turn_plan, solver_projection=pl.solver_projection, diagram=pl.diagram,
                                 visual_status="validated" if pl.diagram else "text_only",
                                 figure_drawn=pl.figure_drawn, turn_kind="resume", turn_id=pl.lesson_turn_id,
                                 continues_board=True)
        st.doubt_awaiting_resolution = False
        # No run (or a discarded/failed-with-nothing-left one): fallback regeneration builds a
        # new resume run (new_run names it "{lesson_id}:p{page_index}:r{n}") using resume.v3.
        await self.run_turn(TurnRequest(kind="resume", generation=st.generation, turn_id=str(uuid4()),
                                        question=pl.lesson_question, lesson_id=pl.lesson_id,
                                        page_index=pl.page_index,
                                        run_id=run.run_id if activate and run is not None else None))
        return ConvState.RESUMING

    def _chapter_page_not_started(self, pl: PausedLesson) -> bool:
        """The checkpoint names a chapter page from its start and this job's runner holds that
        chapter (restored above after a reload)."""
        plan = self.lesson_runner.chapter_plan
        return (plan is not None and bool(pl.lesson_plan) and pl.lesson_id is not None
                and pl.lesson_id == self.lesson_runner.lesson_id
                and int(pl.resume_cursor or 0) == 0
                and 0 <= int(pl.page_index or 0) < len(plan.pages))

    async def off_topic(self) -> None:
        """Naive redirect: static template, no LLM call, no generation bump."""
        self._interrupt_speech("off_topic")
        st = self.state
        topic = st.lesson_topic or "this question"
        text = REDIRECT_TEMPLATES[st.redirect_cursor % len(REDIRECT_TEMPLATES)].format(topic=topic)
        st.redirect_cursor += 1
        gen = st.generation
        await self.speak_aside(text, "redirect",
                               on_done=lambda: self.fire(ConvEvent.REDIRECT_DONE, generation=gen))

    async def replay_from_step(self, step_index: int) -> None:
        """Re-speak from a step without a generation bump. The producer keeps generating."""
        st = self.state
        run = self.lesson_runner.active
        if not st.active_turn_id or run is None:
            return
        self._interrupt_speech("replay")
        await send_event(ReplayFromStep(generation=st.generation, turn_id=st.active_turn_id, step_index=step_index))
        self._start_speech(run, step_index)

    async def end_session(self, say_goodbye: bool = True) -> None:
        await self.supersede("end")
        # A session that ended must not keep prefetched page producers alive.
        self.lesson_runner.discard_prefetch_runs()
        self.state.conv_state = ConvState.TASK_CANCELLED
        self._spawn(self._say_goodbye_and_disconnect(say_goodbye))

    async def _say_goodbye_and_disconnect(self, say_goodbye: bool = True) -> None:
        if self.session and say_goodbye:
            gen = self.state.generation
            await send_event(Aside(generation=gen, phase="start", kind="goodbye"))
            try:
                handle = self.session.say(GOODBYE_LINE, allow_interruptions=False, add_to_chat_ctx=False)
                if hasattr(handle, "wait_for_playout"):
                    await asyncio.wait_for(handle.wait_for_playout(), 8.0)
            except Exception as e:
                log_event("goodbye_failed", error=str(e))
            await send_event(Aside(generation=gen, phase="end", kind="goodbye"))
        room = self.room
        if room is None:
            from app.transport import get_active_room
            room = get_active_room()
        if room is not None and hasattr(room, "disconnect"):
            try:
                await room.disconnect()
            except Exception as e:
                log_event("disconnect_failed", error=str(e))

    async def shutdown(self, *_: Any) -> None:
        """Job shutdown hook: keep a resumable checkpoint, stop every task, flush, release.

        A chapter in flight must leave a checkpoint BEFORE its producers and
        gap task are cancelled; every task this job spawned (outline, gap, prefetched page
        producers, settle loop, caption pacer, summarizer) must be stopped; the lease goes last,
        after the final flush.
        """
        st = self.state
        try:
            self._write_shutdown_checkpoint()
        except Exception as e:
            log_event("shutdown_checkpoint_failed", error=f"{type(e).__name__}: {e}")
        if st.active_turn_id and not self.turn_saved(st.active_turn_id):
            try:
                await self._persist_turn(status="partial")
            except Exception as e:
                logger.warning(f"shutdown persist failed: {e}")
            self.mark_turn_saved(st.active_turn_id)
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.lesson_runner.cancel_gap()              # the pending page gap
        self.lesson_runner.discard_prefetch_runs()   # parked prefetch producers
        if (self._outline_task is not None and not self._outline_task.done()
                and self._outline_task is not asyncio.current_task()):
            self._outline_task.cancel()
        for task in list(self._tasks):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await self._persist_board_state()            # shutdown write point (final flush)
        await self._release_lease()                  # only after the final flush

    def _write_shutdown_checkpoint(self) -> None:
        """Leave a resumable chapter checkpoint when the job stops mid-chapter."""
        st = self.state
        runner = self.lesson_runner
        if runner.chapter_plan is None or st.paused_lesson is not None or self.lease_lost:
            return
        if runner.advance_pending:
            # Mid gap: the next page is the resume target (its parked run may still be there).
            pending_index = runner.page_index
            run = runner.runs.get(f"{st.lesson_id}:p{pending_index}") if st.lesson_id else None
            self._write_chapter_checkpoint(
                pending_index, run.run_id if run is not None and run.mode != "discarded" else None)
            log_event("shutdown_checkpoint", page_index=pending_index, kind="gap")
            return
        run = runner.active
        if run is not None and run.kind in ("lesson", "resume"):
            if st.published_upto >= 0:
                self._write_partial_checkpoint()
                log_event("shutdown_checkpoint", page_index=st.page_index, kind="partial")
            else:
                self._write_chapter_checkpoint(int(run.page_index or st.page_index or 0), run.run_id)
                log_event("shutdown_checkpoint", page_index=st.page_index, kind="page")

    # ------------------------------------------------------------------ rehydrate
    def rehydrate(self, doc: BoardStateDoc, pages: list[dict[str, Any]] | None = None) -> None:
        """Restore board id, memory, checkpoint and ledger pages for a reloaded session."""
        st = self.state
        st.board_id = doc.board_id
        self.memory = MemoryService(doc.memory)
        self.memory.lesson_id = doc.lesson_id
        st.lesson_id = doc.lesson_id
        st.paused_lesson = (PausedLesson.model_validate(doc.paused_lesson)
                            if doc.paused_lesson else None)
        st.generation = max(st.generation, doc.last_generation)
        st.active_turn_id = None
        st.active_turn_kind = None
        st.steps_sent = []
        if pages:
            self.ledger.restore_pages(pages)
        pl = st.paused_lesson
        diagram = pl.diagram if (pl is not None and pl.diagram is not None) else None
        if diagram is None and doc.current_page_id and pages:
            # A completed lesson clears paused_lesson, so its figure only survives in
            # the persisted page row: restore it from there.
            entry = next((p for p in pages
                          if p.get("page_id") == doc.current_page_id and p.get("diagram")), None)
            if entry is not None:
                d = entry["diagram"]
                if isinstance(d, VerifiedDiagram):
                    diagram = d
                elif isinstance(d, dict):
                    try:
                        diagram = VerifiedDiagram.model_validate(d)
                    except Exception as e:
                        log_event("board_state_diagram_invalid",
                                  page_id=doc.current_page_id, error=str(e))
        if doc.current_page_id:
            # A restored page makes voice doubts classify as doubts (lesson_on_board).
            st.page = PageRecord(
                board_id=doc.board_id,
                page_id=doc.current_page_id,
                lesson_question=pl.lesson_question if pl else "",
                turn_plan=pl.turn_plan if pl else None,
                solver_projection=pl.solver_projection if pl else None,
                diagram=diagram,
                visual_status="validated" if diagram is not None else "text_only",
                figure_drawn=diagram is not None,
                turn_kind="lesson",
                turn_id=pl.lesson_turn_id if pl else None,
                continues_board=False,
            )
        log_event("board_rehydrated_state", board_id=doc.board_id,
                  page_id=doc.current_page_id, lesson_id=doc.lesson_id)

    # ------------------------------------------------------------------ board lease
    async def start_lease(self) -> bool:
        """Acquire the board lease and renew it in the background; called once at join."""
        try:
            async with dbmod.async_session() as session:
                acquired = await board_state_mod.acquire_lease(
                    session, self.state.board_id, self.lease_owner)
        except Exception as e:
            log_event("lease_acquire_failed", board_id=self.state.board_id,
                      owner=self.lease_owner, error=str(e))
            return False
        if acquired and not self.lease_lost and (self._lease_task is None or self._lease_task.done()):
            self._lease_task = self._spawn(self._renew_lease_loop())
        return acquired

    async def _renew_lease_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.LEASE_RENEW_S)
            try:
                async with dbmod.async_session() as session:
                    ok = await board_state_mod.renew_lease(
                        session, self.state.board_id, self.lease_owner)
            except Exception as e:
                log_event("lease_renew_failed", error=str(e))
                continue
            if not ok:
                await self._on_lease_lost()
                return

    async def _on_lease_lost(self) -> None:
        """A newer tab took the board: tell the student, stop writing, no goodbye."""
        if self.lease_lost:
            return
        self.lease_lost = True
        log_event("lease_lost", board_id=self.state.board_id, owner=self.lease_owner)
        try:
            await send_event(SessionEnded(generation=self.state.generation,
                                          reason="opened_elsewhere"))
        except Exception as e:
            log_event("session_ended_send_failed", error=str(e))
        await self.end_session(say_goodbye=False)

    async def _release_lease(self) -> None:
        if self._lease_task is not None and not self._lease_task.done():
            self._lease_task.cancel()
        self._lease_task = None
        if self.lease_lost:
            return
        try:
            async with dbmod.async_session() as session:
                await board_state_mod.release_lease(
                    session, self.state.board_id, self.lease_owner)
        except Exception as e:
            log_event("lease_release_failed", error=str(e))

    def set_speed(self, speed: float) -> bool:
        """Store the speed and apply it to the session's TTS (next utterance onward)."""
        from app.voice.providers import apply_tts_speed
        self.state.tts_speed = speed
        return apply_tts_speed(getattr(self.session, "tts", None), speed)

    # ------------------------------------------------------------------ client reports
    def on_step_ack(self, ack: Any) -> None:
        st = self.state
        if ack.generation != st.generation or ack.turn_id != st.active_turn_id:
            log_event("step_ack_stale_ignored", ack_gen=ack.generation, cur_gen=st.generation)
            return
        st.last_acked_step_index = max(st.last_acked_step_index, ack.step_index)
        st.heard_step_index = max(st.heard_step_index, ack.step_index)
        run = self.lesson_runner.active
        if run is not None:
            run.heard_upto = max(run.heard_upto, run.publish_from + ack.step_index)
        st.acked_op_ids.update(ack.drawn_op_ids)
        self.ledger.on_acked(ack.drawn_op_ids)
        self._schedule_board_flush()

    def on_step_progress(self, p: Any) -> None:
        """Cumulative client progress: the heard cursor is the authoritative one."""
        st = self.state
        if p.generation != st.generation or p.turn_id != st.active_turn_id:
            log_event("step_progress_stale_ignored", ack_gen=p.generation, cur_gen=st.generation)
            return
        self.lesson_runner.on_step_progress(p)
        if p.event in ("completed", "final"):
            st.last_acked_step_index = max(st.last_acked_step_index, p.step_index)
            self._schedule_board_flush()
        st.acked_op_ids.update(p.drawn_op_ids)

    def on_board_report(self, rpt: Any) -> None:
        from app.contracts.agent_state import RowMirror
        st = self.state
        root = st.page.page_id if st.page is not None else None
        expected = self.ledger.current_sub(root) if root is not None else None
        if expected is not None and rpt.page_id != expected:
            # A late report from a previous sub-page must not overwrite this page's rows.
            log_event("board_report_foreign_page_ignored", page_id=rpt.page_id, expected=expected)
            return
        rows = [r.model_dump() for r in rpt.rows]
        self.state.rows = RowMirror(page_id=rpt.page_id, rows=rows, rows_remaining=rpt.rows_remaining)
        # The run that is teaching owns the tracker the prompts read.
        run = self.lesson_runner.active
        tracker = run.ctx.row_tracker if run is not None else self.row_tracker
        tracker.record_board_report(rpt.page_id, rows, rpt.rows_remaining)

    def on_page_turned(self, r: Any) -> None:
        """Client page turn (overflow or applied PAGE_BREAK): keep the ledger in step."""
        self.ledger.on_page_turned(r)

    # ------------------------------------------------------------------ resync
    async def _send_board_snapshot(self) -> None:
        st = self.state
        root = st.page.page_id if st.page is not None else None
        current = self.ledger.snapshot_page(root) if root else SnapshotPage(page_id="")
        if st.page is not None:
            current.diagram = st.page.diagram
        active_turn = None
        pending: list[Step] = []
        if st.active_turn_id and st.active_turn_kind:
            active_turn = TurnStarted(generation=st.generation, turn_id=st.active_turn_id,
                                      kind=st.active_turn_kind, page_id=current.page_id,
                                      new_page=False)
            pending = list(st.steps_sent[st.heard_step_index + 1:])
        await send_event(BoardSnapshot(generation=st.generation, current=current,
                                       active_turn=active_turn, pending_steps=pending,
                                       can_continue_lesson=self.can_continue()))

    async def on_resync_request(self, r: Any) -> None:
        """Resend from the buffer when possible, else send a board snapshot; replay when
        a reconnect happens mid-speech. Scheduled by transport, so it takes the manager lock."""
        async with self._lock:
            from app.outbox import get_outbox
            outbox = get_outbox()
            replayed = r.epoch == outbox.epoch and await outbox.resend_from(r.last_seq + 1)
            if not replayed:
                await self._send_board_snapshot()
            if r.reason == "reconnected" and self.state.conv_state == ConvState.AGENT_SPEAKING:
                await self.replay_from_step(self._replay_index())

    # ------------------------------------------------------------------ classification
    def _schedule_classification(self, skip_settle: bool = False, input_mode: str = "spoken") -> None:
        seq = self._interrupt_seq
        self._mark("user_final")

        async def _classify() -> None:
            # If in IDLE state, give a settle window so multi-word/multi-clause questions
            # have time to gather subsequent speech before classifying as a new_question.
            # Typed input is already complete: no settle window.
            if (not skip_settle
                    and self.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
                    and self._prev_state == ConvState.IDLE):
                settle_s = settings.IDLE_QUESTION_SETTLE_MS / 1000.0
                if settle_s > 0:
                    await asyncio.sleep(settle_s)
                    if seq != self._interrupt_seq:
                        return

            st = self.state
            last_line = ""
            if st.steps_sent:
                # The step after the last heard one is what the teacher is saying now.
                idx = min(st.heard_step_index + 1, len(st.steps_sent) - 1)
                last_line = st.steps_sent[idx].spoken_text
            pointed = ""
            if self.active_marks:
                m = self.active_marks[0]
                pointed = str((m.get("text") if isinstance(m, dict) else getattr(m, "text", "")) or "")
            elif self.active_pointer_target:
                pointed = str(self.active_pointer_target.get("text") or self.active_pointer_target.get("id") or "")
            try:
                label = await classify_interrupt(
                    topic=st.lesson_topic, last_teacher_line=last_line,
                    lesson_on_board=st.page is not None, doubt_pending=st.doubt_awaiting_resolution,
                    lesson_paused=st.paused_lesson is not None,
                    input_mode=input_mode,
                    utterance=st.interrupt_transcript, pointed_target=pointed,
                    prev_state=self._prev_state.value, gateway=self.gateway)
            except Exception as e:
                log_event("interrupt_classifier_failed", error=str(e))
                label = "doubt" if st.page is not None else "new_question"
            event = {
                "backchannel": ConvEvent.CLS_BACKCHANNEL, "affirmation": ConvEvent.CLS_AFFIRMATION,
                "doubt": ConvEvent.CLS_DOUBT, "new_question": ConvEvent.CLS_NEW_QUESTION,
                "end_session": ConvEvent.CLS_END, "off_topic": ConvEvent.CLS_OFF_TOPIC,
            }.get(label, ConvEvent.CLS_DOUBT)
            log_event("interrupt_classified", label=label, prev_state=self._prev_state.value)
            self._mark("classified")
            await self.handle_event(event, interrupt_seq=seq)

        self._spawn(_classify())

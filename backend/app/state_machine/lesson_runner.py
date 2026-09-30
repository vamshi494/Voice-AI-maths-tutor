# app/state_machine/lesson_runner.py
"""LessonRunner, PageRun and StreamPublisher.

A PageRun owns one producer stream and one RunContext. Only the active run may publish:
StreamPublisher stamps every Step with the active turn's id/generation, keeps op ids stable and
sets `is_last` when a finished stream is re-published.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import app.agents.graph as graph_mod
from app.agents.graph_state import RunContext
from app.config import settings
from app.contracts.agent_state import ConvState, PageRecord, TurnRequest
from app.contracts.board_ops import Step
from app.contracts.messages import Block, DiagramCommit, ErrorNotice, PageCommit, Rect, StepEvt
from app.observability import log_event, logger
from app.prompts.registry import CHAPTER_STOP_LINE, PAGE_FILLER_LINES
from app.scene_engine.layout import FIGURE_REGION, WORK_RECT_DIAGRAM
from app.state_machine.turn_stream import TurnStream
from app.tutor.board_rows import BoardRowTracker
from app.transport import send_event


@dataclass
class PageRun:
    run_id: str                    # lesson: f"{lesson_id}:p{page_index}"; doubt: f"d:{turn_id}"
    kind: Literal["lesson", "doubt", "resume"]
    lesson_id: str | None
    page_index: int
    page_id: str
    origin_turn_id: str            # op ids are "{origin_turn_id}:{source_step}:{n}" forever
    stream: TurnStream
    ctx: RunContext
    task: asyncio.Task | None = None
    late_scene: asyncio.Task | None = None
    mode: Literal["active", "parked", "discarded"] = "active"
    production: Literal["running", "done", "failed"] = "running"
    publish_from: int = 0          # source index where the current activation starts
    published_upto: int = -1       # source index
    heard_upto: int = -1           # source index, contiguous heard (words|caption|stall)
    figure_committed: bool = False # for the current activation
    completed_by: dict[str, int] = field(default_factory=dict)   # KPI counters
    early_ops: int = 0
    marked: set[str] = field(default_factory=set)                # marks already logged


class StreamPublisher:
    """The only publisher of StepEvt/DiagramCommit for a run."""

    def __init__(self, mgr: Any) -> None:
        self.mgr = mgr

    async def activate(self, run: PageRun, from_src: int, turn_id: str, generation: int) -> None:
        run.mode = "active"
        run.publish_from = from_src
        run.published_upto = from_src - 1
        run.figure_committed = False
        has_commit = getattr(run.ctx, "page_commit", None) is not None
        if run.ctx.page_record is not None and (has_commit or run.ctx.page_record.diagram is not None):
            await self.commit_figure(run)
        for src in range(from_src, len(run.stream.items)):
            await self._publish(run, src, turn_id, generation)

    async def on_new_item(self, run: PageRun) -> None:
        if run.mode == "active":
            await self._publish(run, len(run.stream.items) - 1,
                                self.mgr.state.active_turn_id, self.mgr.state.generation)

    def park(self, run: PageRun) -> None:
        run.mode = "parked"

    async def commit_figure(self, run: PageRun, reference: bool = False) -> None:
        page = run.ctx.page_record
        if run.figure_committed:
            return
        page_commit = getattr(run.ctx, "page_commit", None)
        if page_commit is None and (page is None or page.diagram is None):
            return
        generation = self.mgr.state.generation
        turn_id = self.mgr.state.active_turn_id or run.origin_turn_id
        page_id = page.page_id if page is not None else run.page_id
        if page_commit is not None:
            # The chapter page pipeline built the multi-block commit (figures, tables,
            # text blocks). Re-stamp the wire envelope; idempotency comes from (pageId,
            # commitId) with a fresh commit id per activation.
            await send_event(page_commit.model_copy(update={
                "generation": generation,
                "turn_id": turn_id,
                "page_id": page_id,
                "commit_id": f"c_{page_id}_{turn_id[:8]}",
                "reference": reference,
            }))
        elif settings.FEATURE_PAGE_COMMIT:
            # A single-figure page commits as a one-block PageCommit. The
            # verified diagram was projected into the default figure region, which is the block
            # rect; the work rect is the diagram work column.
            diagram = page.diagram
            await send_event(PageCommit(
                generation=generation,
                turn_id=turn_id,
                page_id=page_id,
                # A fresh id per activation, so a re-sent commit is idempotent while a
                # resume/doubt re-commit of the same page re-stages on the client.
                commit_id=f"c_{page_id}_{turn_id[:8]}",
                work_rect=Rect(x=WORK_RECT_DIAGRAM.x, y=WORK_RECT_DIAGRAM.y,
                               width=WORK_RECT_DIAGRAM.width, height=WORK_RECT_DIAGRAM.height),
                blocks=[Block(
                    id="fig",
                    role="figure",
                    rect=Rect(x=FIGURE_REGION.x, y=FIGURE_REGION.y,
                              width=FIGURE_REGION.width, height=FIGURE_REGION.height),
                    commands=diagram.commands,
                    anchors=diagram.anchors,
                    reveals=diagram.reveals,
                    deferred_annotations=diagram.deferred_annotations,
                    label_glossary=diagram.label_glossary,
                    alias_map=diagram.alias_map,
                    namespace=diagram.namespace,
                )],
                reference=reference,
            ))
        else:
            await send_event(DiagramCommit(
                generation=generation,
                turn_id=turn_id,
                page_id=page_id,
                diagram=page.diagram,
                reference=reference,
            ))
        run.figure_committed = True

    async def _publish(self, run: PageRun, src: int, turn_id: str, generation: int) -> None:
        step, _tts = run.stream.items[src]
        out = step.model_copy(update={
            "turn_id": turn_id,
            "generation": generation,
            "step_index": src - run.publish_from,
            "source_step_index": src,
            "is_last": run.production == "done" and src == len(run.stream.items) - 1,
        })
        await send_event(StepEvt(generation=generation, step=out))
        self.mgr._mark("first_step_published", run)
        self.mgr.state.steps_sent.append(out)
        self.mgr.state.published_upto = src - run.publish_from
        run.published_upto = src
        self.mgr.ledger.on_published(run.page_id, out)


class LessonRunner:
    """Owns every PageRun of the session."""

    def __init__(self, mgr: Any) -> None:
        self.mgr = mgr
        self.runs: dict[str, PageRun] = {}
        self.active: PageRun | None = None
        self.publisher = StreamPublisher(mgr)
        # The semaphore caps concurrent producers (MAX_CONCURRENT_PRODUCERS).
        self._producer_sem = asyncio.Semaphore(settings.MAX_CONCURRENT_PRODUCERS)
        # The outline plan drives page runs; `lesson_id` names the whole chapter.
        self.chapter_plan: Any = None
        self.chapter_title: str = ""
        self.lesson_id: str | None = None
        self.page_index: int = 0
        self.advance_task: asyncio.Task[Any] | None = None
        self.advance_pending: bool = False
        self._prefetched: set[int] = set()
        self._regeneration_attempts: dict[int, int] = {}

    # ------------------------------------------------------------------ chapter
    def start_chapter(self, plan: Any, lesson_id: str) -> None:
        """Adopt an outline; page 0 is started by the manager right after this."""
        self.cancel_gap()
        self._normalize_stickies(plan)
        self.chapter_plan = plan
        self.chapter_title = getattr(plan, "title", "") or ""
        self.lesson_id = lesson_id
        self.page_index = 0
        self.advance_pending = False
        self._prefetched = set()
        self._regeneration_attempts = {}
        self.scene_cache = {}

    def _normalize_stickies(self, plan: Any) -> None:
        """The outline declares a sticky once; a later re-declaration is ignored (first wins,
        logged). This keeps one id = one figure for the whole chapter."""
        seen: set[str] = set()
        for page in getattr(plan, "pages", []):
            for block in getattr(page, "blocks", []):
                if not block.sticky:
                    continue
                if block.id in seen:
                    block.sticky = False
                    log_event("sticky_redeclared_ignored", block_id=block.id)
                else:
                    seen.add(block.id)

    def _harvest_stickies(self) -> None:
        """Copy compiled sticky scenes out of finished/active runs before the next page starts."""
        for run in self.runs.values():
            for block_id, entry in getattr(run.ctx, "sticky_scenes", {}).items():
                self.scene_cache.setdefault(block_id, entry)

    def _carried_stickies(self, index: int) -> tuple[list[tuple[Any, Any]], dict[str, list[str]]]:
        """Sticky blocks declared before `index`, with the reveal ids already shown (ledger)."""
        plan = self.chapter_plan
        if plan is None:
            return [], {}
        self._harvest_stickies()
        carried: list[tuple[Any, Any]] = []
        revealed: dict[str, list[str]] = {}
        for earlier in range(index):
            for block in getattr(plan.pages[earlier], "blocks", []):
                if not block.sticky:
                    continue
                entry = self.scene_cache.get(block.id)
                if entry is None:
                    # Declared but never compiled — the page still teaches without it.
                    log_event("sticky_block_missing", block_id=block.id, page_index=earlier)
                    continue
                carried.append((block, entry))
                scene, namespace = entry
                revealed[block.id] = self._revealed_ids_for(scene, namespace, block.id)
        return carried, revealed

    def _revealed_ids_for(self, scene: Any, namespace: str, block_id: str) -> list[str]:
        """Reveal groups already FOCUSed and deferred annotations already ANNOTATEd (acked
        ops across every page of this lesson)."""
        try:
            from app.scene_engine.verified_diagram import build_verified_diagram

            diagram = build_verified_diagram(scene, plan=None, namespace=namespace or "")
            targets = {r.target_id for r in (diagram.reveals or []) if r.target_id}
            targets |= {da.entity_id for da in (diagram.deferred_annotations or []) if da.entity_id}
        except Exception as e:                      # never crash a page on a ledger read
            log_event("sticky_reveal_scan_failed", block_id=block_id, error=f"{type(e).__name__}")
            return []
        if not targets:
            return []
        shown: set[str] = set()
        for op in self.mgr.ledger.acked_ops_all():
            if op.kind == "FOCUS" and op.entity_id in targets:
                shown.add(op.entity_id)
            elif op.kind == "ANNOTATE" and op.entity_id in targets:
                shown.add(op.entity_id)
        return sorted(shown)

    def has_next_page(self) -> bool:
        """True when the active chapter has a page after the current one."""
        plan = self.chapter_plan
        if plan is None or not getattr(plan, "pages", None):
            return False
        return int(self.mgr.state.page_index or 0) + 1 < len(plan.pages)

    def page_request(self, index: int, turn_id: str | None = None) -> TurnRequest | None:
        """A lesson-kind request that teaches one chapter page; `page_plan` routes the graph
        to the page pipeline."""
        plan = self.chapter_plan
        if plan is None or not (0 <= index < len(plan.pages)):
            return None
        page = plan.pages[index]
        return TurnRequest(
            kind="lesson",
            generation=self.mgr.state.generation,
            turn_id=turn_id or str(uuid4()),
            question=(getattr(page, "title", "") or self.chapter_title or "").strip(),
            student_text=(getattr(page, "title", "") or self.chapter_title or "").strip(),
            lesson_id=self.lesson_id,
            page_index=index,
            page_plan=page,
            page_count=len(plan.pages),
            page_titles=[p.title or f"Page {i + 1}" for i, p in enumerate(plan.pages)],
            intent="topic",
        )

    def _start_page_run(self, index: int, parked: bool = True,
                        turn_id: str | None = None) -> PageRun | None:
        """Create (and optionally park) the run that teaches chapter page `index`."""
        plan = self.chapter_plan
        req = self.page_request(index, turn_id)
        if plan is None or req is None or self.lesson_id is None:
            return None
        carried, revealed = self._carried_stickies(index)
        req.carried_stickies = carried
        req.carried_revealed = revealed
        title = plan.pages[index].title or f"Page {index + 1}"
        page = PageRecord(
            board_id=self.mgr.state.board_id,
            page_id=f"{self.lesson_id}_p{index}",
            lesson_question=self.chapter_title or title,
            turn_kind="lesson",
            turn_id=req.turn_id,
            continues_board=index > 0,
        )
        run = self.new_run(req, page)
        # Parked before the first await: `_produce` can never publish while parked.
        if parked:
            run.mode = "parked"
        return run

    def spawn_advance(self) -> None:
        """The gap timer runs as one task outside the manager lock."""
        if self.chapter_plan is None:
            self.advance_pending = False
            return
        if self.advance_task is not None and not self.advance_task.done():
            return
        self.advance_task = self.mgr._spawn(self.advance_page())

    def cancel_gap(self) -> None:
        """Rule 4 / supersede: cancel the gap task; `advance_pending` stays True so the next
        backchannel re-spawns it."""
        task = self.advance_task
        if task is not None and not task.done():
            task.cancel()
            log_event("page_gap_cancelled")
        self.advance_task = None

    @staticmethod
    def _page_turn_current(st: Any, gen: int) -> bool:
        """The gap / filler re-check. Same generation and GRAPH_RUNNING (rule 29's target)
        — or AGENT_SPEAKING: agent_state_changed fires SPEECH_STARTED for ANY agent audio, the
        filler aside included, and rule 2 then moves GRAPH_RUNNING -> AGENT_SPEAKING. A barge-in
        leaves through rule 4 (INTERRUPT_*) and a doubt/new question bumps the generation, so at
        this generation AGENT_SPEAKING can only be aside audio."""
        if st.generation != gen:
            return False
        if st.conv_state == ConvState.AGENT_SPEAKING:
            st.conv_state = ConvState.GRAPH_RUNNING   # the page's own speech starts next (rule 2)
            return True
        return st.conv_state == ConvState.GRAPH_RUNNING

    def _advance_needs_regeneration(self, nxt: PageRun | None) -> bool:
        if nxt is None or nxt.stream.items:
            return False
        if nxt.production == "running":
            return False                      # the filler path waits for it
        return nxt.production == "failed"

    async def advance_page(self) -> None:
        """Turn to page i+1 after PAGE_GAP_MS, outside the lock, generation-checked after."""
        plan = self.chapter_plan
        if plan is None:
            return
        # A re-spawn after a cancelled gap resumes the SAME target page (page_index was already
        # bumped when the first gap started); a fresh call targets the next page.
        if self.advance_pending and self.page_index < len(plan.pages):
            nxt_index = self.page_index
        else:
            nxt_index = self.page_index + 1
        if nxt_index >= len(plan.pages):
            self.advance_pending = False
            return
        run_id = f"{self.lesson_id}:p{nxt_index}"
        nxt = self.runs.get(run_id)
        if nxt is None or nxt.mode == "discarded":
            nxt = self._start_page_run(nxt_index)
        self.page_index = nxt_index
        self.advance_pending = True
        gen = self.mgr.state.generation
        try:
            await asyncio.sleep(settings.PAGE_GAP_MS / 1000)
        except asyncio.CancelledError:
            log_event("page_gap_cancelled", page_index=nxt_index)
            return                            # advance_pending stays True
        async with self.mgr._lock:
            st = self.mgr.state
            if not self._page_turn_current(st, gen):
                log_event("page_advance_stale_dropped", page_index=nxt_index,
                          gen=gen, current=st.generation, state=st.conv_state.value)
                return
            nxt = self.runs.get(run_id)
            if nxt is None or nxt.mode == "discarded":
                self.advance_pending = False
                log_event("page_advance_missing", page_index=nxt_index)
                return
            if self._advance_needs_regeneration(nxt) and not nxt.stream.items:
                attempts = self._regeneration_attempts.get(nxt_index, 0)
                if attempts < 1:
                    self._regeneration_attempts[nxt_index] = attempts + 1
                    log_event("page_regenerated", page_index=nxt_index)
                    self.discard_run(run_id)
                    nxt = self._start_page_run(nxt_index)
                    # Give an immediately-failing regenerated producer a few scheduling slices
                    # so a SECOND failure is caught here (and stops the chapter) instead of
                    # surfacing later as an empty page.
                    for _ in range(20):
                        if nxt is None or nxt.stream.items or nxt.production != "running":
                            break
                        await asyncio.sleep(0.01)
                if nxt is None:
                    self.advance_pending = False
                    log_event("page_advance_failed", page_index=nxt_index)
                    return
                if self._advance_needs_regeneration(nxt) and not nxt.stream.items:
                    # A second failure stops the chapter with a resumable checkpoint.
                    self.advance_pending = False
                    log_event("chapter_stop", page_index=nxt_index)
                    self.mgr._write_chapter_checkpoint(nxt_index)
                    await self.mgr.speak_aside(CHAPTER_STOP_LINE, "filler")
                    return
            self.advance_pending = False
            st.has_next_page = nxt_index + 1 < len(plan.pages)
            req = self.page_request(nxt_index)
            if req is None:
                log_event("page_advance_failed", page_index=nxt_index)
                return
            req.run_id = nxt.run_id
            if not nxt.stream.items and nxt.production == "running":
                # The prefetched page is still generating: keep the student company.
                await self.mgr.speak_aside(
                    PAGE_FILLER_LINES[nxt_index % 2], "filler",
                    on_done=lambda: self.mgr._spawn(self._run_page_turn(req, gen)))
            else:
                await self.mgr.run_turn(req)

    async def _run_page_turn(self, req: TurnRequest, gen: int) -> None:
        """Filler done-callback: take the lock, re-check the turn, then activate the page."""
        async with self.mgr._lock:
            st = self.mgr.state
            if not self._page_turn_current(st, gen):
                log_event("page_turn_stale_dropped", turn_id=req.turn_id, state=st.conv_state.value)
                return
            await self.mgr.run_turn(req)

    def maybe_prefetch(self) -> None:
        """While page i speaks, generate page i+1 into a parked stream."""
        plan = self.chapter_plan
        active = self.mgr.lesson_runner.active
        if plan is None or active is None:
            return
        if active.kind == "doubt":
            log_event("page_prefetch_deferred", reason="doubt")
            return
        nxt = self.page_index + 1
        if nxt >= len(plan.pages) or nxt in self._prefetched:
            return
        self._prefetched.add(nxt)
        run = self._start_page_run(nxt, parked=True)
        if run is None:
            log_event("page_prefetch_failed", page_index=nxt)
        else:
            log_event("page_prefetch_started", page_index=nxt)

    def new_run(self, req: TurnRequest, page_record: PageRecord | None) -> PageRun:
        page_index = int(getattr(req, "page_index", 0) or 0)
        if req.kind == "doubt":
            lesson_id = None
            run_id = f"d:{req.turn_id}"
        else:
            lesson_id = getattr(req, "lesson_id", None) or f"L_{req.turn_id[:6]}"
            if req.kind == "lesson":
                run_id = f"{lesson_id}:p{page_index}"
            else:
                # Regenerated resume runs are numbered per lesson: :r1, :r2, …
                n = 1 + sum(1 for r in self.runs.values()
                            if r.lesson_id == lesson_id and r.kind == "resume")
                run_id = f"{lesson_id}:p{page_index}:r{n}"
        # Each run owns its tracker, seeded with what is already on the page; the shared
        # allocator keeps wN ids unique across runs and pages.
        tracker = BoardRowTracker(allocator=self.mgr.row_ids)
        if page_record is not None:
            tracker.record_board_report(page_record.page_id,
                                        self.mgr.ledger.rows_for_prompt(page_record.page_id), 99)
        holder: dict[str, PageRun] = {}

        def _is_cancelled() -> bool:
            run = holder.get("run")
            if run is not None and run.mode == "parked":
                return False                      # a parked run keeps generating for its resume
            return self.mgr.state.generation != req.generation

        ctx = RunContext(
            run_id=run_id,
            page_record=page_record,
            row_tracker=tracker,
            is_cancelled=_is_cancelled,
        )
        stream = TurnStream(req)
        run = PageRun(
            run_id=run_id,
            kind=req.kind,
            lesson_id=lesson_id,
            page_index=page_index,
            page_id=page_record.page_id if page_record is not None else f"p_{req.turn_id[:8]}",
            origin_turn_id=req.turn_id,
            stream=stream,
            ctx=ctx,
        )
        self.runs[run_id] = run
        holder["run"] = run
        run.task = self.mgr._spawn(self._produce(run, req))
        return run

    def park_active(self) -> PageRun | None:
        run = self.active
        if run is None:
            return None
        self.publisher.park(run)
        self._evict_parked()
        return run

    def _evict_parked(self) -> None:
        """Keep at most MAX_PARKED_RUNS parked runs; discard the oldest."""
        parked = [r for r in self.runs.values() if r.mode == "parked"]
        while len(parked) > settings.MAX_PARKED_RUNS:
            oldest = parked.pop(0)
            log_event("parked_run_evicted", run_id=oldest.run_id)
            self.discard_run(oldest.run_id)

    def discard_run(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        run.mode = "discarded"
        if run.task is not None and not run.task.done():
            run.task.cancel()
        for task in (run.late_scene, run.ctx.late_scene):
            if task is not None and not task.done():
                task.cancel()

    def discard_lesson(self, lesson_id: str) -> None:
        for run_id, run in list(self.runs.items()):
            if run.lesson_id == lesson_id:
                self.discard_run(run_id)
        if self.lesson_id == lesson_id:
            self.chapter_plan = None

    def discard_prefetch_runs(self) -> None:
        """A doubt that cannot park the lesson orphans every prefetched page: cancel them."""
        for run_id, run in list(self.runs.items()):
            if run.mode == "parked":
                log_event("prefetch_discarded", run_id=run_id)
                self.discard_run(run_id)

    def run_for(self, run_id: str | None) -> PageRun | None:
        if not run_id:
            return None
        return self.runs.get(run_id)

    def resume_cursor(self, run: PageRun) -> int:
        return run.heard_upto + 1

    def on_step_progress(self, p: Any) -> None:
        """Cumulative progress from the client's audio/caption clock."""
        st = self.mgr.state
        if p.generation != st.generation or p.turn_id != st.active_turn_id or self.active is None:
            log_event("step_progress_stale_ignored", ack_gen=p.generation, cur_gen=st.generation)
            return
        run = self.active
        run.heard_upto = max(run.heard_upto, run.publish_from + p.heard_up_to)
        st.heard_step_index = max(st.heard_step_index, p.started_up_to)
        self.mgr.ledger.on_acked(p.drawn_op_ids)
        if p.event == "completed" and p.completed_by:
            run.completed_by[p.completed_by] = run.completed_by.get(p.completed_by, 0) + 1
        run.early_ops += int(getattr(p, "early_ops", 0) or 0)
        # The first started report of a page run starts generating the next page.
        if p.event == "started" and p.started_up_to >= 0:
            self.maybe_prefetch()

    async def _commit_late_scene(self, run: PageRun) -> None:
        """A figure that outlived the join budget is committed as a reference figure."""
        late = run.ctx.late_scene
        if late is None:
            return
        if run.late_scene is not late:
            run.late_scene = late
        if not late.done() or late.cancelled():
            return
        run.late_scene = None
        run.ctx.late_scene = None
        try:
            _scene, _report, diagram, status = late.result()
        except Exception as e:
            # Never crash the turn on a broken late scene; log the loss distinctly.
            log_event("late_scene_failed", run_id=run.run_id, error=f"{type(e).__name__}")
            return
        page = run.ctx.page_record
        if (diagram is not None and status == "validated" and run.mode == "active"
                and not run.figure_committed and page is not None):
            page.diagram = diagram
            page.visual_status = "validated"
            self.mgr._mark("scene_late", run)
            await self.publisher.commit_figure(run, reference=True)

    async def _produce(self, run: PageRun, req: TurnRequest) -> None:
        text_only_notified = False
        async with self._producer_sem:
            try:
                if settings.FEATURE_MEMORY:
                    history = self.mgr.memory.chat_messages()
                    memory = self.mgr.memory
                else:
                    history = getattr(self.mgr.state, "history", [])
                    memory = None
                async for step, tts in graph_mod.iter_turn_steps(
                    req, row_tracker=run.ctx.row_tracker, agent_state=self.mgr.state,
                    history=history, stored_turns=self.mgr.stored_turns,
                    gw=self.mgr.gateway, run_ctx=run.ctx, memory=memory,
                ):
                    if run.mode == "discarded":
                        return
                    if run.ctx.is_cancelled():
                        break
                    await self._commit_late_scene(run)
                    page = run.ctx.page_record
                    if page is not None and page.turn_plan is not None:
                        self.mgr._mark("plan_ready", run)
                    if page is not None and page.diagram is not None:
                        self.mgr._mark("scene_ready", run)
                    if (page is not None and page.visual_status == "retry_required"
                            and not text_only_notified and run.mode == "active"):
                        text_only_notified = True
                        await send_event(ErrorNotice(
                            generation=self.mgr.state.generation,
                            message="I'll describe the figure in words this time."))
                    if (page is not None
                            and (page.diagram is not None
                                 or getattr(run.ctx, "page_commit", None) is not None)
                            and not run.figure_committed and run.mode == "active"):
                        await self.publisher.commit_figure(run)
                    run.stream.push(step, tts)
                    await self.publisher.on_new_item(run)
            except asyncio.CancelledError:
                run.stream.finish()
                raise
            except Exception as e:
                logger.error(f"turn producer failed: {type(e).__name__}: {e}")
                log_event("turn_producer_failed", turn_id=req.turn_id, error=f"{type(e).__name__}: {e}")
                run.production = "failed"
                await self._commit_pending_figure(run)
                run.stream.finish(failed=True, partial=bool(run.stream.items))
                if run.mode == "active" and not run.stream.items:
                    # Steps were taught before the failure: the consumer ends with the pause line
                    # instead of an error notice.
                    await send_event(ErrorNotice(generation=self.mgr.state.generation,
                                                 message="I had trouble with that one. Please try asking again."))
                return
            await self._commit_pending_figure(run)
            # An empty stream is a failed production even without an exception (e.g. a
            # reasoning-only reply): advance_page/resume only regenerate "failed" runs; "done"
            # activated an empty page and the consumer spoke FALLBACK_LINE mid-chapter.
            run.production = "done" if run.stream.items else "failed"
            if not run.stream.items:
                log_event("turn_producer_empty", turn_id=req.turn_id, run_id=run.run_id)
            run.stream.finish(failed=not run.stream.items)

    async def _commit_pending_figure(self, run: PageRun) -> None:
        """Commit the pending figure at stream end, not only when a STEP arrives: a validated
        figure of a 0-step turn would otherwise stay in the page record (and board_state) without
        ever reaching the board — a ghost on refresh or on the next unrelated turn."""
        page = run.ctx.page_record
        if (page is None or run.figure_committed or run.mode != "active"
                or (page.diagram is None and getattr(run.ctx, "page_commit", None) is None)):
            return
        try:
            await self.publisher.commit_figure(run)
        except Exception as e:                    # never turn a stream end into a crash
            log_event("figure_commit_failed", run_id=run.run_id, error=f"{type(e).__name__}: {e}")

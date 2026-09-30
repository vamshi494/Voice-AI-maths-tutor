# app/agents/graph.py
import asyncio
from collections.abc import AsyncIterator
from typing import Any
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter
from app.agents.graph_state import GraphState, RunContext
from app.agents.nodes.doubt_context import inherited_teaching_context
from app.agents.nodes.problem_ir import process_problem_ir_and_solve
from app.agents.nodes.scene import plan_and_compile_scene
from app.agents.nodes.teaching import stream_teaching_turn
from app.agents.nodes.turn_plan import plan_turn
from app.config import settings
from app.contracts.agent_state import TurnRequest
from app.contracts.board_ops import Step
from app.gateway.groq_client import GroqGateway
from app.observability import log_event
from app.scene_engine.layout import BlockSpec, PageIntent, compute_layout, table_aspect, text_aspect
from app.scene_engine.page_commit import build_page_commit
from app.scene_engine.project import project_scene_to_commands
from app.scene_engine.verified_diagram import build_verified_diagram
from app.tutor.authority import apply_solver_override
from app.tutor.board_rows import BoardRowTracker
from app.tutor.stream_parser import scrub_board_text


def _cancelled(run_ctx: RunContext | None, agent_state: Any, req: TurnRequest) -> bool:
    """A run is cancelled when its context says so; without one, fall back to generation."""
    if run_ctx is not None:
        return bool(run_ctx.is_cancelled())
    return agent_state is not None and getattr(agent_state, "generation", req.generation) != req.generation


def _page_for(run_ctx: RunContext | None, agent_state: Any) -> Any:
    """Graph writes go to the run's page record; agent_state.page is the legacy fallback."""
    if run_ctx is not None:
        return run_ctx.page_record
    return getattr(agent_state, "page", None)


def _topic_from_plan(plan: Any, question: str) -> str:
    """Short human topic for the off-topic redirect template ("Let's keep our focus on ...")."""
    law_ids = list(getattr(plan, "law_ids", None) or [])
    if law_ids:
        return law_ids[0].replace("_", " ").strip()
    q = " ".join((question or "").split())
    return (q[:48] + "...") if len(q) > 48 else (q or "this question")


def route_request(state: GraphState) -> str:
    """Route execution based on TurnRequest.kind; a chapter page has its own pipeline."""
    req = state["request"]
    if req.kind == "lesson":
        if getattr(req, "page_plan", None) is not None:
            return "page_prepare"
        return "lesson_turn_plan"
    if req.kind == "doubt":
        return "doubt_prepare"
    if req.kind == "resume":
        return "resume_prepare"
    raise ValueError(f"Unknown TurnRequest kind: '{req.kind}'")


async def lesson_turn_plan_node(state: GraphState) -> dict:
    req = state["request"]
    gw = state.get("gw")
    plan = await plan_turn(req.question, conversation="", gw=gw)
    return {"plan": plan}


async def problem_ir_solve_audit_node(state: GraphState) -> dict:
    req = state["request"]
    plan = state.get("plan")
    gw = state.get("gw")
    if plan is None:
        return {"problem_ir": None, "solver": None, "audit": None, "solver_projection": {}}
    ir, solver_res, audit, solver_proj = await process_problem_ir_and_solve(req.question, plan, gw=gw)
    return {
        "problem_ir": ir,
        "solver": solver_res,
        "audit": audit,
        "solver_projection": solver_proj,
    }


async def scene_plan_compile_node(state: GraphState) -> dict:
    req = state["request"]
    plan = state.get("plan")
    gw = state.get("gw")
    agent_state = state.get("agent_state")
    run_ctx = state.get("run_ctx")
    # Teaching must not wait on the figure longer than the join budget. The task is
    # shielded so a late figure can still be committed as a reference by the LessonRunner.
    # plan may be None: plan_and_compile_scene decides (a geometry question still gets a figure)
    late_scene = asyncio.create_task(plan_and_compile_scene(
        req.question, plan, namespace=getattr(req, "namespace", "") or "", gw=gw
    ))
    try:
        scene, report, diagram, visual_status = await asyncio.wait_for(
            asyncio.shield(late_scene), settings.SCENE_JOIN_BUDGET_S)
    except asyncio.TimeoutError:
        if run_ctx is not None:
            run_ctx.late_scene = late_scene
        log_event("scene_join_budget_exceeded", turn_id=req.turn_id)
        return {"scene": None, "report": None, "diagram": None, "visual_status": "text_only",
                "late_scene": late_scene}

    # Stale guard: a superseded turn must never write into the NEW turn's page record nor
    # commit its figure to the board.
    if _cancelled(run_ctx, agent_state, req):
        log_event("stale_scene_dropped", turn_id=req.turn_id)
        return {"scene": scene, "report": report, "diagram": None, "visual_status": visual_status}

    # If diagram is verified, update the run's page record; the manager publishes DiagramCommit.
    if diagram and visual_status == "validated":
        page = _page_for(run_ctx, agent_state)
        if page is not None:
            page.diagram = diagram
            page.visual_status = visual_status
            page.turn_plan = plan
    else:
        page = _page_for(run_ctx, agent_state)
        if page is not None:
            page.visual_status = visual_status

    return {
        "scene": scene,
        "report": report,
        "diagram": diagram,
        "visual_status": visual_status,
    }


async def page_prepare_node(state: GraphState) -> dict:
    """One chapter page -> optional TurnPlan + figures in parallel + layout + commit.

    Every failure degrades to a text-only (or block-less) page, never an exception: a page with
    no blocks still commits its work rect so the client switches columns.
    """
    req = state["request"]
    page_plan = req.page_plan
    run_ctx = state.get("run_ctx")
    agent_state = state.get("agent_state")
    gw = state.get("gw")
    page = _page_for(run_ctx, agent_state)
    if page_plan is None:
        return {"plan": None, "page_commit": None, "diagram": None, "visual_status": "text_only"}

    plan = None
    if getattr(page_plan, "numeric_task", None):
        plan = await plan_turn(page_plan.numeric_task, gw=gw)
        if plan is not None:
            try:
                _ir, _solver, _audit, solver_proj = await process_problem_ir_and_solve(
                    page_plan.numeric_task, plan, gw=gw)
                if page is not None and solver_proj:
                    page.solver_projection = solver_proj
            except Exception as e:
                log_event("page_solver_failed", error=f"{type(e).__name__}: {e}")

    specs: list[BlockSpec] = []
    figures: dict[str, tuple[Any, Any, str]] = {}
    texts: dict[str, list[str]] = {}
    addons: list[str] = []
    first_diagram = None
    page_index = int(getattr(req, "page_index", 0) or 0)

    # Carried sticky blocks come first, from the runner's compiled scene cache — they
    # are the same figures as earlier pages (stable ids) re-laid-out for this page.
    carried = list(getattr(req, "carried_stickies", None) or [])
    for block, entry in carried:
        try:
            scene, namespace = entry
            diagram = build_verified_diagram(scene, plan, namespace or "")
        except Exception as e:
            log_event("page_block_failed", block_id=getattr(block, "id", "?"),
                      error=f"{type(e).__name__}: {e}")
            continue
        specs.append(BlockSpec(id=block.id, role="figure",
                               preferred_aspect=project_scene_to_commands(scene, namespace="")[2],
                               sticky=True))
        figures[block.id] = (scene, plan, namespace or "")
        if diagram.prompt_addon:
            addons.append(diagram.prompt_addon)
        if first_diagram is None:
            first_diagram = diagram

    figure_blocks = [b for b in page_plan.blocks if b.role == "figure"]

    async def _compile(block: Any) -> Any:
        return await plan_and_compile_scene(block.brief, plan,
                                            namespace=f"pg{page_index}_", gw=gw,
                                            declared_figure=True)

    results = await asyncio.gather(*[_compile(b) for b in figure_blocks], return_exceptions=True)
    for block, res in zip(figure_blocks, results):
        if isinstance(res, BaseException):
            log_event("page_block_failed", block_id=block.id,
                      error=f"{type(res).__name__}: {res}")
            continue
        scene, _report, diagram, status = res
        if scene is None or diagram is None or status != "validated":
            log_event("page_block_failed", block_id=block.id, error=f"scene {status}")
            continue
        namespace = f"pg{page_index}_"
        specs.append(BlockSpec(id=block.id, role="figure",
                               preferred_aspect=project_scene_to_commands(scene, namespace="")[2],
                               sticky=bool(block.sticky)))
        figures[block.id] = (scene, plan, namespace)
        if block.sticky and run_ctx is not None:
            run_ctx.sticky_scenes[block.id] = (scene, namespace)
        if diagram.prompt_addon:
            addons.append(diagram.prompt_addon)
        if first_diagram is None:
            first_diagram = diagram

    for block in page_plan.blocks:
        if block.role == "figure":
            continue
        # Outline text is drawn verbatim on the canvas — LaTeX becomes board symbols.
        lines = [scrub_board_text(line) for line in (block.text or "").split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            log_event("page_block_failed", block_id=block.id, error="empty text")
            continue
        texts[block.id] = lines
        aspect = table_aspect(lines) if block.role == "table" else text_aspect(lines)
        specs.append(BlockSpec(id=block.id, role=block.role, preferred_aspect=aspect,
                               sticky=bool(block.sticky)))

    layout = compute_layout(PageIntent(has_work=True, blocks=specs))
    commit = None
    if page is not None:
        carried_ids = [getattr(block, "id", "") for block, _entry in carried]
        commit = build_page_commit(
            turn_id=req.turn_id, page_id=page.page_id, commit_id="c_page", layout=layout,
            figures=figures, texts=texts,
            revealed=dict(getattr(req, "carried_revealed", None) or {}),
            carried_ids=carried_ids)
        if run_ctx is not None:
            run_ctx.page_commit = commit
            run_ctx.figure_addons = addons
        page.turn_plan = plan
        if first_diagram is not None:
            page.diagram = first_diagram
            page.visual_status = "validated"
            page.figure_drawn = True
        else:
            page.visual_status = "text_only"
            page.figure_drawn = False
    return {"plan": plan, "page_commit": commit, "diagram": first_diagram,
            "visual_status": "validated" if first_diagram is not None else "text_only"}


async def reconcile_authority_node(state: GraphState) -> dict:
    audit = state.get("audit")
    plan = state.get("plan")
    solver = state.get("solver")
    problem_ir = state.get("problem_ir")
    req = state["request"]
    agent_state = state.get("agent_state")
    run_ctx = state.get("run_ctx")
    out: dict = {}
    solver_projection = state.get("solver_projection") or {}
    if audit and audit.status == "contradiction" and plan and solver and problem_ir:
        solver_projection = apply_solver_override(plan, problem_ir, solver)
        out["solver_projection"] = solver_projection

    # Record what this page was taught from for EVERY lesson, text-only included: storing
    # turn_plan and solver_projection here means a doubt on an algebra lesson still inherits
    # a plan via inheritedTeachingContext Case A and is taught with locked numbers.
    if not _cancelled(run_ctx, agent_state, req):
        page = _page_for(run_ctx, agent_state)
        if page is not None:
            if plan is not None:
                page.turn_plan = plan
            page.solver_projection = solver_projection or page.solver_projection
            page.visual_status = state.get("visual_status") or page.visual_status
            page.figure_drawn = bool(state.get("diagram"))
        if agent_state is not None:
            agent_state.lesson_topic = _topic_from_plan(plan, req.question)
    return out


async def doubt_prepare_node(state: GraphState) -> dict:
    req = state["request"]
    gw = state.get("gw")
    agent_state = state.get("agent_state")
    run_ctx = state.get("run_ctx")
    stored_turns = state.get("stored_turns", [])

    page_record = getattr(agent_state, "page", None)
    board_id = getattr(agent_state, "board_id", "")
    inherited_plan, solver_proj = inherited_teaching_context(
        record=page_record,
        board_id=board_id,
        lesson_question=req.question,
        stored_turns=stored_turns,
    )

    plan = getattr(req, "plan", None) or inherited_plan

    if req.requires_new_figure:
        doubt_text = req.doubt_prompt or ""
        # Context enrichment: derive the actual lesson topic/goal and recent board rows
        topic = getattr(agent_state, "lesson_topic", "") or req.question
        board_rows = getattr(getattr(agent_state, "rows", None), "rows", []) or []
        first_row = board_rows[0].get("text", "") if board_rows else ""
        if len(req.question.strip()) <= 10 and first_row:
            base_q = f"{first_row} (topic: {topic})" if topic and topic != req.question else first_row
        else:
            base_q = f"{req.question} (topic: {topic})" if topic and topic != req.question else req.question

        recent_rows_str = "; ".join(r.get("text", "") for r in board_rows[:5]) if board_rows else ""
        context_addon = f"\nBoard context so far: {recent_rows_str}" if recent_rows_str else ""
        doubt_question = f"{base_q}{context_addon}\nFollow-up case: {doubt_text}"
        mini_plan = await plan_turn(doubt_question, gw=gw)
        if mini_plan:
            plan = mini_plan

        if plan is None:
            return {
                "plan": None,
                "scene": None,
                "report": None,
                "diagram": None,
                "visual_status": "text_only",
                "solver_projection": solver_proj or {},
            }

        scene, report, diagram, visual_status = await plan_and_compile_scene(
            doubt_question,
            plan,
            namespace=getattr(req, "namespace", None) or "d1_",  # manager assigns d1_, d2_, ... per session
            gw=gw,
        )
        if _cancelled(run_ctx, agent_state, req):
            return {"plan": plan, "scene": scene, "report": report, "diagram": None,
                    "visual_status": visual_status, "solver_projection": solver_proj or {}}
        page = _page_for(run_ctx, agent_state)
        if diagram and visual_status == "validated":
            if page is not None:
                page.diagram = diagram
                page.visual_status = visual_status
                page.turn_plan = plan
                page.figure_drawn = True
        elif page is not None:
            page.visual_status = visual_status

        return {
            "plan": plan,
            "scene": scene,
            "report": report,
            "diagram": diagram,
            "visual_status": visual_status,
            "solver_projection": solver_proj or {},
        }

    diagram = getattr(req, "diagram", None) or (getattr(page_record, "diagram", None) if page_record else None)
    if not diagram and getattr(agent_state, "paused_lesson", None):
        diagram = getattr(agent_state.paused_lesson, "diagram", None)
    if not plan and getattr(agent_state, "paused_lesson", None):
        plan = getattr(agent_state.paused_lesson, "turn_plan", None)
    if not solver_proj and getattr(agent_state, "paused_lesson", None):
        solver_proj = getattr(agent_state.paused_lesson, "solver_projection", None)
    return {
        "plan": plan,
        "diagram": diagram,
        "solver_projection": solver_proj or {},
    }


async def resume_prepare_node(state: GraphState) -> dict:
    req = state["request"]
    agent_state = state.get("agent_state")
    paused = getattr(agent_state, "paused_lesson", None)

    plan = getattr(req, "plan", None) or (getattr(paused, "turn_plan", None) if paused else None) or (getattr(paused, "plan", None) if paused else None)
    diagram = getattr(req, "diagram", None) or (getattr(paused, "diagram", None) if paused else None)
    return {
        "plan": plan,
        "diagram": diagram,
    }


async def teaching_node(state: GraphState, writer: StreamWriter) -> dict:
    req = state["request"]
    plan = state.get("plan")
    diagram = state.get("diagram")
    row_tracker = state.get("row_tracker") or BoardRowTracker()
    history = state.get("history", [])
    memory = state.get("memory")
    agent_state = state.get("agent_state")
    run_ctx = state.get("run_ctx")
    gw = state.get("gw")

    steps: list[Step] = []
    raw_response = ""

    async for item in stream_teaching_turn(
        request=req,
        plan=plan,
        diagram=diagram,
        row_tracker=row_tracker,
        history=history,
        lesson_question=req.question,
        agent_state=agent_state,
        gw=gw,
        run_ctx=run_ctx,
        memory=memory,
    ):
        step = item["step"]
        tts_text = item["tts_text"]
        raw_response = item.get("raw_response", "")
        steps.append(step)
        writer({"step": step, "tts_text": tts_text, "raw_response": raw_response})

    return {
        "steps": steps,
        "raw_response": raw_response,
    }


def build_graph():
    """Build and compile the LangGraph execution graph for a turn."""
    builder = StateGraph(GraphState)

    builder.add_node("lesson_turn_plan", lesson_turn_plan_node)
    builder.add_node("problem_ir_solve_audit", problem_ir_solve_audit_node)
    builder.add_node("scene_plan_compile", scene_plan_compile_node)
    builder.add_node("page_prepare", page_prepare_node)
    builder.add_node("reconcile_authority", reconcile_authority_node)
    builder.add_node("doubt_prepare", doubt_prepare_node)
    builder.add_node("resume_prepare", resume_prepare_node)
    builder.add_node("teaching", teaching_node)

    builder.add_conditional_edges(
        START,
        route_request,
        {
            "lesson_turn_plan": "lesson_turn_plan",
            "page_prepare": "page_prepare",
            "doubt_prepare": "doubt_prepare",
            "resume_prepare": "resume_prepare",
        },
    )

    builder.add_edge("lesson_turn_plan", "problem_ir_solve_audit")
    builder.add_edge("lesson_turn_plan", "scene_plan_compile")
    builder.add_edge("problem_ir_solve_audit", "reconcile_authority")
    builder.add_edge("scene_plan_compile", "reconcile_authority")
    builder.add_edge("reconcile_authority", "teaching")

    builder.add_edge("page_prepare", "teaching")
    builder.add_edge("doubt_prepare", "teaching")
    builder.add_edge("resume_prepare", "teaching")
    builder.add_edge("teaching", END)

    return builder.compile()


# Lazily compiled singleton
_compiled_graph = None


def get_compiled_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


async def iter_turn_steps(
    request: TurnRequest,
    graph: Any = None,
    row_tracker: BoardRowTracker | None = None,
    agent_state: Any | None = None,
    history: list[dict[str, str]] | None = None,
    stored_turns: list[Any] | None = None,
    gw: GroqGateway | None = None,
    run_ctx: RunContext | None = None,
    memory: Any | None = None,
) -> AsyncIterator[tuple[Step, str]]:
    """Run a turn through the graph and yield (Step, tts_text) as each step completes.

    The graph only produces steps; the manager/StreamPublisher sends the
    DiagramCommit and StepEvt (and records steps_sent) before pushing the step into the
    TurnStream, so the client holds a step's ops before it can hear that step.
    """
    app = graph or get_compiled_graph()
    rows = row_tracker or BoardRowTracker()
    if run_ctx is None:
        run_ctx = RunContext(
            run_id=request.turn_id,
            page_record=getattr(agent_state, "page", None),
            row_tracker=rows,
            is_cancelled=lambda: agent_state is not None and agent_state.generation != request.generation,
        )
    initial_state: GraphState = {
        "request": request,
        "steps": [],
        "raw_response": "",
        "run_ctx": run_ctx,
        "row_tracker": rows,
        "agent_state": agent_state,
        "history": history or [],
        "memory": memory,
        "stored_turns": stored_turns or [],
        "gw": gw,
    }
    async for item in app.astream(initial_state, stream_mode="custom"):
        if not (isinstance(item, dict) and "step" in item):
            continue
        step: Step = item["step"]
        tts_text: str = item.get("tts_text", "")
        if run_ctx.is_cancelled():
            log_event("stale_generation_aborted", req_gen=request.generation,
                      state_gen=getattr(agent_state, "generation", None))
            return
        yield step, tts_text


async def run_turn_stream(
    request: TurnRequest,
    graph: Any = None,
    row_tracker: BoardRowTracker | None = None,
    agent_state: Any | None = None,
    history: list[dict[str, str]] | None = None,
    stored_turns: list[Any] | None = None,
    gw: GroqGateway | None = None,
    run_ctx: RunContext | None = None,
    memory: Any | None = None,
) -> AsyncIterator[str]:
    """Text-only view of iter_turn_steps (kept for the llm_node adapter and tests)."""
    async for _step, tts_text in iter_turn_steps(
        request, graph=graph, row_tracker=row_tracker, agent_state=agent_state,
        history=history, stored_turns=stored_turns, gw=gw, run_ctx=run_ctx, memory=memory,
    ):
        yield tts_text + " "

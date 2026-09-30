# app/agents/nodes/teaching.py
import asyncio
from collections.abc import AsyncIterator
from typing import Any
from app.config import settings
from app.contracts.agent_state import AgentState, TurnRequest
from app.contracts.board_ops import Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.turn_plan import TurnPlan
from app.gateway.groq_client import GatewayStreamError, GroqGateway, gateway
from app.memory.service import MemoryService
from app.prompts.registry import ACTIVE, get_prompt
from app.tutor.board_rows import BoardRowTracker
from app.tutor.doubt_prompt import build_marked_doubt_prompt
from app.tutor.stream_parser import StreamParser


def _rows_remaining(row_tracker: BoardRowTracker, diagram: VerifiedDiagram | None) -> int:
    """A reported capacity wins, else estimate from the page kind."""
    if row_tracker.rows_remaining < 99:
        return row_tracker.rows_remaining
    return 13 if diagram else 39


def _rows_listing(row_tracker: BoardRowTracker) -> str:
    listing = "\n".join(f"{r['row_id']}: {r['text']}" for r in row_tracker.visible_rows)
    return listing if listing else "(no rows written yet)"


def format_heard_steps(texts: list[str]) -> str:
    """Numbered heard steps, trimmed from the oldest end to 2000 chars."""
    if not texts:
        return "(nothing yet)"
    lines = [f"{i + 1}. {t}" for i, t in enumerate(texts)]
    joined = "\n".join(lines)
    trimmed = False
    while len(joined) > 2000 and len(lines) > 1:
        lines.pop(0)
        trimmed = True
        joined = "\n".join(lines)
    if trimmed:
        lines[0] = "…"
        joined = "\n".join(lines)
    return joined


def build_teaching_system_prompt(
    turn_kind: str,
    plan: TurnPlan | None,
    diagram: VerifiedDiagram | None,
    lesson_question: str,
    row_tracker: BoardRowTracker,
    tutor_name: str = "Vamshi",
    requires_new_figure: bool = False,
    heard_steps_text: list[str] | None = None,
    memory_block: str = "",
    page_plan: Any | None = None,
    page_number: int = 0,
    page_count: int = 0,
    lesson_title: str = "",
    page_titles: list[str] | None = None,
    figure_addons: list[str] | None = None,
) -> str:
    """Compose the teaching system prompt."""
    parts = []

    # 1. Base prompt
    parts.append(get_prompt(ACTIVE["teaching.base"]).format(tutor_name=tutor_name))

    # 2. Turn plan block
    if plan:
        parts.append(f"\nTURN PLAN:\n{plan.model_dump_json(by_alias=True, exclude_none=True)}")

    # 3. Verified figures or text-only block. A multi-block page lists every figure's addon;
    #    the legacy single-figure path keeps `diagram`.
    addons = [a for a in (figure_addons or []) if a]
    if not addons and diagram is not None and diagram.prompt_addon:
        addons = [diagram.prompt_addon]
    if addons:
        parts.append("\nVERIFIED FIGURES:\n" + "\n".join(addons))
    else:
        parts.append("\nVERIFIED FIGURES:\n" + get_prompt(ACTIVE["teaching.text_only"]).format(
            rows_remaining=_rows_remaining(row_tracker, diagram)))

    # 4. Kind-specific block; a chapter page uses teaching.page.v1 instead of the
    #    single-problem structure.
    if page_plan is not None:
        titles = list(page_titles or [])
        previous_line = ""
        if page_number > 1 and len(titles) >= page_number - 1:
            previous_line = "Earlier pages: " + "; ".join(titles[: page_number - 1])
        key_points = "; ".join(getattr(page_plan, "key_points", None) or []) or "(none)"
        parts.append("\n" + get_prompt(ACTIVE["teaching.page"]).format(
            page_number=page_number,
            page_count=page_count,
            lesson_title=lesson_title or lesson_question,
            page_title=getattr(page_plan, "title", "") or lesson_question,
            objective=getattr(page_plan, "objective", "") or "(none)",
            key_points=key_points,
            previous_pages_line=previous_line,
            step_budget=getattr(page_plan, "step_budget", settings.STEP_BUDGET_PAGE_DEFAULT),
            rows_remaining=_rows_remaining(row_tracker, diagram),
        ))

    elif turn_kind == "lesson":
        parts.append("\n" + get_prompt(ACTIVE["teaching.lesson_structure"]).format(
            step_budget=settings.STEP_BUDGET_PROBLEM,
            rows_remaining=_rows_remaining(row_tracker, diagram),
        ))

    elif turn_kind == "doubt":
        if requires_new_figure:
            parts.append("\n" + get_prompt(ACTIVE["teaching.doubt_new_page"]).format(
                lesson_question=lesson_question,
            ))
        else:
            figure_line = (
                "The figure on the right stays. Point only at the parts the doubt is about."
                if diagram
                else "There is no figure on this page."
            )
            parts.append("\n" + get_prompt(ACTIVE["teaching.doubt_same_board"]).format(
                lesson_question=lesson_question,
                rows_listing=_rows_listing(row_tracker),
                figure_line=figure_line,
                rows_remaining=_rows_remaining(row_tracker, diagram),
            ))

    elif turn_kind == "resume":
        parts.append("\n" + get_prompt(ACTIVE["teaching.resume"]).format(
            lesson_question=lesson_question,
            heard_steps=format_heard_steps(heard_steps_text or []),
            rows_listing=_rows_listing(row_tracker),
            rows_remaining=_rows_remaining(row_tracker, diagram),
        ))

    # 5. Memory block: omitted when empty.
    if memory_block:
        parts.append("\nMEMORY:\n" + memory_block)

    return "\n".join(parts)


async def stream_teaching_turn(
    request: TurnRequest,
    plan: TurnPlan | None,
    diagram: VerifiedDiagram | None,
    row_tracker: BoardRowTracker,
    history: list[Any] | None = None,
    lesson_question: str = "",
    agent_state: AgentState | None = None,
    gw: GroqGateway | None = None,
    run_ctx: Any | None = None,
    memory: MemoryService | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream teaching steps from LLM with tag parsing and staleness guards."""
    gw_client = gw or gateway

    def _is_cancelled() -> bool:
        if run_ctx is not None:
            return bool(run_ctx.is_cancelled())
        return bool(agent_state and agent_state.generation != request.generation)

    heard_steps_text: list[str] | None = None
    if request.kind == "resume" and agent_state is not None:
        paused = getattr(agent_state, "paused_lesson", None)
        if paused is not None:
            heard_steps_text = paused.heard_steps_text

    # MEMORY block: the session's rolling/page summaries plus this page's rows and
    # the steps the student heard this turn.
    memory_block = ""
    if memory is not None and settings.FEATURE_MEMORY:
        page_id = agent_state.page.page_id if agent_state is not None and agent_state.page else None
        heard = ""
        if agent_state is not None and agent_state.heard_step_index >= 0:
            heard = " ".join(s.spoken_text
                             for s in agent_state.steps_sent[: agent_state.heard_step_index + 1])
        memory_block = memory.context_block(page_id, row_tracker.visible_rows, heard)

    page_plan = getattr(request, "page_plan", None)
    chapter_title = ""
    if page_plan is not None and run_ctx is not None and run_ctx.page_record is not None:
        chapter_title = run_ctx.page_record.lesson_question or ""

    system_prompt = build_teaching_system_prompt(
        turn_kind=request.kind,
        plan=plan,
        diagram=diagram,
        lesson_question=lesson_question or request.question,
        row_tracker=row_tracker,
        requires_new_figure=bool(getattr(request, "requires_new_figure", False)),
        heard_steps_text=heard_steps_text,
        memory_block=memory_block,
        page_plan=page_plan,
        page_number=int(getattr(request, "page_index", 0) or 0) + 1,
        page_count=int(getattr(request, "page_count", 0) or 0),
        lesson_title=chapter_title,
        page_titles=list(getattr(request, "page_titles", None) or []),
        figure_addons=list(getattr(run_ctx, "figure_addons", None) or []) if run_ctx is not None else [],
    )

    messages = [{"role": "system", "content": system_prompt}]

    if history:
        for turn in history[-settings.HISTORY_EXCHANGES * 2:]:
            if isinstance(turn, dict):
                messages.append(turn)          # already a role dict
            else:
                role = "user" if getattr(turn, "role", "") == "student" else "assistant"
                messages.append({"role": role, "content": str(getattr(turn, "content", ""))})

    # User message
    if request.kind == "lesson" and page_plan is not None:
        messages.append({"role": "user", "content": (
            f"Teach page {int(getattr(request, 'page_index', 0) or 0) + 1} "
            f"of {int(getattr(request, 'page_count', 0) or 0)}: "
            f"{getattr(page_plan, 'title', '') or request.question}.")})
    elif request.kind == "lesson":
        messages.append({"role": "user", "content": request.question})
    elif request.kind == "doubt":
        doubt_content = request.doubt_prompt or build_marked_doubt_prompt(
            request.marks, "", lesson_question or request.question
        )
        messages.append({"role": "user", "content": doubt_content})
    elif request.kind == "resume":
        messages.append({"role": "user", "content": "Continue the lesson from where we stopped."})

    parser = StreamParser(
        turn_id=request.turn_id,
        generation=request.generation,
        turn_kind=request.kind,
        row_tracker=row_tracker,
        diagram=diagram,
    )

    model_to_use = settings.MODEL_FAST if request.kind == "doubt" else settings.MODEL_MAIN

    stream_iter = gw_client.stream_text(
        prompt_key=ACTIVE["teaching.base"],
        messages=messages,
        model=model_to_use,
        timeout_s=60.0,
    )

    # Watchdog: a stalled or runaway teaching stream raises GatewayStreamError so the
    # turn ends partial and stays resumable instead of hanging the lesson.
    aiter = stream_iter.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.STREAM_TOTAL_TIMEOUT_S
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise GatewayStreamError("teaching stream exceeded total timeout")
        try:
            chunk = await asyncio.wait_for(
                aiter.__anext__(), timeout=min(settings.STREAM_IDLE_TIMEOUT_S, remaining))
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as e:
            raise GatewayStreamError(
                f"teaching stream idle for more than {settings.STREAM_IDLE_TIMEOUT_S}s") from e

        # Stale guard
        if _is_cancelled():
            return

        steps_and_tts = parser.append(chunk)
        for step, tts_text in steps_and_tts:
            if _is_cancelled():
                return
            yield {"step": step, "tts_text": tts_text, "raw_response": parser.raw_response}

    # Finalize trailing step
    for step, tts_text in parser.finish():
        if _is_cancelled():
            return
        yield {"step": step, "tts_text": tts_text, "raw_response": parser.raw_response}

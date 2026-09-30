# app/agents/nodes/problem_ir.py
import asyncio
from typing import Any
from app.config import settings
from app.contracts.problem_ir import ProblemIR
from app.contracts.solver import AuthorityAudit, SolverResult
from app.contracts.turn_plan import TurnPlan
from app.gateway.groq_client import GroqGateway, gateway
from app.prompts.registry import ACTIVE
from app.solver import sympy_solver
from app.tutor.authority import apply_solver_override, verify_plan_against_solver


def validate_problem_ir(ir: ProblemIR, question: str, plan: TurnPlan) -> list[str]:
    """Post-validate ProblemIR against submitted question and TurnPlan."""
    errors = []
    # 1. Exact question match
    if ir.question != question:
        errors.append(f"ProblemIR question does not match submitted question: '{ir.question}' != '{question}'")

    # 2. Evidence spans match exactly
    for f in ir.facts:
        ev = f.evidence
        if ev.source == "question":
            expected_quote = question[ev.start:ev.end]
            if ev.quote != expected_quote:
                errors.append(
                    f"Evidence quote mismatch for fact '{f.id}': quote '{ev.quote}' != question[{ev.start}:{ev.end}] ('{expected_quote}')"
                )

    # 3. Referenced IDs resolve
    fact_ids = {f.id for f in ir.facts}
    for ent in ir.entities:
        for fid in ent.evidence_fact_ids:
            if fid not in fact_ids:
                errors.append(f"Entity '{ent.id}' references missing fact '{fid}'")

    # 4. Result bindings match plan unknown IDs and symbols
    plan_unknowns = {u.id: u.symbol for u in plan.unknowns}
    plan_derived = {d.id: d.symbol for d in plan.derived}
    for req in ir.solve_requests:
        if req.result_binding:
            qid = req.result_binding.turn_plan_quantity_id
            sym = req.result_binding.symbol
            if qid not in plan_unknowns and qid not in plan_derived:
                errors.append(f"ResultBinding quantity '{qid}' is not an unknown or derived quantity in TurnPlan")
            elif qid in plan_unknowns and plan_unknowns[qid] != sym:
                errors.append(f"ResultBinding symbol '{sym}' does not match TurnPlan symbol '{plan_unknowns[qid]}'")

    return errors


async def process_problem_ir_and_solve(
    question: str,
    plan: TurnPlan,
    gw: GroqGateway | None = None,
) -> tuple[ProblemIR | None, SolverResult | None, AuthorityAudit | None, dict[str, str]]:
    """Execute ProblemIR call, solve with sympy (via asyncio.to_thread), and authority audit."""
    gw_client = gw or gateway
    user_prompt = f"VALIDATED TURNPLAN:\n{plan.model_dump_json(by_alias=True, exclude_none=True)}\n\nSUBMITTED QUESTION:\n{question}"

    # First attempt
    ir = await gw_client.complete_json(
        prompt_key=ACTIVE["problem_ir"],
        user=user_prompt,
        schema=ProblemIR,
        model=settings.MODEL_MAIN,
        timeout_s=settings.TURN_PLAN_LANE_TIMEOUT_S,
    )

    errors: list[str] = []
    if ir is not None:
        errors = validate_problem_ir(ir, question, plan)

    # Retry once if failed or invalid
    if ir is None or errors:
        error_lines = "\n".join(f"- {e}" for e in errors)
        retry_user_prompt = f"{user_prompt}\n\nERRORS FROM PREVIOUS ATTEMPT:\n{error_lines}"
        ir_retry = await gw_client.complete_json(
            prompt_key=ACTIVE["problem_ir"],
            user=retry_user_prompt,
            schema=ProblemIR,
            model=settings.MODEL_MAIN,
            timeout_s=settings.TURN_PLAN_LANE_TIMEOUT_S,
        )
        if ir_retry is not None:
            retry_errors = validate_problem_ir(ir_retry, question, plan)
            if not retry_errors:
                ir = ir_retry
            else:
                ir = None

    if ir is None:
        audit = AuthorityAudit(status="incomplete", issues=[{"code": "problem_ir_failed"}], bindings=[])
        return None, None, audit, {}

    # Solve requests using sympy via asyncio.to_thread
    try:
        solver_res = await asyncio.wait_for(
            asyncio.to_thread(sympy_solver.solve, ir),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, Exception):
        solver_res = SolverResult(status="unsolvable", values=[])

    audit = verify_plan_against_solver(plan, ir, solver_res)

    # Check contradiction policy
    solver_projection: dict[str, str] = {}
    if audit.status == "contradiction":
        # Apply solver override
        solver_projection = apply_solver_override(plan, ir, solver_res)
    else:
        for req in ir.solve_requests:
            if req.result_binding:
                val = next((v for v in solver_res.values if v.request_id == req.id), None)
                if val and val.approximate is not None:
                    solver_projection[req.result_binding.turn_plan_quantity_id] = val.exact or f"{val.approximate:g}"

    return ir, solver_res, audit, solver_projection

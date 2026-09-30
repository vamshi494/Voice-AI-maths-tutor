# app/tutor/authority.py
from app.contracts.problem_ir import ProblemIR
from app.contracts.solver import AuthorityAudit, AuthorityBinding, SolvedValue, SolverResult
from app.contracts.turn_plan import Quantity, TurnPlan
from app.observability import log_event


def find_derived(plan: TurnPlan, quantity_id: str, symbol: str | None = None) -> Quantity | None:
    for d in plan.derived:
        if d.id == quantity_id or (symbol and d.symbol == symbol):
            return d
    for g in plan.givens:
        if g.id == quantity_id or (symbol and g.symbol == symbol):
            return g
    return None


def value_for_request(result: SolverResult, request_id: str, prefer_near: float = 0.0) -> SolvedValue | None:
    for val in result.values:
        if val.request_id == request_id:
            if val.roots:
                # Pick root nearest prefer_near
                nearest = min(val.roots, key=lambda r: abs(r - prefer_near))
                return SolvedValue(
                    request_id=val.request_id,
                    exact=val.exact,
                    approximate=nearest,
                    error_bound=val.error_bound,
                    roots=val.roots,
                )
            return val
    return None


def verify_plan_against_solver(
    plan: TurnPlan, ir: ProblemIR, result: SolverResult
) -> AuthorityAudit:
    """Audit plan quantities against solver results.

    A binding agrees when |solver - plan| <= max(error_bound, 1e-4).
    """
    bindings: list[AuthorityBinding] = []
    issues = []

    for req in ir.solve_requests:
        if not req.result_binding:
            continue
        target_qid = req.result_binding.turn_plan_quantity_id
        target_sym = req.result_binding.symbol
        q = find_derived(plan, quantity_id=target_qid, symbol=target_sym)

        if q is None:
            issues.append({"code": "binding_missing_in_plan", "message": f"Bound quantity '{target_qid}' not in plan"})
            continue

        sv = value_for_request(result, req.id, prefer_near=q.value)
        if sv is None or sv.approximate is None:
            issues.append({"code": "solver_missing_value", "message": f"No solver value for request '{req.id}'"})
            continue

        tol = max(sv.error_bound, 1e-4)
        agreed = abs(sv.approximate - q.value) <= tol

        bindings.append(
            AuthorityBinding(
                quantity_id=q.id,
                plan_value=q.value,
                solver_value=sv.approximate,
                agreed=agreed,
            )
        )

    if not bindings and issues:
        status = "incomplete"
    elif any(not b.agreed for b in bindings):
        status = "contradiction"
    else:
        status = "verified"

    return AuthorityAudit(status=status, issues=issues, bindings=bindings)


def apply_solver_override(plan: TurnPlan, ir: ProblemIR, result: SolverResult) -> dict[str, str]:
    """Contradiction policy: overwrite disagreeing derived values with solver values and drop source_text.

    Returns solver_projection: {quantity_id: exact_str}.
    """
    solver_projection: dict[str, str] = {}
    for req in ir.solve_requests:
        if not req.result_binding:
            continue
        q = find_derived(plan, req.result_binding.turn_plan_quantity_id, req.result_binding.symbol)
        sv = value_for_request(result, req.id, prefer_near=q.value if q else 0.0)
        if q and sv and sv.approximate is not None:
            tol = max(sv.error_bound, 1e-4)
            if abs(sv.approximate - q.value) > tol:
                log_event(
                    "authority_contradiction",
                    quantity_id=q.id,
                    plan_value=q.value,
                    solver_value=sv.approximate,
                )
                q.value = sv.approximate
                q.source_text = None  # drop source_text so teaching never sees stale arithmetic
            exact_str = sv.exact or f"{sv.approximate:g}"
            solver_projection[q.id] = exact_str

    return solver_projection

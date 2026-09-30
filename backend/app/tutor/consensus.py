# app/tutor/consensus.py
from dataclasses import dataclass, field
from app.contracts.turn_plan import PlanIssue, TurnPlan
from app.observability import log_event


@dataclass
class LaneResult:
    plan: TurnPlan | None
    reconciled: bool = False
    lane: str = ""
    issues: list[PlanIssue] = field(default_factory=list)


def select_consensus(completed: list[LaneResult]) -> TurnPlan | None:
    """Select winning TurnPlan from completed lanes.

    1. If there is only one result, return it.
    2. If both lanes agree on the value of every unknown, within max(1e-4, 1e-6*|v|), return primary-lane plan.
    3. Otherwise, return the lane with reconciled == False (no arithmetic had to be corrected).
       If both or neither were reconciled, return the primary.
       Log turnplan_consensus_disagreement with both values.
    """
    valid_results = [r for r in completed if r.plan is not None]
    if not valid_results:
        return None

    if len(valid_results) == 1:
        return valid_results[0].plan

    primary_res = next((r for r in valid_results if "primary" in r.lane), valid_results[0])
    alt_res = next((r for r in valid_results if r is not primary_res), None)

    if alt_res is None or primary_res.plan is None or alt_res.plan is None:
        return primary_res.plan

    # Compare values of all unknowns
    primary_unknowns = {u.id: u for u in primary_res.plan.unknowns}
    primary_derived = {d.id: d.value for d in primary_res.plan.derived}
    for d in primary_res.plan.derived:
        primary_derived[d.symbol] = d.value

    alt_derived = {d.id: d.value for d in alt_res.plan.derived}
    for d in alt_res.plan.derived:
        alt_derived[d.symbol] = d.value

    agrees_all = True
    disagreements = {}

    for uid, u in primary_unknowns.items():
        v_primary = primary_derived.get(uid, primary_derived.get(u.symbol))
        v_alt = alt_derived.get(uid, alt_derived.get(u.symbol))

        if v_primary is None or v_alt is None:
            agrees_all = False
            disagreements[uid] = {"primary": v_primary, "alt": v_alt}
            continue

        tol = max(1e-4, 1e-6 * abs(v_primary))
        if abs(v_primary - v_alt) > tol:
            agrees_all = False
            disagreements[uid] = {"primary": v_primary, "alt": v_alt}

    if agrees_all:
        return primary_res.plan

    # Disagreement path
    log_event("turnplan_consensus_disagreement", disagreements=disagreements)

    # Prefer lane that needed no arithmetic correction
    if not primary_res.reconciled and alt_res.reconciled:
        return primary_res.plan
    if primary_res.reconciled and not alt_res.reconciled:
        return alt_res.plan

    # If both or neither were reconciled, return primary
    return primary_res.plan

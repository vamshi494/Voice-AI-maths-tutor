# tests/test_consensus.py
import pytest
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.tutor.consensus import LaneResult, select_consensus


def make_plan(val: float) -> TurnPlan:
    return TurnPlan(
        schema_version="turn-plan/v3",
        question="What is x?",
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=val, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="d1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )


def test_consensus_single():
    plan = make_plan(5.0)
    res = select_consensus([LaneResult(plan=plan, lane="primary")])
    assert res == plan


def test_consensus_agree():
    plan_a = make_plan(5.0)
    plan_b = make_plan(5.00001)  # within 1e-4 tolerance
    results = [
        LaneResult(plan=plan_a, lane="primary"),
        LaneResult(plan=plan_b, lane="retry"),
    ]
    chosen = select_consensus(results)
    assert chosen is not None
    assert abs(chosen.derived[0].value - 5.0) < 1e-4


def test_consensus_reconciled_preference():
    plan_a = make_plan(5.0)
    plan_b = make_plan(5.0)
    # Both agree, but lane B had arithmetic reconciled
    results = [
        LaneResult(plan=plan_a, lane="primary", reconciled=False),
        LaneResult(plan=plan_b, lane="retry", reconciled=True),
    ]
    chosen = select_consensus(results)
    # The reconciled plan should be preferred
    assert chosen == plan_b


def test_consensus_disagree_fallback():
    plan_a = make_plan(5.0)
    plan_b = make_plan(10.0)  # Disagrees
    results = [
        LaneResult(plan=plan_a, lane="turn_plan.primary"),
        LaneResult(plan=plan_b, lane="turn_plan.retry"),
    ]
    chosen = select_consensus(results)
    # On disagreement, selects primary lane
    assert chosen == plan_a

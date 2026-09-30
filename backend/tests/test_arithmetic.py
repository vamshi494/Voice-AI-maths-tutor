# tests/test_arithmetic.py
import pytest
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.tutor.arithmetic import eval_expr_part, normalize_source_text, reconcile_explicit_arithmetic


def evaluate_source_arithmetic(text: str, var_map: dict[str, float] | None = None) -> tuple[float | None, bool]:
    normalized = normalize_source_text(text)
    parts = normalized.split("=")
    val = eval_expr_part(parts[0], var_map or {})
    return val, val is not None


def make_test_plan(derived_list: list[Quantity]) -> TurnPlan:
    return TurnPlan(
        schema_version="turn-plan/v3",
        question="What is 4 * 5?",
        visual_requirement="none",
        givens=[],
        derived=derived_list,
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )


def test_evaluate_source_arithmetic():
    # 4 * 5 = 20
    val, ok = evaluate_source_arithmetic("4 * 5")
    assert ok and abs(val - 20.0) < 1e-6

    # Chained =: "2 * 3 = 6"
    val, ok = evaluate_source_arithmetic("2 * 3 = 6")
    assert ok and abs(val - 6.0) < 1e-6

    # Square root: \sqrt{16} or sqrt(16)
    val, ok = evaluate_source_arithmetic(r"\sqrt{16}")
    assert ok and abs(val - 4.0) < 1e-6

    val, ok = evaluate_source_arithmetic("sqrt(25)")
    assert ok and abs(val - 5.0) < 1e-6

    # Degrees trig: sin(30)
    val, ok = evaluate_source_arithmetic("sin(30)")
    assert ok and abs(val - 0.5) < 1e-6

    val, ok = evaluate_source_arithmetic("cos(60)")
    assert ok and abs(val - 0.5) < 1e-6

    # Unevaluable text is skipped
    val, ok = evaluate_source_arithmetic("let us assume x is positive")
    assert not ok


def test_reconcile_explicit_arithmetic_cases():
    # "4*5=25" with value 20 becomes 20
    derived = [
        Quantity(
            id="d1",
            symbol="A",
            value=25.0,  # Model produced 25
            source_text="4 * 5 = 25",  # Evaluates to 20
            provenance="derived",
            depends_on=[],
        )
    ]
    plan = make_test_plan(derived)
    reconciled_plan, reconciled = reconcile_explicit_arithmetic(plan)

    assert reconciled is True
    assert abs(reconciled_plan.derived[0].value - 20.0) < 1e-6

    # When source arithmetic matches value, no reconciliation needed
    derived_ok = [
        Quantity(
            id="d1",
            symbol="A",
            value=20.0,
            source_text="4 * 5 = 20",
            provenance="derived",
            depends_on=[],
        )
    ]
    plan_ok = make_test_plan(derived_ok)
    reconciled_plan_ok, reconciled = reconcile_explicit_arithmetic(plan_ok)
    assert reconciled is False
    assert abs(reconciled_plan_ok.derived[0].value - 20.0) < 1e-6

    # Unevaluable text leaves value as is
    derived_text = [
        Quantity(
            id="d2",
            symbol="B",
            value=7.0,
            source_text="by Pythagoras theorem",
            provenance="derived",
            depends_on=[],
        )
    ]
    plan_text = make_test_plan(derived_text)
    reconciled_plan_text, reconciled = reconcile_explicit_arithmetic(plan_text)
    assert reconciled is False
    assert reconciled_plan_text.derived[0].value == 7.0

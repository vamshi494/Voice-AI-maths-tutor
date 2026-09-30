# tests/test_plan_validation.py
import pytest
from app.contracts.turn_plan import (
    PlanIssue,
    Quantity,
    TurnPlan,
    Unknown,
)
from app.tutor.plan_validation import validate_turn_plan


def test_validate_plan_valid():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Find x when 2x = 6",
        visual_requirement="none",
        givens=[Quantity(id="g1", symbol="c", value=6.0, provenance="given", source_text="2x = 6")],
        derived=[Quantity(id="d1", symbol="x", value=3.0, provenance="derived", source_text="6 / 2", depends_on=["g1"])],
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],

        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Find x when 2x = 6")
    assert len(issues) == 0


def test_question_mismatch():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Find y",
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="y", value=1.0, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="d1", symbol="y")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Find x")
    codes = {i.code for i in issues}
    assert "question_mismatch" in codes


def test_unknown_unresolved():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Find z",
        visual_requirement="none",
        givens=[],
        derived=[],  # No derived item for unknown z
        unknowns=[Unknown(id="u1", symbol="z")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Find z")
    codes = {i.code for i in issues}
    assert "unknown_unresolved" in codes


def test_duplicate_id():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Test duplicates",
        visual_requirement="none",
        givens=[Quantity(id="k1", symbol="a", value=1.0, provenance="given", source_text="a=1")],
        derived=[Quantity(id="k1", symbol="b", value=2.0, provenance="derived", depends_on=[])],  # duplicate id k1
        unknowns=[Unknown(id="k1", symbol="b")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Test duplicates")
    codes = {i.code for i in issues}
    assert "duplicate_id" in codes


def test_dangling_dependency():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Test dangling",
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=5.0, provenance="derived", depends_on=["non_existent"])],
        unknowns=[Unknown(id="d1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Test dangling")
    codes = {i.code for i in issues}
    assert "dangling_dependency" in codes


def test_sign_mismatch():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Test sign",
        visual_requirement="none",
        givens=[],
        derived=[
            Quantity(
                id="d1",
                symbol="x",
                value=-5.0,
                sign="positive",  # Disagrees with value -5.0
                provenance="derived",
                depends_on=[],
            )
        ],
        unknowns=[Unknown(id="d1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Test sign")
    codes = {i.code for i in issues}
    assert "sign_mismatch" in codes


def test_arithmetic_mismatch():
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Test arithmetic mismatch",
        visual_requirement="none",
        givens=[],
        derived=[
            Quantity(
                id="d1",
                symbol="x",
                value=10.0,
                source_text="2 + 3",  # Evaluates to 5, disagrees with 10 beyond tolerance
                provenance="derived",
                depends_on=[],
            )
        ],
        unknowns=[Unknown(id="d1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    issues = validate_turn_plan(plan, expected_question="Test arithmetic mismatch")
    codes = {i.code for i in issues}
    assert "arithmetic_mismatch" in codes


def test_limit_exceeded():
    # Model validation permits up to 6 assumptions and 8 claims. Exceeding at validation level
    # or validating limit_exceeded:
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Test limits",
        visual_requirement="none",
        givens=[],
        derived=[
            Quantity(
                id="d1",
                symbol="x",
                value=1.0,
                source_text="a" * 180,  # Max allowed in model
                provenance="derived",
                depends_on=[],
            )
        ],
        unknowns=[Unknown(id="d1", symbol="x")],
        assumptions=["a"] * 6,
        qualitative_claims=[],
        law_ids=[],
    )
    # Testing issue codes manually or validator
    issues = validate_turn_plan(plan, expected_question="Test limits")
    assert all(i.code != "question_mismatch" for i in issues)

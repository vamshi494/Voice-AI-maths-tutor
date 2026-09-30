# tests/test_problem_ir.py
import pytest
from app.agents.nodes.problem_ir import validate_problem_ir
from app.contracts.problem_ir import (
    BinaryNode,
    ConstantNode,
    EvaluateReq,
    Evidence,
    NumberNode,
    ProblemEntity,
    ProblemExpression,
    ProblemFact,
    ProblemIR,
    ResultBinding,
)
from app.contracts.solver import AuthorityAudit, SolvedValue, SolverResult
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.tutor.authority import apply_solver_override, verify_plan_against_solver


def make_test_fixture():
    q = "What is the area of a circle with radius 7?"
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question=q,
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="A", value=154.0, provenance="derived", source_text="pi * 7^2", depends_on=[])],
        unknowns=[Unknown(id="u1", symbol="A")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    quote = "radius 7"
    start = q.index(quote)
    end = start + len(quote)

    expr = ProblemExpression(
        id="expr1",
        value_type="scalar",
        root=BinaryNode(
            kind="binary",
            operator="*",
            left=NumberNode(kind="number", value=3.14159),
            right=BinaryNode(
                kind="binary",
                operator="^",
                left=NumberNode(kind="number", value=7.0),
                right=NumberNode(kind="number", value=2.0),
            ),
        ),
        evidence_fact_ids=["f1"],
    )

    ir = ProblemIR(
        schema_version="problem-ir/v1",
        id="ir1",
        question=q,
        facts=[
            ProblemFact(
                id="f1",
                kind="given",
                statement="radius is 7",
                evidence=Evidence(source="question", start=start, end=end, quote=quote),
            )
        ],
        entities=[ProblemEntity(id="e1", kind="circle", label="circle", evidence_fact_ids=["f1"])],
        expressions=[expr],
        constraints=[],
        representation_intents=[],
        solve_requests=[
            EvaluateReq(
                id="sr1",
                kind="evaluate",
                expression_id="expr1",
                result_binding=ResultBinding(turn_plan_quantity_id="d1", symbol="A", evidence_fact_ids=["f1"]),
            )
        ],
    )
    return q, plan, ir


def test_problem_ir_validation_valid():
    q, plan, ir = make_test_fixture()
    errors = validate_problem_ir(ir, q, plan)
    assert len(errors) == 0


def test_problem_ir_evidence_span_mismatch():
    q, plan, ir = make_test_fixture()
    ir.facts[0].evidence.quote = "wrong quote"
    errors = validate_problem_ir(ir, q, plan)
    assert any("Evidence quote mismatch" in e for e in errors)


def test_problem_ir_missing_fact_reference():
    q, plan, ir = make_test_fixture()
    ir.entities[0].evidence_fact_ids = ["f_missing"]
    errors = validate_problem_ir(ir, q, plan)
    assert any("references missing fact" in e for e in errors)


def test_problem_ir_binding_unknown_mismatch():
    q, plan, ir = make_test_fixture()
    ir.solve_requests[0].result_binding.turn_plan_quantity_id = "non_existent"
    errors = validate_problem_ir(ir, q, plan)
    assert any("not an unknown or derived quantity" in e for e in errors)


def test_authority_audit_tolerance_edges():
    q, plan, ir = make_test_fixture()

    # Case 1: Exact match
    solver_exact = SolverResult(status="exact_solved", values=[SolvedValue(request_id="sr1", exact="154", approximate=154.0, error_bound=0.0)])
    audit = verify_plan_against_solver(plan, ir, solver_exact)
    assert audit.status == "verified"
    assert audit.bindings[0].agreed is True

    # Case 2: Within 1e-4 tolerance
    solver_near = SolverResult(status="exact_solved", values=[SolvedValue(request_id="sr1", approximate=154.00005, error_bound=0.00001)])
    audit = verify_plan_against_solver(plan, ir, solver_near)
    assert audit.status == "verified"
    assert audit.bindings[0].agreed is True

    # Case 3: Beyond tolerance
    solver_diff = SolverResult(status="exact_solved", values=[SolvedValue(request_id="sr1", approximate=154.1, error_bound=0.001)])
    audit = verify_plan_against_solver(plan, ir, solver_diff)
    assert audit.status == "contradiction"
    assert audit.bindings[0].agreed is False


def test_authority_contradiction_override():
    q, plan, ir = make_test_fixture()
    solver_diff = SolverResult(status="exact_solved", values=[SolvedValue(request_id="sr1", exact="150", approximate=150.0, error_bound=0.0)])

    audit = verify_plan_against_solver(plan, ir, solver_diff)
    assert audit.status == "contradiction"

    proj = apply_solver_override(plan, ir, solver_diff)
    assert plan.derived[0].value == 150.0
    assert plan.derived[0].source_text is None
    assert proj["d1"] == "150"

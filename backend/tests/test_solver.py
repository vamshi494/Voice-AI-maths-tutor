# backend/tests/test_solver.py
import asyncio
import pytest
from app.contracts.problem_ir import (
    BinaryNode,
    ConstantNode,
    DefiniteIntegralReq,
    Domain,
    EvaluateReq,
    IntersectionsReq,
    NumberNode,
    ProblemExpression,
    ProblemFact,
    ProblemIR,
    RootsReq,
    VariableNode,
)
from app.solver import sympy_solver


@pytest.mark.asyncio
async def test_solver_evaluate_exact_and_async_rule():
    # MANDATORY TEST RULE: Calling sympy_solver.solve via asyncio.to_thread
    # Expression: 2 * pi * 7
    ir = ProblemIR(
        schema_version="problem-ir/v1",
        id="prob_eval",
        question="Find circumference",
        facts=[],
        entities=[],
        expressions=[
            ProblemExpression(
                id="e1",
                value_type="scalar",
                root=BinaryNode(
                    kind="binary",
                    operator="*",
                    left=BinaryNode(
                        kind="binary",
                        operator="*",
                        left=NumberNode(kind="number", value=2.0),
                        right=ConstantNode(kind="constant", name="pi"),
                    ),
                    right=NumberNode(kind="number", value=7.0),
                ),
                evidence_fact_ids=[],
            )
        ],
        constraints=[],
        representation_intents=[],
        solve_requests=[
            EvaluateReq(id="sr1", kind="evaluate", expression_id="e1")
        ],
    )

    res = await asyncio.wait_for(
        asyncio.to_thread(sympy_solver.solve, ir),
        timeout=2.0,
    )
    assert res.status == "exact_solved"
    assert len(res.values) == 1
    assert "14*pi" in res.values[0].exact
    assert pytest.approx(res.values[0].approximate, 0.01) == 43.982297


@pytest.mark.asyncio
async def test_solver_roots_within_domain():
    # Expression: x^2 - 9 = 0 over [0, 5] -> root is 3
    ir = ProblemIR(
        schema_version="problem-ir/v1",
        id="prob_roots",
        question="Find positive root of x^2 - 9",
        facts=[],
        entities=[],
        expressions=[
            ProblemExpression(
                id="e1",
                value_type="scalar",
                root=BinaryNode(
                    kind="binary",
                    operator="-",
                    left=BinaryNode(
                        kind="binary",
                        operator="^",
                        left=VariableNode(kind="variable", name="x"),
                        right=NumberNode(kind="number", value=2.0),
                    ),
                    right=NumberNode(kind="number", value=9.0),
                ),
                evidence_fact_ids=[],
            )
        ],
        constraints=[],
        representation_intents=[],
        solve_requests=[
            RootsReq(
                id="sr1",
                kind="roots",
                expression_id="e1",
                variable="x",
                domain=Domain(min=0.0, max=5.0),
            )
        ],
    )

    res = await asyncio.wait_for(
        asyncio.to_thread(sympy_solver.solve, ir),
        timeout=2.0,
    )
    assert res.status in ("exact_solved", "isolated_intervals")
    assert len(res.values) == 1
    assert pytest.approx(res.values[0].approximate, 1e-4) == 3.0


@pytest.mark.asyncio
async def test_solver_intersections():
    # y = 2x and y = x + 3 -> 2x = x + 3 -> x = 3 over [0, 5]
    ir = ProblemIR(
        schema_version="problem-ir/v1",
        id="prob_inter",
        question="Find intersection of 2x and x+3",
        facts=[],
        entities=[],
        expressions=[
            ProblemExpression(
                id="e1",
                value_type="scalar",
                root=BinaryNode(
                    kind="binary",
                    operator="*",
                    left=NumberNode(kind="number", value=2.0),
                    right=VariableNode(kind="variable", name="x"),
                ),
                evidence_fact_ids=[],
            ),
            ProblemExpression(
                id="e2",
                value_type="scalar",
                root=BinaryNode(
                    kind="binary",
                    operator="+",
                    left=VariableNode(kind="variable", name="x"),
                    right=NumberNode(kind="number", value=3.0),
                ),
                evidence_fact_ids=[],
            ),
        ],
        constraints=[],
        representation_intents=[],
        solve_requests=[
            IntersectionsReq(
                id="sr1",
                kind="intersections",
                left_expression_id="e1",
                right_expression_id="e2",
                variable="x",
                domain=Domain(min=0.0, max=5.0),
            )
        ],
    )

    res = await asyncio.wait_for(
        asyncio.to_thread(sympy_solver.solve, ir),
        timeout=2.0,
    )
    assert len(res.values) == 1
    assert pytest.approx(res.values[0].approximate, 1e-4) == 3.0


@pytest.mark.asyncio
async def test_solver_definite_integral():
    # integrate 3x^2 from 0 to 2 -> x^3 | 0..2 = 8
    ir = ProblemIR(
        schema_version="problem-ir/v1",
        id="prob_int",
        question="integrate 3x^2 dx from 0 to 2",
        facts=[],
        entities=[],
        expressions=[
            ProblemExpression(
                id="e1",
                value_type="scalar",
                root=BinaryNode(
                    kind="binary",
                    operator="*",
                    left=NumberNode(kind="number", value=3.0),
                    right=BinaryNode(
                        kind="binary",
                        operator="^",
                        left=VariableNode(kind="variable", name="x"),
                        right=NumberNode(kind="number", value=2.0),
                    ),
                ),
                evidence_fact_ids=[],
            )
        ],
        constraints=[],
        representation_intents=[],
        solve_requests=[
            DefiniteIntegralReq(
                id="sr1",
                kind="definite_integral",
                expression_id="e1",
                variable="x",
                lower=0.0,
                upper=2.0,
            )
        ],
    )

    res = await asyncio.wait_for(
        asyncio.to_thread(sympy_solver.solve, ir),
        timeout=2.0,
    )
    assert res.status == "exact_solved"
    assert len(res.values) == 1
    assert pytest.approx(res.values[0].approximate, 1e-4) == 8.0

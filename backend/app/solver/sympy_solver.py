# app/solver/sympy_solver.py
import math
from typing import Any
import sympy

from app.contracts.problem_ir import (
    BinaryNode,
    CallNode,
    ConstantNode,
    DefiniteIntegralReq,
    EvaluateReq,
    ExprNode,
    IntersectionsReq,
    NumberNode,
    ProblemIR,
    RootsReq,
    UnaryNode,
    VariableNode,
)
from app.contracts.solver import SolvedValue, SolverResult


def ast_node_to_sympy(node: ExprNode) -> Any:
    """Convert a ProblemIR AST ExprNode into a Sympy expression."""
    if isinstance(node, NumberNode):
        val = node.value
        if isinstance(val, (int, float)) and float(val).is_integer():
            return sympy.Integer(int(val))
        return sympy.nsimplify(val)
    if isinstance(node, ConstantNode):
        if node.name == "pi":
            return sympy.pi
        if node.name == "e":
            return sympy.E
        raise ValueError(f"Unknown constant: {node.name}")
    if isinstance(node, VariableNode):
        return sympy.Symbol(node.name)
    if isinstance(node, UnaryNode):
        op = ast_node_to_sympy(node.operand)
        if node.operator == "+":
            return +op
        if node.operator == "-":
            return -op
        raise ValueError(f"Unknown unary operator: {node.operator}")
    if isinstance(node, BinaryNode):
        left = ast_node_to_sympy(node.left)
        right = ast_node_to_sympy(node.right)
        if node.operator == "+":
            return left + right
        if node.operator == "-":
            return left - right
        if node.operator == "*":
            return left * right
        if node.operator == "/":
            return left / right
        if node.operator == "^":
            return left ** right
        raise ValueError(f"Unknown binary operator: {node.operator}")
    if isinstance(node, CallNode):
        arg = ast_node_to_sympy(node.argument)
        func_map = {
            "sin": sympy.sin,
            "cos": sympy.cos,
            "tan": sympy.tan,
            "asin": sympy.asin,
            "acos": sympy.acos,
            "atan": sympy.atan,
            "sqrt": sympy.sqrt,
            "abs": sympy.Abs,
            "exp": sympy.exp,
            "log": sympy.log,
            "ln": sympy.log,
        }
        fn = func_map.get(node.function)
        if fn is None:
            raise ValueError(f"Unknown call function: {node.function}")
        return fn(arg)
    raise ValueError(f"Unsupported AST node: {type(node)}")


def solve(ir: ProblemIR) -> SolverResult:
    """Synchronous solver function.

    MANDATORY CALLER RULE:
    Every call to this function MUST be wrapped with asyncio.to_thread, e.g.:
        result = await asyncio.wait_for(asyncio.to_thread(sympy_solver.solve, ir), timeout=2.0)
    Never call this synchronously from inside an async def.
    """
    try:
        expr_map = {expr.id: ast_node_to_sympy(expr.root) for expr in ir.expressions}
        solved_values: list[SolvedValue] = []
        all_exact = True

        for req in ir.solve_requests:
            if isinstance(req, EvaluateReq):
                expr = expr_map[req.expression_id]
                val = sympy.nsimplify(expr)
                try:
                    approx = float(val.evalf())
                    if not math.isfinite(approx):
                        return SolverResult(status="unsolvable", values=[])
                except Exception:
                    approx = None

                is_exact = not val.has(sympy.Float)
                if not is_exact:
                    all_exact = False
                error_bound = 0.0 if is_exact else (1e-9 * max(1.0, abs(approx or 0.0)))

                solved_values.append(
                    SolvedValue(
                        request_id=req.id,
                        exact=str(val) if is_exact else None,
                        approximate=approx,
                        error_bound=error_bound,
                    )
                )

            elif isinstance(req, RootsReq):
                expr = expr_map[req.expression_id]
                var = sympy.Symbol(req.variable)
                domain = sympy.Interval(req.domain.min, req.domain.max)
                roots_found: list[float] = []

                # Attempt algebraic solution first
                try:
                    sol_set = sympy.solveset(expr, var, domain=sympy.Reals)
                    if isinstance(sol_set, sympy.FiniteSet):
                        for s in sol_set:
                            s_val = float(s.evalf())
                            if req.domain.min <= s_val <= req.domain.max and math.isfinite(s_val):
                                roots_found.append(s_val)
                except Exception:
                    pass

                # Fallback to numerical solver with 20 seeds if no roots found algebraically
                if not roots_found and req.domain.max > req.domain.min:
                    step = (req.domain.max - req.domain.min) / 21.0
                    for k in range(1, 21):
                        seed = req.domain.min + k * step
                        try:
                            sol_num = sympy.nsolve(expr, var, seed)
                            s_val = float(sol_num)
                            if req.domain.min <= s_val <= req.domain.max and math.isfinite(s_val):
                                # Check if already added within tolerance
                                if not any(abs(s_val - r) < 1e-5 for r in roots_found):
                                    roots_found.append(s_val)
                        except Exception:
                            continue

                roots_found.sort()
                if not roots_found:
                    return SolverResult(status="unsolvable", values=[])

                primary_root = roots_found[0]
                all_exact = False
                solved_values.append(
                    SolvedValue(
                        request_id=req.id,
                        exact=None,
                        approximate=primary_root,
                        error_bound=1e-5,
                        roots=roots_found,
                    )
                )

            elif isinstance(req, IntersectionsReq):
                left_expr = expr_map[req.left_expression_id]
                right_expr = expr_map[req.right_expression_id]
                diff = left_expr - right_expr
                var = sympy.Symbol(req.variable)
                roots_found = []

                try:
                    sol_set = sympy.solveset(diff, var, domain=sympy.Reals)
                    if isinstance(sol_set, sympy.FiniteSet):
                        for s in sol_set:
                            s_val = float(s.evalf())
                            if req.domain.min <= s_val <= req.domain.max and math.isfinite(s_val):
                                roots_found.append(s_val)
                except Exception:
                    pass

                if not roots_found and req.domain.max > req.domain.min:
                    step = (req.domain.max - req.domain.min) / 21.0
                    for k in range(1, 21):
                        seed = req.domain.min + k * step
                        try:
                            sol_num = sympy.nsolve(diff, var, seed)
                            s_val = float(sol_num)
                            if req.domain.min <= s_val <= req.domain.max and math.isfinite(s_val):
                                if not any(abs(s_val - r) < 1e-5 for r in roots_found):
                                    roots_found.append(s_val)
                        except Exception:
                            continue

                roots_found.sort()
                if not roots_found:
                    return SolverResult(status="unsolvable", values=[])

                all_exact = False
                solved_values.append(
                    SolvedValue(
                        request_id=req.id,
                        exact=None,
                        approximate=roots_found[0],
                        error_bound=1e-5,
                        roots=roots_found,
                    )
                )

            elif isinstance(req, DefiniteIntegralReq):
                expr = expr_map[req.expression_id]
                var = sympy.Symbol(req.variable)
                sym_lower = sympy.nsimplify(req.lower)
                sym_upper = sympy.nsimplify(req.upper)
                integral_res = sympy.integrate(expr, (var, sym_lower, sym_upper))
                try:
                    approx = float(integral_res.evalf())
                    if not math.isfinite(approx):
                        return SolverResult(status="unsolvable", values=[])
                except Exception:
                    return SolverResult(status="unsolvable", values=[])

                is_exact = not integral_res.has(sympy.Float)
                if not is_exact:
                    all_exact = False

                solved_values.append(
                    SolvedValue(
                        request_id=req.id,
                        exact=str(integral_res) if is_exact else None,
                        approximate=approx,
                        error_bound=0.0 if is_exact else 1e-9 * max(1.0, abs(approx)),
                    )
                )

        status = "exact_solved" if all_exact else "isolated_intervals"
        return SolverResult(status=status, values=solved_values)

    except Exception:
        return SolverResult(status="unsolvable", values=[])

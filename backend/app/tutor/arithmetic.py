# app/tutor/arithmetic.py
import ast
import math
import re
from typing import Any
from app.contracts.turn_plan import Quantity, TurnPlan
from app.observability import log_event


class WhitelistEvaluator(ast.NodeVisitor):
    def __init__(self, var_map: dict[str, float]) -> None:
        self.var_map = var_map

    def visit_Expression(self, node: ast.Expression) -> float:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> float:
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    def visit_Num(self, node: Any) -> float:  # Python < 3.8 compatibility
        return float(node.n)


    def visit_Name(self, node: ast.Name) -> float:
        name = node.id
        if name == "pi":
            return math.pi
        if name == "e":
            return math.e
        if name in self.var_map:
            return float(self.var_map[name])
        raise ValueError(f"Unknown variable in arithmetic evaluation: {name}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> float:
        val = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        raise ValueError(f"Unsupported unary operator: {type(node.op)}")

    def visit_BinOp(self, node: ast.BinOp) -> float:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if abs(right) < 1e-12:
                raise ZeroDivisionError("Division by zero in arithmetic evaluation")
            return left / right
        if isinstance(node.op, ast.Pow):
            return left ** right
        raise ValueError(f"Unsupported binary operator: {type(node.op)}")

    def visit_Call(self, node: ast.Call) -> float:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple named function calls supported")
        fn_name = node.func.id
        if len(node.args) != 1:
            raise ValueError("Only single-argument functions supported")
        arg_val = self.visit(node.args[0])

        if fn_name == "sqrt":
            if arg_val < 0.0:
                raise ValueError("sqrt of negative number")
            return math.sqrt(arg_val)
        if fn_name == "abs":
            return abs(arg_val)
        # Trig functions take degrees
        if fn_name == "sin":
            return math.sin(math.radians(arg_val))
        if fn_name == "cos":
            return math.cos(math.radians(arg_val))
        if fn_name == "tan":
            return math.tan(math.radians(arg_val))
        raise ValueError(f"Unsupported function call: {fn_name}")

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"Disallowed AST node: {type(node)}")


def normalize_source_text(text: str) -> str:
    """Normalize arithmetic symbols in source text."""
    s = text.replace("×", "*").replace("÷", "/").replace("^", "**")
    # LaTeX math expressions
    s = re.sub(r"\\sqrt\{([^}]+)\}", r"sqrt(\1)", s)
    s = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"((\1)/(\2))", s)
    s = s.replace(r"\cdot", "*").replace(r"\times", "*")
    # Replace √x with sqrt(x) or √(expr) with sqrt(expr)
    s = re.sub(r"√\(([^)]+)\)", r"sqrt(\1)", s)
    s = re.sub(r"√([0-9a-zA-Z_]+)", r"sqrt(\1)", s)
    s = s.replace("π", "pi")
    # Remove comma thousands separators between digits (e.g. 1,000 -> 1000)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    return s



def eval_expr_part(part_str: str, var_map: dict[str, float]) -> float | None:
    part_str = part_str.strip()
    if not part_str:
        return None
    try:
        tree = ast.parse(part_str, mode="eval")
        evaluator = WhitelistEvaluator(var_map)
        val = evaluator.visit(tree)
        if math.isfinite(val):
            return float(val)
        return None
    except Exception:
        return None


def format_number(val: float) -> str:
    """Format float cleanly."""
    if float(val).is_integer():
        return str(int(val))
    return f"{val:.6g}"


def sort_quantities_by_dependency(quantities: list[Quantity]) -> list[Quantity]:
    """Topological sort of quantities by depends_on."""
    q_by_id = {q.id: q for q in quantities}
    visited: set[str] = set()
    order: list[Quantity] = []

    def visit(qid: str) -> None:
        if qid in visited:
            return
        visited.add(qid)
        q = q_by_id.get(qid)
        if q:
            for dep in q.depends_on:
                if dep in q_by_id:
                    visit(dep)
            order.append(q)

    for q in quantities:
        visit(q.id)
    return order


def reconcile_explicit_arithmetic(plan: TurnPlan) -> tuple[TurnPlan, bool]:
    """Reconcile explicit arithmetic in source_text.

    Evaluate each part of source_text in dependency order and correct the declared value
    in place. Returns (plan, reconciled_flag).
    """
    reconciled = False
    all_quantities = plan.givens + plan.derived
    sorted_quantities = sort_quantities_by_dependency(all_quantities)

    var_map: dict[str, float] = {}
    for q in all_quantities:
        if q.value is None:
            continue  # a missing value is not a variable binding (it may be filled below)
        var_map[q.id] = q.value
        if q.symbol:
            var_map[q.symbol] = q.value

    for q in sorted_quantities:
        if not q.source_text:
            continue

        normalized = normalize_source_text(q.source_text)
        if "solve" in normalized.lower() or "=>" in normalized:
            continue

        parts = normalized.split("=")

        # Find parts that contain an operator
        expr_parts = [p for p in parts if any(op in p for op in ("+", "-", "*", "/", "**", "sqrt", "sin", "cos", "tan"))]

        if expr_parts:
            last_expr_part = expr_parts[-1]
            # If the expression contains the variable itself, it's an equation constraint, not an arithmetic definition
            if (q.symbol and re.search(r"\b" + re.escape(q.symbol) + r"\b", last_expr_part)) or (
                q.id and re.search(r"\b" + re.escape(q.id) + r"\b", last_expr_part)
            ):
                continue

            e = eval_expr_part(last_expr_part, var_map)

            if e is not None and q.value is None:
                # The model wrote the arithmetic but omitted the number: fill it from its own
                # expression. This is not a correction, so `reconciled` stays False.
                q.value = e
                var_map[q.id] = e
                if q.symbol:
                    var_map[q.symbol] = e
                log_event("arithmetic_filled", quantity_id=q.id, value=e)
            elif e is not None:
                tol = max(1e-9, 1e-9 * abs(e))
                if abs(e - q.value) > tol:
                    old_val = q.value
                    q.value = e
                    var_map[q.id] = e
                    if q.symbol:
                        var_map[q.symbol] = e
                    reconciled = True
                    log_event("arithmetic_reconciled", quantity_id=q.id, old=old_val, new=e)

                    # If final part of source_text was a bare number, update it
                    final_part = parts[-1].strip()
                    try:
                        float(final_part)
                        # Rewrite final part
                        parts[-1] = f" {format_number(e)}"
                        q.source_text = "=".join(parts)
                    except ValueError:
                        pass

    return plan, reconciled

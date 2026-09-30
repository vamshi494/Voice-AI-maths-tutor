# app/contracts/solver.py
from typing import Any, Literal
from .base import CamelModel


class SolvedValue(CamelModel):
    request_id: str
    exact: str | None = None            # sympy srepr/str when exact
    approximate: float | None = None
    error_bound: float = 0.0
    roots: list[float] | None = None


class SolverResult(CamelModel):
    status: Literal["exact_solved", "isolated_intervals", "unsolvable"]
    values: list[SolvedValue]


class AuthorityBinding(CamelModel):
    quantity_id: str
    plan_value: float
    solver_value: float
    agreed: bool


class AuthorityAudit(CamelModel):
    status: Literal["verified", "incomplete", "contradiction"]
    issues: list[dict[str, Any]]           # {code, message}
    bindings: list[AuthorityBinding]

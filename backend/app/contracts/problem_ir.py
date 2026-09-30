# app/contracts/problem_ir.py
from typing import Annotated, Any, Literal, Union
from pydantic import Field, field_validator, model_validator
from .base import LLMModel as CamelModel


class Evidence(CamelModel):
    source: Literal["question"] = "question"
    start: int = 0
    end: int = 0
    quote: str = ""                                 # MUST equal question[start:end]


class ProblemFact(CamelModel):
    id: str
    kind: Literal["given", "requested", "assumption"]
    statement: str
    evidence: Evidence

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, v: Any) -> str:
        s = str(v).lower()
        if s in ("derived", "inferred", "deduced"):
            return "assumption"
        return s


EntityKind = Literal[   # DEVIATION: CBSE maths enum
    "point", "segment", "line", "ray", "angle", "triangle", "quadrilateral", "polygon",
    "circle", "arc", "chord", "tangent", "region", "curve", "solid",
    "number_line", "axis", "other",
]


class ProblemEntity(CamelModel):
    id: str
    kind: EntityKind
    label: str | None = None
    evidence_fact_ids: list[str]


class NumberNode(CamelModel):
    kind: Literal["number"]
    value: float


class ConstantNode(CamelModel):
    kind: Literal["constant"]
    name: Literal["pi", "e"]


class VariableNode(CamelModel):
    kind: Literal["variable"]
    name: str


class UnaryNode(CamelModel):
    kind: Literal["unary"]
    operator: Literal["+", "-"]
    operand: "ExprNode"


class BinaryNode(CamelModel):
    kind: Literal["binary"]
    operator: Literal["+", "-", "*", "/", "^"]
    left: "ExprNode"
    right: "ExprNode"

    @model_validator(mode="before")
    @classmethod
    def normalize_binary(cls, v: Any) -> Any:
        if isinstance(v, dict):
            v = dict(v)
            raw_op = v.pop("op", None)
            op = v.get("operator") or raw_op
            op_map = {
                "mul": "*", "times": "*", "mult": "*",
                "add": "+", "plus": "+",
                "sub": "-", "minus": "-",
                "div": "/", "divide": "/",
                "pow": "^", "power": "^",
            }
            if op in op_map:
                op = op_map[op]
            if op:
                v["operator"] = op
        return v


class CallNode(CamelModel):
    kind: Literal["call"]
    function: Literal["sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "exp", "log", "ln"]
    argument: "ExprNode"


ExprNode = Annotated[
    Union[NumberNode, ConstantNode, VariableNode, UnaryNode, BinaryNode, CallNode],
    Field(discriminator="kind"),
]
for _m in (UnaryNode, BinaryNode, CallNode):
    _m.model_rebuild()


class ProblemExpression(CamelModel):
    id: str
    value_type: Literal["scalar", "function"]
    root: ExprNode
    evidence_fact_ids: list[str]


class EquationConstraint(CamelModel):
    id: str
    kind: Literal["equation"]
    left_expression_id: str
    right_expression_id: str
    evidence_fact_ids: list[str]


class InequalityConstraint(CamelModel):
    id: str
    kind: Literal["inequality"]
    left_expression_id: str
    relation: Literal["<", "<=", ">", ">="]
    right_expression_id: str
    evidence_fact_ids: list[str]


class RelationConstraint(CamelModel):
    id: str
    kind: Literal[  # DEVIATION: CBSE additions after "symmetric"
        "incident", "parallel", "perpendicular", "tangent", "inside", "connected", "symmetric",
        "midpoint", "bisects", "congruent", "similar", "equal_length", "equal_angle", "collinear",
    ]
    entity_ids: list[str]
    evidence_fact_ids: list[str]


ProblemConstraint = Annotated[
    Union[EquationConstraint, InequalityConstraint, RelationConstraint],
    Field(discriminator="kind"),
]


class RepresentationIntent(CamelModel):
    id: str
    kind: Literal[  # DEVIATION: CBSE enum
        "geometric_figure", "graph", "number_line", "bounded_region", "solid", "table", "conceptual",
    ]
    entity_ids: list[str]
    evidence_fact_ids: list[str]


class ResultBinding(CamelModel):
    turn_plan_quantity_id: str
    symbol: str
    unit: str | None = None
    evidence_fact_ids: list[str]


class Domain(CamelModel):
    min: float
    max: float


class EvaluateReq(CamelModel):
    id: str
    kind: Literal["evaluate"]
    expression_id: str
    result_binding: ResultBinding | None = None


class RootsReq(CamelModel):
    id: str
    kind: Literal["roots"]
    expression_id: str
    variable: str
    domain: Domain
    result_binding: ResultBinding | None = None


class IntersectionsReq(CamelModel):
    id: str
    kind: Literal["intersections"]
    left_expression_id: str
    right_expression_id: str
    variable: str
    domain: Domain
    result_binding: ResultBinding | None = None


class DefiniteIntegralReq(CamelModel):
    id: str
    kind: Literal["definite_integral"]
    expression_id: str
    variable: str
    lower: float
    upper: float
    result_binding: ResultBinding | None = None


SolveRequest = Annotated[
    Union[EvaluateReq, RootsReq, IntersectionsReq, DefiniteIntegralReq],
    Field(discriminator="kind"),
]


class ProblemIR(CamelModel):
    schema_version: Literal["problem-ir/v1", "v1", "1"] = "problem-ir/v1"

    @field_validator("schema_version", mode="before")
    @classmethod
    def normalize_schema_version(cls, v: Any) -> str:
        if v in ("v1", "1", "problem-ir/v1"):
            return "problem-ir/v1"
        return str(v)
    @field_validator("solve_requests", mode="before")
    @classmethod
    def normalize_solve_requests(cls, v: Any) -> Any:
        if isinstance(v, list):
            normalized = []
            for item in v:
                if isinstance(item, dict):
                    item = dict(item)
                    if "kind" not in item:
                        k = item.get("solveFor") or item.get("type") or "evaluate"
                        if k not in ("evaluate", "roots", "intersections", "definite_integral"):
                            k = "evaluate"
                        item["kind"] = k
                    if "expressionId" not in item and "expression_id" not in item:
                        item["expressionId"] = item.get("target") or item.get("id") or "expr1"
                    if "id" not in item:
                        item["id"] = f"req_{len(normalized)+1}"
                    if "target" in item:
                        target_val = item.pop("target")
                        if "expressionId" not in item and "expression_id" not in item:
                            item["expressionId"] = target_val
                normalized.append(item)
            return normalized
        return v
    id: str
    question: str
    facts: list[ProblemFact] = Field(default_factory=list)
    entities: list[ProblemEntity] = Field(default_factory=list)
    expressions: list[ProblemExpression] = Field(default_factory=list)
    constraints: list[ProblemConstraint] = Field(default_factory=list)
    representation_intents: list[RepresentationIntent] = Field(default_factory=list)
    solve_requests: list[SolveRequest] = Field(default_factory=list)

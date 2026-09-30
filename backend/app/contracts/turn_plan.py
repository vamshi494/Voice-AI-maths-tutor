# app/contracts/turn_plan.py
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .base import LLMModel, coerce_model_list, coerce_number, coerce_str_list

Provenance = Literal["given", "derived", "assumed"]
Sign = Literal["positive", "negative", "zero", "unsigned"]


class Quantity(LLMModel):
    id: str
    symbol: str = ""
    value: float | None = None
    unit: str | None = None
    sign: Sign | None = None
    # No max_length: an over-long sourceText is truncated with a warning, never fatal.
    # plan_validation truncates to 180 and records limit_exceeded.
    source_text: str | None = None
    provenance: Provenance = "given"
    depends_on: list[str] = Field(default_factory=list)
    uncertainty: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # LLMs commonly write "name"/"label" for the id-less symbol and "computation"
            # (the word used in the TurnPlan prompt itself) for sourceText.
            if not data.get("id") and (data.get("symbol") or data.get("name")):
                data["id"] = str(data.get("symbol") or data.get("name"))
            if not data.get("sourceText") and not data.get("source_text") and data.get("computation"):
                data["source_text"] = str(data["computation"])
        return data

    @field_validator("id", "symbol", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: Any) -> float | None:
        return coerce_number(v)

    @field_validator("uncertainty", mode="before")
    @classmethod
    def _uncertainty(cls, v: Any) -> float | None:
        try:
            return coerce_number(v)
        except ValueError:
            return None

    @field_validator("unit", "source_text", mode="before")
    @classmethod
    def _opt_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sign", mode="before")
    @classmethod
    def _sign(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip().lower()
        return {"+": "positive", "-": "negative", "0": "zero", "pos": "positive", "neg": "negative"}.get(
            s, s if s in ("positive", "negative", "zero", "unsigned") else None
        )

    @field_validator("provenance", mode="before")
    @classmethod
    def _prov(cls, v: Any) -> str:
        s = str(v or "given").strip().lower()
        return s if s in ("given", "derived", "assumed") else ("assumed" if "assum" in s else "given")

    @field_validator("depends_on", mode="before")
    @classmethod
    def _deps(cls, v: Any) -> list[str]:
        return coerce_str_list(v)


class Unknown(LLMModel):
    id: str
    symbol: str = ""
    unit: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _ids(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"id": data, "symbol": data}
        if isinstance(data, dict) and not data.get("id") and data.get("symbol"):
            data["id"] = str(data["symbol"])
        return data


class QualitativeClaim(LLMModel):
    id: str = ""
    claim: str = ""                           # stable key, e.g. "triangle_is_right_angled"
    expected: bool | str | float = True
    related_quantity_ids: list[str] = Field(default_factory=list)
    related_entity_hints: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"id": data[:40], "claim": data}
        return data

    @field_validator("related_quantity_ids", "related_entity_hints", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return coerce_str_list(v)


class TurnPlan(LLMModel):
    schema_version: Literal["turn-plan/v3"] = "turn-plan/v3"

    @field_validator("schema_version", mode="before")
    @classmethod
    def normalize_schema_version(cls, v: Any) -> str:
        return "turn-plan/v3"

    question: str = ""
    givens: list[Quantity] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    derived: list[Quantity] = Field(default_factory=list)
    qualitative_claims: list[QualitativeClaim] = Field(default_factory=list)
    law_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    visual_requirement: Literal["required", "optional", "none"] = "optional"
    teaching_sequence_hints: list[str] | None = None

    @field_validator("givens", "unknowns", "derived", "qualitative_claims", mode="before")
    @classmethod
    def _model_lists(cls, v: Any) -> Any:
        return coerce_model_list(v)

    @field_validator("law_ids", "assumptions", mode="before")
    @classmethod
    def _str_lists(cls, v: Any) -> list[str]:
        # Assumptions sometimes arrive as objects ({"text": "..."}) despite the prompt.
        if isinstance(v, list):
            out = []
            for x in v:
                if isinstance(x, dict):
                    x = x.get("text") or x.get("assumption") or x.get("id") or json_dump(x)
                if x is not None and str(x).strip():
                    out.append(str(x).strip())
            return out
        return coerce_str_list(v)

    @field_validator("teaching_sequence_hints", mode="before")
    @classmethod
    def _hints(cls, v: Any) -> list[str] | None:
        return None if v is None else coerce_str_list(v)

    @field_validator("visual_requirement", mode="before")
    @classmethod
    def _visual(cls, v: Any) -> str:
        s = str(v or "optional").strip().lower()
        if s in ("required", "require", "yes", "true", "mandatory", "needed"):
            return "required"
        if s in ("none", "no", "false", "not_required", "not required", "n/a"):
            return "none"
        return "optional"

    @field_validator("question", mode="before")
    @classmethod
    def _q(cls, v: Any) -> str:
        return "" if v is None else str(v)


def json_dump(x: Any) -> str:
    import json
    return json.dumps(x, ensure_ascii=False)


class PlanIssue(LLMModel):
    code: Literal[
        "schema", "question_mismatch", "unknown_unresolved", "non_finite",
        "dangling_dependency", "duplicate_id", "sign_mismatch",
        "arithmetic_mismatch", "limit_exceeded",
    ]
    path: str
    message: str


NUMERIC_CONTRADICTION_CODES = {"arithmetic_mismatch", "sign_mismatch"}

# Codes that make a plan untrustworthy for TEACHING NUMBERS: a contradicting number
# is the one error the pipeline treats as fatal. Everything else is repaired in
# place and reported as a warning.
FATAL_PLAN_CODES = {"unknown_unresolved", "non_finite", "arithmetic_mismatch", "sign_mismatch"}

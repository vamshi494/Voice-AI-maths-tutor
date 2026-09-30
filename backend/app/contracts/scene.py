# app/contracts/scene.py
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .base import CamelModel, LLMModel, coerce_dict, coerce_model_list, coerce_str_list
from .turn_plan import Quantity


class SceneEntity(LLMModel):
    id: str
    kind: str = "other"
    role: str | None = None
    label: str | None = None
    visible: bool = True

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"id": data}
        return data

    @field_validator("id", "kind", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return "other" if v is None else str(v)

    @field_validator("label", "role", mode="before")
    @classmethod
    def _opt(cls, v: Any) -> str | None:
        return None if v is None or str(v).strip() == "" else str(v)


class SceneConstruction(LLMModel):
    id: str
    operator: str                 # must be in the capability manifest
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "operator" not in data:
                for k in ("op", "type", "kind"):
                    if k in data:
                        data["operator"] = data.pop(k)
                        break
            if "inputs" not in data:
                for k in ("args", "params", "input"):
                    if k in data:
                        data["inputs"] = data.pop(k)
                        break
            if "outputs" not in data and "output" in data:
                data["outputs"] = data.pop("output")
            if not data.get("id"):
                data["id"] = f"c_{uuid4().hex[:6]}"
        return data

    @field_validator("inputs", mode="before")
    @classmethod
    def _inputs(cls, v: Any) -> dict[str, Any]:
        return coerce_dict(v)

    @field_validator("outputs", mode="before")
    @classmethod
    def _outputs(cls, v: Any) -> list[str]:
        return coerce_str_list(v)

    @field_validator("operator", mode="before")
    @classmethod
    def _op(cls, v: Any) -> str:
        return str(v or "").strip().lower()


class SceneRelation(LLMModel):
    id: str = Field(default_factory=lambda: f"rel_{uuid4().hex[:6]}")
    type: str = "relation"
    entity_ids: list[str] = Field(default_factory=list)
    params: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_relation(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if not data.get("id"):
                data["id"] = f"rel_{uuid4().hex[:6]}"
            if "entity_ids" not in data and "entityIds" not in data and "entities" in data:
                data["entity_ids"] = data.pop("entities")
        return data

    @field_validator("entity_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return coerce_str_list(v)


class SceneAssertion(LLMModel):
    id: str = Field(default_factory=lambda: f"as_{uuid4().hex[:6]}")
    predicate: str                # must be in capability manifest
    entities: list[str] = Field(default_factory=list)
    expected: Any | None = None
    severity: Literal["error", "warning"] = "error"

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "entities" not in data:
                for k in ("entityIds", "entity_ids", "args", "targets"):
                    if k in data:
                        data["entities"] = data.pop(k)
                        break
            if not data.get("id"):
                data["id"] = f"as_{uuid4().hex[:6]}"
        return data

    @field_validator("entities", mode="before")
    @classmethod
    def _ents(cls, v: Any) -> list[str]:
        return coerce_str_list(v)

    @field_validator("predicate", mode="before")
    @classmethod
    def _pred(cls, v: Any) -> str:
        return str(v or "").strip().lower()

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, v: Any) -> str:
        if str(v).lower() in ("warning", "warn", "info", "notice"):
            return "warning"
        return "error"


class SceneAnnotation(LLMModel):
    id: str = Field(default_factory=lambda: f"an_{uuid4().hex[:6]}")
    kind: Literal["label", "callout", "caption"] = "label"
    target_ids: list[str] = Field(default_factory=list)
    text: str | None = None
    placement_intent: str | None = None
    quantity_id: str | None = None   # annotation bound to a DERIVED quantity -> deferred

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "target_ids" not in data and "targetIds" not in data:
                for k in ("targets", "target", "entityIds", "entity_ids", "targetId"):
                    if k in data:
                        data["target_ids"] = data.pop(k)
                        break
            if not data.get("id"):
                data["id"] = f"an_{uuid4().hex[:6]}"
        return data

    @field_validator("target_ids", mode="before")
    @classmethod
    def _t(cls, v: Any) -> list[str]:
        return coerce_str_list(v)

    @field_validator("text", "quantity_id", "placement_intent", mode="before")
    @classmethod
    def _opt(cls, v: Any) -> str | None:
        return None if v is None or str(v).strip() == "" else str(v)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: Any) -> str:
        s = str(v).lower()
        if s in ("label", "callout", "caption"):
            return s
        return "label"                # dimension / measure / value / anything else


class SceneRevealGroup(LLMModel):
    id: str = Field(default_factory=lambda: f"rg_{uuid4().hex[:6]}")
    entity_ids: list[str] = Field(default_factory=list)
    label: str | None = None

    @field_validator("entity_ids", mode="before")
    @classmethod
    def _e(cls, v: Any) -> list[str]:
        return coerce_str_list(v)


class TimelineAction(LLMModel):
    action: str = ""
    target_id: str = ""
    step_index: int | None = None


class SceneDocument(LLMModel):
    schema_version: Literal["scene-document/v2"] = "scene-document/v2"

    @field_validator("schema_version", mode="before")
    @classmethod
    def normalize_schema_version(cls, v: Any) -> str:
        return "scene-document/v2"

    visual_decision: Literal["scene", "text_only"] = "scene"
    source: str = ""
    quantities: list[Quantity] = Field(default_factory=list)
    entities: list[SceneEntity] = Field(default_factory=list)
    constructions: list[SceneConstruction] = Field(default_factory=list)
    relations: list[SceneRelation] = Field(default_factory=list)
    assertions: list[SceneAssertion] = Field(default_factory=list)
    annotations: list[SceneAnnotation] = Field(default_factory=list)
    required_entity_ids: list[str] = Field(default_factory=list)
    reveal_groups: list[SceneRevealGroup] = Field(default_factory=list)
    teaching_timeline: list[TimelineAction] | None = None

    @field_validator("visual_decision", mode="before")
    @classmethod
    def _vd(cls, v: Any) -> str:
        s = str(v or "scene").strip().lower()
        return "text_only" if s in ("text_only", "text-only", "text", "none") else "scene"

    @field_validator("quantities", "entities", "constructions", "relations", "assertions",
                     "annotations", "reveal_groups", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return coerce_model_list(v)

    @field_validator("required_entity_ids", mode="before")
    @classmethod
    def _req(cls, v: Any) -> list[str]:
        return coerce_str_list(v)

    @field_validator("teaching_timeline", mode="before")
    @classmethod
    def _tl(cls, v: Any) -> Any:
        return None if v is None else coerce_model_list(v)

    @field_validator("source", mode="before")
    @classmethod
    def _src(cls, v: Any) -> str:
        return "" if v is None else str(v)


# Server-produced (compiler output) — strict.
class RepairError(CamelModel):
    code: str
    message: str
    entity_ids: list[str] = Field(default_factory=list)
    severity: Literal["error", "warning"]


class ValidationReport(CamelModel):
    valid: bool
    errors: list[RepairError]
    warnings: list[RepairError]
    evaluated_assertions: int
    passed_assertions: int

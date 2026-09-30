# app/contracts/lesson.py
"""LLM contracts for the chapter outline.

LLM trust level: `extra="ignore"`, every field coerced. The outline
declares the whole lesson (scope, title, pages); each page declares its blocks. Clamps are
deterministic and never fatal: an outline that drifts (9 pages, 5 blocks, budget 42, extra
stickies) is clamped into the configured bounds, not rejected.
"""
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.config import settings

from .base import LLMModel, coerce_model_list, coerce_number, coerce_str_list


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return v is True or v == 1


class BlockIntent(LLMModel):
    """One visual block the outline asks for on a page."""

    id: str = ""
    role: Literal["figure", "table", "text"] = "figure"
    brief: str = ""
    sticky: bool = False
    text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            role = str(d.get("role") or "figure").strip().lower()
            d["role"] = role if role in ("figure", "table", "text") else "figure"
            if d.get("brief") is None:
                d["brief"] = d.get("description") or ""
            if "sticky" in d:
                d["sticky"] = _truthy(d.get("sticky"))
            return d
        return data

    @field_validator("id", "brief", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("text", mode="before")
    @classmethod
    def _opt_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v)
        return s if s.strip() else None


class PagePlan(LLMModel):
    """One page of the outline: teaching intent, not pixels."""

    title: str = ""
    objective: str = ""
    key_points: list[str] = Field(default_factory=list)
    numeric_task: str | None = None
    blocks: list[BlockIntent] = Field(default_factory=list)
    step_budget: int = settings.STEP_BUDGET_PAGE_DEFAULT

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "keyPoints" in d and "key_points" not in d:
                d["key_points"] = d.pop("keyPoints")
            if "numericTask" in d and "numeric_task" not in d:
                d["numeric_task"] = d.pop("numericTask")
            if "stepBudget" in d and "step_budget" not in d:
                d["step_budget"] = d.pop("stepBudget")
            d["key_points"] = coerce_str_list(d.get("key_points"))
            d["blocks"] = coerce_model_list(d.get("blocks"))
            if d.get("numeric_task") is not None:
                s = str(d.get("numeric_task") or "").strip()
                d["numeric_task"] = s or None
            if d.get("step_budget") is not None:
                try:
                    d["step_budget"] = coerce_number(d.get("step_budget"))
                except ValueError:
                    d["step_budget"] = None
            d["title"] = "" if d.get("title") is None else str(d.get("title")).strip()
            d["objective"] = "" if d.get("objective") is None else str(d.get("objective")).strip()
            return d
        return data

    @model_validator(mode="after")
    def _clamp(self) -> "PagePlan":
        self.key_points = [k for k in self.key_points if k][:5]
        self.blocks = self.blocks[: settings.PLANNER_BLOCK_BUDGET]
        budget = self.step_budget if isinstance(self.step_budget, (int, float)) else settings.STEP_BUDGET_PAGE_DEFAULT
        self.step_budget = min(settings.STEP_BUDGET_PAGE_MAX,
                               max(settings.STEP_BUDGET_PAGE_MIN, int(budget or settings.STEP_BUDGET_PAGE_DEFAULT)))
        # The LLM sometimes omits a block id or repeats one. Derive a stable per-page fallback
        # so two blocks never collide in the layout engine's id-keyed maps.
        used: set[str] = set()
        for i, block in enumerate(self.blocks):
            bid = (block.id or "").strip()
            if not bid or bid in used:
                bid = f"{block.role}_{i + 1}"
                while bid in used:
                    bid += "_"
            block.id = bid
            used.add(bid)
        return self


class LessonPlan(LLMModel):
    """The whole outlined lesson: page list + lesson-level sticky clamp."""

    scope: Literal["problem", "topic"] = "problem"
    title: str = ""
    pages: list[PagePlan] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            scope = str(d.get("scope") or "problem").strip().lower()
            d["scope"] = scope if scope in ("problem", "topic") else "problem"
            d["pages"] = coerce_model_list(d.get("pages"))
            d["title"] = "" if d.get("title") is None else str(d.get("title")).strip()
        return data

    @model_validator(mode="after")
    def _clamp(self) -> "LessonPlan":
        self.pages = self.pages[: settings.MAX_CHAPTER_PAGES]
        # At most MAX_STICKY_BLOCKS stickies per lesson; the rest teach normally.
        stickies = 0
        for page in self.pages:
            for block in page.blocks:
                block.sticky = bool(block.sticky) and stickies < settings.MAX_STICKY_BLOCKS
                if block.sticky:
                    stickies += 1
        if self.scope == "topic" and not self.pages:
            self.scope = "problem"          # a topic with zero pages is treated as a problem
        return self

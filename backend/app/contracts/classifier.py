# app/contracts/classifier.py
from typing import Any, Literal

from pydantic import field_validator

from .base import LLMModel

InterruptLabel = Literal[
    "backchannel", "affirmation", "doubt", "new_question", "end_session", "off_topic",
]
_LABELS = ("backchannel", "affirmation", "doubt", "new_question", "end_session", "off_topic")


class InterruptDecision(LLMModel):
    label: InterruptLabel

    @field_validator("label", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> str:
        s = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
        if s in _LABELS:
            return s
        for lbl in _LABELS:           # "new_question." / "label: doubt"
            if lbl in s:
                return lbl
        if "end" in s or "stop" in s:
            return "end_session"
        return "doubt"                # safe tie-break: never lose a doubt


class FigureNeedDecision(LLMModel):
    requires_new_figure: bool = False
    reason: str = ""

    @field_validator("requires_new_figure", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "required")
        return bool(v)

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> str:
        return str(v or "")[:200]

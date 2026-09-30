# app/contracts/base.py
"""Contract base classes.

Three trust levels, three base classes:

* ``CamelModel``   – objects the SERVER produces (wire events, diagrams, board ops).
                     ``extra="forbid"`` so our own contract drift fails loudly in tests.
* ``LLMModel``     – objects an LLM produces (TurnPlan, ProblemIR, SceneDocument, classifier
                     output). ``extra="ignore"``: an LLM adding a harmless key such as
                     ``"notes"`` must never nullify a whole plan or scene. Type drift is
                     coerced by the helpers below, never by loosening a field to ``Any``.
* ``WireInModel``  – objects the BROWSER sends (RPC payloads, reports). ``extra="ignore"``
                     so a newer client field never drops a student's doubt on the floor.
"""
import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Server-produced JSON uses camelCase on the wire; Python uses snake_case."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class LLMModel(BaseModel):
    """LLM-produced JSON: camelCase aliases, snake_case accepted, unknown keys ignored."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class WireInModel(BaseModel):
    """Browser-produced JSON: camelCase aliases, unknown keys ignored."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


# ---------------------------------------------------------------------------
# Coercion helpers shared by every LLM-facing contract
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def coerce_number(v: Any) -> float | None:
    """'5 cm' -> 5.0, '3/4' -> 0.75, '1,000' -> 1000.0, True -> None, None/'' -> None.

    Raises ValueError only for a non-finite result, which is a real contradiction.
    Returns None for text that carries no number (e.g. 'unknown'), which callers treat
    as "value missing" rather than as a schema failure.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        val = float(v)
    else:
        s = str(v).strip().replace(",", "").replace("−", "-")
        if not s:
            return None
        frac = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*", s)
        if frac and float(frac.group(2)) != 0:
            val = float(frac.group(1)) / float(frac.group(2))
        else:
            m = _NUM_RE.search(s)
            if not m:
                return None
            val = float(m.group(0))
    if not math.isfinite(val):
        raise ValueError("non_finite")
    return val


def coerce_str_list(v: Any) -> list[str]:
    """None -> [], 'a, b' -> ['a','b'], '["a","b"]' -> ['a','b'], ['a', 3] -> ['a','3']."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except ValueError:
                pass
        return [p.strip() for p in re.split(r"[,;]", s) if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return [str(v)]


def coerce_dict(v: Any) -> dict[str, Any]:
    """None -> {}, '{"x":1}' (stringified JSON) -> {'x': 1}, dict -> dict."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return {}
    return {}


def coerce_model_list(v: Any) -> Any:
    """A stringified JSON array for a list-of-objects field -> the parsed list; None -> []."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
        return []
    if isinstance(v, dict):
        return [v]
    return v

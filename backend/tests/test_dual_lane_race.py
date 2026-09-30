# tests/test_dual_lane_race.py
import asyncio
from typing import Any
import pytest
from app.config import settings
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.gateway.groq_client import GroqGateway
from app.agents.nodes.turn_plan import plan_turn


def make_valid_plan(q: str, val: float = 2.0) -> TurnPlan:
    return TurnPlan(
        schema_version="turn-plan/v3",
        question=q,
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=val, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],

        qualitative_claims=[],
        law_ids=[],
    )


class ScriptedGateway:
    """Mock gateway with per-prompt scripted responses and delays."""

    def __init__(self, responses: dict[str, tuple[Any, float]]):
        # responses: prompt_key -> (result_or_callable, delay_seconds)
        self.responses = responses
        self.call_log: list[str] = []

    async def complete_json(
        self,
        prompt_key: str,
        user: str,
        schema: Any,
        model: str,
        timeout_s: float | None = None,
        fmt_args: dict[str, Any] | None = None,
    ) -> Any:
        self.call_log.append(prompt_key)
        if prompt_key not in self.responses:
            return None
        res, delay = self.responses[prompt_key]
        if delay > 0:
            await asyncio.sleep(delay)
        return res


@pytest.mark.asyncio
async def test_single_lane_toggle_success(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", False)
    q = "What is x?"
    plan = make_valid_plan(q, 2.0)

    gw = ScriptedGateway({
        "turn_plan.primary.v1": (plan, 0.0),
        "turn_plan.retry.v1": (None, 0.0),
    })

    result = await plan_turn(q, gw=gw)
    assert result is not None
    assert result.derived[0].value == 2.0
    # Retry was not called
    assert "turn_plan.retry.v1" not in gw.call_log


@pytest.mark.asyncio
async def test_single_lane_toggle_fallback_to_retry(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", False)
    q = "What is x?"
    retry_plan = make_valid_plan(q, 3.0)

    gw = ScriptedGateway({
        "turn_plan.primary.v1": (None, 0.0),
        "turn_plan.retry.v1": (retry_plan, 0.0),
    })

    result = await plan_turn(q, gw=gw)
    assert result is not None
    assert result.derived[0].value == 3.0
    assert "turn_plan.primary.v1" in gw.call_log
    assert "turn_plan.retry.v1" in gw.call_log


@pytest.mark.asyncio
async def test_dual_lane_both_inside_grace(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", True)
    monkeypatch.setattr(settings, "TURN_PLAN_PEER_GRACE_MS", 100)
    q = "What is x?"
    plan_a = make_valid_plan(q, 2.0)
    plan_b = make_valid_plan(q, 2.0)

    # Primary finishes at 0.01s, retry at 0.03s (well within 100ms grace)
    gw = ScriptedGateway({
        "turn_plan.primary.v1": (plan_a, 0.01),
        "turn_plan.retry.v1": (plan_b, 0.03),
    })

    result = await plan_turn(q, gw=gw)
    assert result is not None
    assert result.derived[0].value == 2.0
    assert "turn_plan.primary.v1" in gw.call_log
    assert "turn_plan.retry.v1" in gw.call_log


@pytest.mark.asyncio
async def test_dual_lane_peer_outside_grace(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", True)
    monkeypatch.setattr(settings, "TURN_PLAN_PEER_GRACE_MS", 30)  # 30ms grace
    q = "What is x?"
    plan_a = make_valid_plan(q, 5.0)
    plan_b = make_valid_plan(q, 0.20)

    # Primary finishes fast (0.01s), peer is very slow (0.2s, > 30ms grace)
    gw = ScriptedGateway({
        "turn_plan.primary.v1": (plan_a, 0.01),
        "turn_plan.retry.v1": (plan_b, 0.20),
    })

    result = await plan_turn(q, gw=gw)
    assert result is not None
    assert result.derived[0].value == 5.0  # Chose plan_a since peer timed out


@pytest.mark.asyncio
async def test_dual_lane_both_invalid_falls_back_to_retry(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", True)
    q = "What is x?"
    retry_plan = make_valid_plan(q, 7.0)

    calls = {"retry_count": 0}

    class FailingDualGateway:
        def __init__(self):
            self.call_log = []

        async def complete_json(self, prompt_key, user, schema, model, timeout_s=None, fmt_args=None):
            self.call_log.append(prompt_key)
            if prompt_key == "turn_plan.primary.v1":
                return None
            if prompt_key == "turn_plan.retry.v1":
                calls["retry_count"] += 1
                if calls["retry_count"] == 1:
                    return None  # first retry fails in race
                return retry_plan  # second retry (the fallback) succeeds

    dual_gw = FailingDualGateway()
    result = await plan_turn(q, gw=dual_gw)
    assert result is not None
    assert result.derived[0].value == 7.0
    assert calls["retry_count"] == 2


@pytest.mark.asyncio
async def test_dual_lane_both_fail_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "TURN_PLAN_DUAL_LANE", True)
    q = "What is x?"

    gw = ScriptedGateway({
        "turn_plan.primary.v1": (None, 0.01),
        "turn_plan.retry.v1": (None, 0.01),
    })

    result = await plan_turn(q, gw=gw)
    assert result is None

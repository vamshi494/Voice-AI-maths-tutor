# backend/tests/test_gateway_reasoning.py
"""Reasoning-token control per provider and the reasoning-only fallback (offline).

Provider parameters were verified live on 2026-09-27:
  * Groq openai/gpt-oss-*   : reasoning_effort must be low|medium|high ("none" -> HTTP 400).
                              Default (no param) is medium: on a doubt prompt 5 of 7
                              streams ended finish_reason=length with ~7k reasoning chars and
                              ZERO content; with "low" 6/6 streamed content (TTFT 0.68 s vs 2.4 s).
  * Groq qwen/qwen3*        : reasoning_effort "none" accepted.
  * OpenCode chat (deepseek): reasoning_effort "none" -> reasoning_tokens 0.
  * OpenCode /responses (muse-spark): effort "none" rejected; "minimal" accepted.
"""
import json

import httpx
import pytest

from app.config import settings
from app.contracts.classifier import InterruptDecision
from app.gateway.groq_client import GroqGateway

GROQ = "https://api.groq.com/openai/v1"
OC = "https://opencode.ai/zen/go/v1"
FMT = {"topic": "maths", "last_teacher_line": "x = 4", "lesson_on_board": True,
       "doubt_pending": False, "lesson_paused": False, "input_mode": "spoken", "utterance": "why?"}


def _sse(*objs) -> bytes:
    return b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objs) + b"data: [DONE]\n\n"


def _gateway(groq_handler=None, oc_handler=None) -> GroqGateway:
    gw = GroqGateway(api_key="mock_key")
    gw.opencode_api_key = "mock_oc_key"
    gw.opencode_base_url = OC
    if groq_handler:
        gw._clients[GROQ] = httpx.AsyncClient(transport=httpx.MockTransport(groq_handler), base_url=GROQ)
    if oc_handler:
        gw._clients[OC] = httpx.AsyncClient(transport=httpx.MockTransport(oc_handler), base_url=OC)
    return gw


REASONING_ONLY_STREAM = _sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
    {"choices": [{"delta": {"reasoning": "We need to respond to the student's doubt...", "channel": "analysis"}}]},
    {"choices": [{"delta": {"reasoning": " Let's think about the formula", "channel": "analysis"}}]},
    {"choices": [{"delta": {}, "finish_reason": "length"}]},
)
GOOD_STREAM = _sse({"choices": [{"delta": {"content": "[STEP] The formula is d. [/STEP]"}}]},
                   {"choices": [{"delta": {}, "finish_reason": "stop"}]})


# ------------------------------------------------------------------ reasoning disabled / minimal
@pytest.mark.asyncio
async def test_groq_gpt_oss_json_sends_low_reasoning_effort():
    seen: dict = {}

    async def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "doubt"}'}}]})

    gw = _gateway(groq_handler=groq)
    res = await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                                 model="openai/gpt-oss-safeguard-20b", user="u", timeout_s=5,
                                 fmt_args=FMT)
    assert res is not None
    assert seen["reasoning_effort"] == "low", "gpt-oss minimum; 'none' is rejected by Groq"
    await gw.close()


@pytest.mark.asyncio
async def test_groq_gpt_oss_stream_sends_low_reasoning_effort():
    seen: dict = {}

    async def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=GOOD_STREAM)

    gw = _gateway(groq_handler=groq)
    out = [c async for c in gw.stream_text(prompt_key="teaching.base", model="openai/gpt-oss-20b",
                                           messages=[{"role": "user", "content": "q"}], timeout_s=5)]
    assert "".join(out).startswith("[STEP]")
    assert seen["reasoning_effort"] == "low"
    await gw.close()


@pytest.mark.asyncio
async def test_gpt_oss_effort_none_setting_is_clamped_to_low(monkeypatch):
    """An owner setting of "none" must not turn every gpt-oss call into an HTTP 400."""
    monkeypatch.setattr(settings, "GROQ_REASONING_EFFORT", "none", raising=False)
    seen: dict = {}

    async def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "doubt"}'}}]})

    gw = _gateway(groq_handler=groq)
    await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                           model="openai/gpt-oss-20b", user="u", timeout_s=5, fmt_args=FMT)
    assert seen["reasoning_effort"] == "low"
    await gw.close()


@pytest.mark.asyncio
async def test_groq_qwen3_sends_reasoning_none():
    seen: dict = {}

    async def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "doubt"}'}}]})

    gw = _gateway(groq_handler=groq)
    await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                           model="qwen/qwen3.8-27b", user="u", timeout_s=5, fmt_args=FMT)
    assert seen["reasoning_effort"] == "none"
    await gw.close()


@pytest.mark.asyncio
async def test_non_reasoning_groq_model_gets_no_reasoning_param():
    seen: dict = {}

    async def groq(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "doubt"}'}}]})

    gw = _gateway(groq_handler=groq)
    await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                           model="llama-3.1-8b-instant", user="u", timeout_s=5, fmt_args=FMT)
    assert "reasoning_effort" not in seen and "reasoning" not in seen
    await gw.close()


@pytest.mark.asyncio
async def test_opencode_responses_api_sends_minimal_effort():
    seen: dict = {}

    async def oc(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"label": "doubt"}'}]}]})

    gw = _gateway(oc_handler=oc)
    await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                           model="muse-spark-1.3", user="u", timeout_s=5, fmt_args=FMT)
    assert seen["reasoning"] == {"effort": "minimal"}, "'none' is rejected for muse-spark"
    await gw.close()


# ------------------------------------------------------------------ reasoning-only fallback
@pytest.mark.asyncio
async def test_stream_reasoning_only_retries_once_on_non_reasoning_model(monkeypatch):
    """A gpt-oss stream that is ALL reasoning and no content must not yield 0 chunks (0 steps ->
    FALLBACK_LINE): it is detected and retried once on MODEL_MAIN (OpenCode deepseek with
    reasoning_effort none)."""
    monkeypatch.setattr(settings, "MODEL_MAIN", "opencode-go/deepseek-v4.1-flash")
    calls: list[str] = []
    oc_seen: dict = {}

    async def groq(req):
        calls.append("groq")
        return httpx.Response(200, content=REASONING_ONLY_STREAM)

    async def oc(req):
        calls.append("opencode")
        oc_seen.update(json.loads(req.content))
        return httpx.Response(200, content=GOOD_STREAM)

    gw = _gateway(groq_handler=groq, oc_handler=oc)
    out = [c async for c in gw.stream_text(prompt_key="teaching.base", model="openai/gpt-oss-20b",
                                           messages=[{"role": "user", "content": "q"}], timeout_s=5)]
    assert "".join(out) == "[STEP] The formula is d. [/STEP]"
    assert calls == ["groq", "opencode"], "exactly one retry"
    assert oc_seen["reasoning_effort"] == "none"
    await gw.close()


@pytest.mark.asyncio
async def test_stream_reasoning_only_same_model_retry_forces_minimal(monkeypatch):
    """When the failing model IS the fallback, retry it once with the provider's minimum."""
    monkeypatch.setattr(settings, "MODEL_MAIN", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "GROQ_REASONING_EFFORT", "high", raising=False)
    efforts: list[str] = []

    async def groq(req):
        efforts.append(json.loads(req.content).get("reasoning_effort"))
        return httpx.Response(200, content=REASONING_ONLY_STREAM if len(efforts) == 1 else GOOD_STREAM)

    gw = _gateway(groq_handler=groq)
    out = [c async for c in gw.stream_text(prompt_key="teaching.base", model="openai/gpt-oss-20b",
                                           messages=[{"role": "user", "content": "q"}], timeout_s=5)]
    assert "".join(out).startswith("[STEP]")
    assert efforts == ["high", "low"]
    await gw.close()


@pytest.mark.asyncio
async def test_stream_reasoning_only_twice_ends_empty_without_looping(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_MAIN", "openai/gpt-oss-20b")
    n = {"calls": 0}

    async def groq(req):
        n["calls"] += 1
        return httpx.Response(200, content=REASONING_ONLY_STREAM)

    gw = _gateway(groq_handler=groq)
    out = [c async for c in gw.stream_text(prompt_key="teaching.base", model="openai/gpt-oss-20b",
                                           messages=[{"role": "user", "content": "q"}], timeout_s=5)]
    assert out == [] and n["calls"] == 2
    await gw.close()


@pytest.mark.asyncio
async def test_stream_with_content_is_not_retried(monkeypatch):
    n = {"calls": 0}

    async def groq(req):
        n["calls"] += 1
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"reasoning": "thinking"}}]},
            {"choices": [{"delta": {"content": "[STEP] ok [/STEP]"}, "finish_reason": "stop"}]}))

    gw = _gateway(groq_handler=groq)
    out = [c async for c in gw.stream_text(prompt_key="teaching.base", model="openai/gpt-oss-20b",
                                           messages=[{"role": "user", "content": "q"}], timeout_s=5)]
    assert "".join(out) == "[STEP] ok [/STEP]" and n["calls"] == 1
    await gw.close()


@pytest.mark.asyncio
async def test_complete_json_reasoning_only_retries_once(monkeypatch):
    """Non-streaming: content "" + message.reasoning (or only a <think> block) is not a JSON
    validation failure to shrug off — retry once on the fallback model."""
    monkeypatch.setattr(settings, "MODEL_MAIN", "opencode-go/deepseek-v4.1-flash")
    calls: list[str] = []

    async def groq(req):
        calls.append("groq")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning": "long analysis..."},
                         "finish_reason": "length"}],
            "usage": {"completion_tokens": 2000, "completion_tokens_details": {"reasoning_tokens": 2000}}})

    async def oc(req):
        calls.append("opencode")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "affirmation"}'}}]})

    gw = _gateway(groq_handler=groq, oc_handler=oc)
    res = await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                                 model="openai/gpt-oss-safeguard-20b", user="u", timeout_s=5,
                                 fmt_args=FMT)
    assert res is not None and res.label == "affirmation"
    assert calls == ["groq", "opencode"]
    await gw.close()


@pytest.mark.asyncio
async def test_complete_json_think_block_only_counts_as_reasoning_only(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_MAIN", "opencode-go/deepseek-v4.1-flash")
    calls: list[str] = []

    async def groq(req):
        calls.append("groq")
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "<think>The student said why, so...</think>"}}]})

    async def oc(req):
        calls.append("opencode")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label": "doubt"}'}}]})

    gw = _gateway(groq_handler=groq, oc_handler=oc)
    res = await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                                 model="qwen/qwen3.8-27b", user="u", timeout_s=5, fmt_args=FMT)
    assert res is not None and calls == ["groq", "opencode"]
    await gw.close()


@pytest.mark.asyncio
async def test_complete_json_invalid_json_with_content_is_not_retried(monkeypatch):
    """A real schema failure (content present) is the repair loop's job, not this fallback."""
    n = {"calls": 0}

    async def groq(req):
        n["calls"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})

    gw = _gateway(groq_handler=groq)
    res = await gw.complete_json(prompt_key="classifier.interrupt", schema=InterruptDecision,
                                 model="openai/gpt-oss-20b", user="u", timeout_s=5, fmt_args=FMT)
    assert res is None and n["calls"] == 1
    await gw.close()


@pytest.mark.asyncio
async def test_doubt_turn_with_reasoning_only_stream_still_teaches(monkeypatch):
    """End to end through the teaching node: MODEL_FAST streams only reasoning; the doubt turn
    must still produce steps instead of the FALLBACK_LINE path."""
    from app.agents.nodes.teaching import stream_teaching_turn
    from app.contracts.agent_state import TurnRequest
    from app.tutor.board_rows import BoardRowTracker

    monkeypatch.setattr(settings, "MODEL_FAST", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "MODEL_MAIN", "opencode-go/deepseek-v4.1-flash")

    async def groq(req):
        return httpx.Response(200, content=REASONING_ONLY_STREAM)

    async def oc(req):
        return httpx.Response(200, content=GOOD_STREAM)

    gw = _gateway(groq_handler=groq, oc_handler=oc)
    req = TurnRequest(kind="doubt", generation=0, turn_id="t_d", question="distance from a line",
                      student_text="what formula you used?", doubt_prompt="my doubt: what formula?")
    steps = [item["step"] async for item in stream_teaching_turn(
        request=req, plan=None, diagram=None, row_tracker=BoardRowTracker(), history=[],
        lesson_question=req.question, agent_state=None, gw=gw, run_ctx=None, memory=None)]
    assert len(steps) == 1 and "formula" in steps[0].spoken_text
    await gw.close()

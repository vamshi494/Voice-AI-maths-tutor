# backend/tests/test_gateway.py
import json
import pytest
import httpx
from app.gateway.groq_client import GroqGateway, GatewayStreamError
from app.contracts.classifier import InterruptDecision


@pytest.mark.asyncio
async def test_complete_json_success():
    mock_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"label": "doubt"})
                }
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
        },
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw._clients["https://api.groq.com/openai/v1"] = gw._client = httpx.AsyncClient(transport=transport, base_url="https://api.groq.com/openai/v1")

    res = await gw.complete_json(
        prompt_key="classifier.interrupt",
        schema=InterruptDecision,
        model="qwen-2.5-7b-instruct",
        user="student: wait why?",
        timeout_s=5.0,
        fmt_args={
            "topic": "maths",
            "last_teacher_line": "x = 4",
            "lesson_on_board": True,
            "doubt_pending": False,
            "lesson_paused": False,
            "input_mode": "spoken",
            "utterance": "wait why?",
        },
    )
    assert res is not None
    assert res.label == "doubt"
    await gw.close()


@pytest.mark.asyncio
async def test_complete_json_http_error_returns_none():
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw._clients["https://api.groq.com/openai/v1"] = gw._client = httpx.AsyncClient(transport=transport, base_url="https://api.groq.com/openai/v1")

    res = await gw.complete_json(
        prompt_key="classifier.interrupt",
        schema=InterruptDecision,
        model="qwen-2.5-7b-instruct",
        user="student: test",
        timeout_s=5.0,
        fmt_args={
            "topic": "maths",
            "last_teacher_line": "x = 4",
            "lesson_on_board": True,
            "doubt_pending": False,
            "lesson_paused": False,
            "input_mode": "spoken",
            "utterance": "test",
        },
    )
    assert res is None
    await gw.close()


@pytest.mark.asyncio
async def test_stream_text_success():
    chunks = [
        b'data: {"choices": [{"delta": {"content": "[STEP]First"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": " step[/STEP]"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(chunks), headers={"Content-Type": "text/event-stream"})

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw._clients["https://api.groq.com/openai/v1"] = gw._client = httpx.AsyncClient(transport=transport, base_url="https://api.groq.com/openai/v1")

    tokens = []
    async for chunk in gw.stream_text(
        prompt_key="teaching.base",
        messages=[{"role": "user", "content": "hello"}],
        model="llama-3.3-70b-versatile",
        timeout_s=5.0,
    ):
        tokens.append(chunk)

    assert "".join(tokens) == "[STEP]First step[/STEP]"
    await gw.close()


def test_key_rotator_round_robin_and_cooldown():
    from app.gateway.groq_client import KeyRotator
    import time

    rotator = KeyRotator(["key_1", "key_2", "key_3"], cooldown_seconds=2.0)
    assert rotator.total_keys == 3

    # Sequential round-robin
    k1 = rotator.get_key()
    k2 = rotator.get_key()
    k3 = rotator.get_key()
    k4 = rotator.get_key()
    assert [k1, k2, k3, k4] == ["key_1", "key_2", "key_3", "key_1"]

    # Quarantine key_2
    rotator.report_rate_limit("key_2")
    st = rotator.status()
    assert st["active_keys"] == 2
    assert st["quarantined_keys"] == 1

    # Next keys should skip key_2
    active_picks = [rotator.get_key(), rotator.get_key(), rotator.get_key()]
    assert "key_2" not in active_picks
    assert set(active_picks).issubset({"key_1", "key_3"})


def test_normalize_opencode_model():
    from app.gateway.groq_client import normalize_opencode_model, is_opencode_model

    assert normalize_opencode_model("muse-spark-1.2-contributer") == "muse-spark-1.2-contributor"
    assert normalize_opencode_model("opencode-go/muse-spark-1.2-contributer") == "muse-spark-1.2-contributor"
    assert normalize_opencode_model("muse-spark-1.3-contributer") == "muse-spark-1.3-contributor"
    assert normalize_opencode_model("deepseek-v4-flash") == "deepseek-v4-flash"
    assert normalize_opencode_model("opencode-go/deepseek-v4.1-flash") == "deepseek-v4.1-flash"

    assert is_opencode_model("muse-spark-1.2-contributer", "oc_sk_123") is True
    assert is_opencode_model("opencode-go/anything", "oc_sk_123") is True
    assert is_opencode_model("muse-spark-1.2-contributer", "") is False
    assert is_opencode_model("openai/gpt-oss-120b", "oc_sk_123") is False
    # deepseek must route to OpenCode Zen, not fall through to Groq
    assert is_opencode_model("opencode-go/deepseek-v4.1-flash", "oc_sk_123") is True
    assert is_opencode_model("deepseek-v4.1-flash", "oc_sk_123") is True
    assert is_opencode_model("deepseek-v4.1-flash", "") is False


@pytest.mark.asyncio
async def test_opencode_chat_completions_disables_reasoning():
    """deepseek-v4.1-flash must go to /chat/completions with reasoning_effort=none and max_tokens."""
    mock_payload = {
        "choices": [{"message": {"content": json.dumps({"label": "doubt"})}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 15, "total_tokens": 55},
    }

    seen: dict = {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers.get("authorization") == "Bearer mock_oc_key"
        data = json.loads(request.content)
        seen.update(data)
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw.opencode_api_key = "mock_oc_key"
    gw.opencode_base_url = "https://opencode.ai/zen/go/v1"
    gw._clients["https://opencode.ai/zen/go/v1"] = httpx.AsyncClient(
        transport=transport, base_url="https://opencode.ai/zen/go/v1"
    )

    res = await gw.complete_json(
        prompt_key="classifier.interrupt",
        schema=InterruptDecision,
        model="opencode-go/deepseek-v4.1-flash",
        user="student: wait what?",
        timeout_s=5.0,
        fmt_args={
            "topic": "maths",
            "last_teacher_line": "x = 4",
            "lesson_on_board": True,
            "doubt_pending": False,
            "lesson_paused": False,
            "input_mode": "spoken",
            "utterance": "wait what?",
        },
    )
    assert res is not None and res.label == "doubt"
    assert seen["model"] == "deepseek-v4.1-flash"
    assert seen["reasoning_effort"] == "none"
    assert seen["max_tokens"] > 0
    await gw.close()


@pytest.mark.asyncio
async def test_opencode_stream_chat_completions_disables_reasoning():
    chunks = [
        b'data: {"choices": [{"delta": {"content": "[STEP]Deep"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": " seek step[/STEP]"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    seen: dict = {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"".join(chunks), headers={"Content-Type": "text/event-stream"})

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw.opencode_api_key = "mock_oc_key"
    gw.opencode_base_url = "https://opencode.ai/zen/go/v1"
    gw._clients["https://opencode.ai/zen/go/v1"] = httpx.AsyncClient(
        transport=transport, base_url="https://opencode.ai/zen/go/v1"
    )

    tokens = []
    async for chunk in gw.stream_text(
        prompt_key="teaching.base",
        messages=[{"role": "user", "content": "hello"}],
        model="opencode-go/deepseek-v4.1-flash",
        timeout_s=5.0,
    ):
        tokens.append(chunk)

    assert "".join(tokens) == "[STEP]Deep seek step[/STEP]"
    assert seen["model"] == "deepseek-v4.1-flash"
    assert seen["reasoning_effort"] == "none"
    assert seen["stream"] is True
    await gw.close()


@pytest.mark.asyncio
async def test_opencode_complete_json_responses_api():
    from app.contracts.classifier import InterruptDecision

    mock_resp_payload = {
        "id": "resp_test123",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"label": "doubt"}),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 40,
            "output_tokens": 15,
            "total_tokens": 55,
        },
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        assert request.headers.get("authorization") == "Bearer mock_oc_key"
        assert "x-opencode-session" in request.headers
        data = json.loads(request.content)
        assert data["model"] == "muse-spark-1.2-contributor"
        return httpx.Response(200, json=mock_resp_payload)

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw.opencode_api_key = "mock_oc_key"
    gw.opencode_base_url = "https://opencode.ai/zen/go/v1"
    gw._clients["https://opencode.ai/zen/go/v1"] = httpx.AsyncClient(
        transport=transport, base_url="https://opencode.ai/zen/go/v1"
    )

    res = await gw.complete_json(
        prompt_key="classifier.interrupt",
        schema=InterruptDecision,
        model="muse-spark-1.2-contributer",
        user="student: wait what?",
        timeout_s=5.0,
        fmt_args={
            "topic": "maths",
            "last_teacher_line": "x = 4",
            "lesson_on_board": True,
            "doubt_pending": False,
            "lesson_paused": False,
            "input_mode": "spoken",
            "utterance": "wait what?",
        },
    )
    assert res is not None
    assert res.label == "doubt"
    await gw.close()


@pytest.mark.asyncio
async def test_opencode_stream_text_responses_api():
    chunks = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"[STEP]Muse step"}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"[/STEP]"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
    ]

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        assert request.headers.get("authorization") == "Bearer mock_oc_key"
        assert "x-opencode-session" in request.headers
        data = json.loads(request.content)
        assert data["model"] == "muse-spark-1.2-contributor"
        assert data["stream"] is True
        return httpx.Response(200, content=b"".join(chunks), headers={"Content-Type": "text/event-stream"})

    transport = httpx.MockTransport(mock_handler)
    gw = GroqGateway(api_key="mock_key")
    gw.opencode_api_key = "mock_oc_key"
    gw.opencode_base_url = "https://opencode.ai/zen/go/v1"
    gw._clients["https://opencode.ai/zen/go/v1"] = httpx.AsyncClient(
        transport=transport, base_url="https://opencode.ai/zen/go/v1"
    )

    tokens = []
    async for chunk in gw.stream_text(
        prompt_key="teaching.base",
        messages=[{"role": "user", "content": "hello"}],
        model="muse-spark-1.2-contributer",
        timeout_s=5.0,
    ):
        tokens.append(chunk)

    assert "".join(tokens) == "[STEP]Muse step[/STEP]"
    await gw.close()


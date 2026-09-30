# app/gateway/groq_client.py
import asyncio
import json
import re
import time
import uuid
from contextvars import ContextVar
from collections.abc import AsyncIterator
from typing import Any, TypeVar
import httpx
from pydantic import BaseModel

from app.config import settings
from app.observability import (
    log_event,
    log_llm_failure,
    log_llm_rate_limit,
    log_llm_start,
    log_llm_stream_done,
    log_llm_stream_start,
    log_llm_success,
    logger,
    trace_span,
)
from app.prompts.registry import ACTIVE, get_prompt

M = TypeVar("M", bound=BaseModel)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"


def normalize_opencode_model(model: str) -> str:
    clean = model.strip()
    if clean.startswith("opencode-go/"):
        clean = clean[len("opencode-go/"):]
    if clean.endswith("-contributer"):
        clean = clean[:-len("-contributer")] + "-contributor"
    if clean in ("muse-spark-1.2", "muse-spark-1.2-contributor"):
        return "muse-spark-1.2-contributor"
    if clean in ("muse-spark-1.3", "muse-spark-1.3-contributor"):
        return "muse-spark-1.3-contributor"
    return clean


def is_opencode_model(model: str, opencode_api_key: str | None) -> bool:
    if not opencode_api_key:
        return False
    m = model.strip().lower()
    return (
        m.startswith("muse-")
        or "spark" in m
        or m.startswith("deepseek")
        or m.startswith("opencode-go/")
    )


# Per-task error slot. asyncio gives every task its own context copy, so concurrent calls
# on the shared gateway singleton can never read each other's error.
_call_error: ContextVar[str | None] = ContextVar("gateway_call_error", default=None)


def _set_err(msg: str | None) -> str | None:
    _call_error.set(msg)
    return msg


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Set by a single attempt when the reply carried reasoning but no usable output (per task).
_reasoning_only: ContextVar[bool] = ContextVar("gateway_reasoning_only", default=False)

_GPT_OSS_EFFORTS = ("low", "medium", "high")


def is_gpt_oss(model: str) -> bool:
    return model.strip().lower().startswith("openai/gpt-oss")


def is_qwen3(model: str) -> bool:
    return model.strip().lower().startswith("qwen/qwen3")


def reasoning_params(model: str, route: str, minimal: bool = False) -> dict[str, Any]:
    """Provider-correct reasoning control (verified live 2026-09-27).

    route: "opencode_chat" | "opencode_responses" | "groq" | "openrouter".
    `minimal=True` is the reasoning-only retry: the lowest effort the provider accepts.
      * OpenCode chat-completions (deepseek): reasoning_effort "none" -> 0 reasoning tokens.
      * OpenCode /responses (muse-spark): "none" is rejected; "minimal" is the floor.
      * Groq openai/gpt-oss-*: low|medium|high only ("none" -> HTTP 400); no parameter = medium.
      * Groq qwen/qwen3*: reasoning_effort "none".
      * OpenRouter / other Groq models: nothing is sent (OpenRouter's parameter could not be
        verified: the key is over its limit; no configured OpenRouter model reasons).
    """
    if route == "opencode_chat":
        return {"reasoning_effort": "none" if minimal else settings.OPENCODE_REASONING_EFFORT}
    if route == "opencode_responses":
        return {"reasoning": {"effort": "minimal" if minimal
                              else settings.OPENCODE_RESPONSES_REASONING_EFFORT}}
    if route == "groq":
        if is_gpt_oss(model):
            effort = "low" if minimal else str(settings.GROQ_REASONING_EFFORT or "low").strip().lower()
            return {"reasoning_effort": effort if effort in _GPT_OSS_EFFORTS else "low"}
        if is_qwen3(model):
            return {"reasoning_effort": "none"}
    return {}


def visible_text(content: Any) -> str:
    """Model output with reasoning removed: closed <think> blocks and an unclosed leading one
    (a reply cut off mid-thought)."""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    text = _THINK_RE.sub("", content or "")
    if text.lstrip().lower().startswith("<think>"):
        return ""
    return text.strip()


def _chat_delta(chunk: dict[str, Any], stats: dict[str, Any]) -> str:
    """Content text of one chat-completions SSE chunk; reasoning deltas (Groq `reasoning`,
    Zen/DeepSeek `reasoning_content`) and the finish reason are only counted into `stats`."""
    choices = chunk.get("choices", [])
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta", {}) or {}
    stats["reasoning"] += len(delta.get("reasoning") or delta.get("reasoning_content") or "")
    if choice.get("finish_reason"):
        stats["finish"] = choice.get("finish_reason")
    content = delta.get("content") or ""
    if content:
        stats["text"] += content
    return content


def _new_stream_stats() -> dict[str, Any]:
    return {"text": "", "reasoning": 0, "finish": None}


def stream_was_reasoning_only(stats: dict[str, Any]) -> bool:
    """Nothing usable reached the caller and the model spent its turn thinking (or was cut off
    by the output limit before any content)."""
    if visible_text(stats["text"]):
        return False
    return stats["reasoning"] > 0 or stats["finish"] == "length" or "<think>" in stats["text"].lower()


def message_is_reasoning_only(message: dict[str, Any], usage: dict[str, Any] | None = None) -> bool:
    """A chat-completions message that thought but produced nothing usable."""
    content = message.get("content") or ""
    if visible_text(content):
        return False
    if message.get("reasoning") or message.get("reasoning_content"):
        return True
    raw = content if isinstance(content, str) else ""
    if "<think>" in raw.lower():
        return True
    details = (usage or {}).get("completion_tokens_details") or {}
    return int(details.get("reasoning_tokens") or 0) > 0


def extract_json_object(text: str) -> Any:
    """Recover the JSON object from an LLM reply.

    Handles, in order: <think>...</think> reasoning preambles (reasoning models, and any
    model once the json_validate_failed fallback drops response_format), ``` fences,
    prose before/after the object, and minor syntax damage (trailing commas, single
    quotes) via json_repair. Raises ValueError if no object can be recovered.
    """
    clean = _THINK_RE.sub("", text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        return json.loads(clean)
    except ValueError:
        pass
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end > start:
        candidate = clean[start:end + 1]
        try:
            return json.loads(candidate)
        except ValueError:
            try:
                import json_repair
                repaired = json_repair.loads(candidate)
                if isinstance(repaired, (dict, list)) and repaired:
                    return repaired
            except Exception:
                pass
    raise ValueError("no JSON object found in model output")


def summarize_validation_error(err: Exception, limit: int = 1200) -> str:
    """Compact, model-readable description of why a JSON reply was rejected.
    Fed back into repair prompts so a repair attempt knows what to fix."""
    try:
        from pydantic import ValidationError
        if isinstance(err, ValidationError):
            lines = []
            for e in err.errors()[:12]:
                loc = ".".join(str(x) for x in e.get("loc", ()))
                lines.append(f"{loc}: {e.get('msg')}")
            return "schema: " + "; ".join(lines)[:limit]
    except Exception:
        pass
    return f"{type(err).__name__}: {err}"[:limit]


class GatewayStreamError(Exception):
    """Raised when streaming chat completion fails."""
    pass


class KeyRotator:
    """
    Thread-safe round-robin API key manager with 429 rate-limit cooldown.
    Distributes requests across multiple keys and temporarily quarantines
    any key that encounters rate limits or quota exhaustion.
    """

    def __init__(self, keys: list[str] | str, name: str = "groq", cooldown_seconds: float = 60.0):
        self.name = name
        self.cooldown_seconds = cooldown_seconds
        if isinstance(keys, str):
            self.keys = [k.strip() for k in keys.split(",") if k.strip()]
        else:
            self.keys = [k.strip() for k in keys if k and k.strip()]
        self._index = 0
        self._quarantined_until: dict[str, float] = {k: 0.0 for k in self.keys}

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    def set_keys(self, keys: list[str] | str) -> None:
        if isinstance(keys, str):
            self.keys = [k.strip() for k in keys.split(",") if k.strip()]
        else:
            self.keys = [k.strip() for k in keys if k and k.strip()]
        self._quarantined_until = {k: 0.0 for k in self.keys}
        self._index = 0

    def get_key(self) -> str | None:
        """Returns the next available unquarantined API key, or the soonest expiring key."""
        if not self.keys:
            return None

        now = time.monotonic()
        total = len(self.keys)

        # Check in round-robin order for an unquarantined key
        for _ in range(total):
            key = self.keys[self._index]
            self._index = (self._index + 1) % total
            if self._quarantined_until.get(key, 0.0) <= now:
                return key

        # If all keys are cooling down, return the one expiring soonest
        soonest_key = min(self.keys, key=lambda k: self._quarantined_until.get(k, 0.0))
        remaining = max(0.0, self._quarantined_until[soonest_key] - now)
        logger.warning(
            f"[{self.name}] All {total} keys in cooldown! Key ...{soonest_key[-4:]} expires in {remaining:.1f}s"
        )
        return soonest_key

    def report_rate_limit(self, key: str, custom_cooldown: float | None = None) -> None:
        """Quarantines a key for cooldown_seconds after a 429 error."""
        cd = custom_cooldown if custom_cooldown is not None else self.cooldown_seconds
        self._quarantined_until[key] = time.monotonic() + cd
        logger.warning(
            f"[{self.name}] Key ...{key[-4:] if len(key) >= 4 else '***'} hit rate limit (429). Quarantined for {cd}s."
        )

    def get_remaining_cooldown(self, key: str) -> float:
        now = time.monotonic()
        return max(0.0, self._quarantined_until.get(key, 0.0) - now)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        active = sum(1 for k in self.keys if self._quarantined_until.get(k, 0.0) <= now)
        return {
            "name": self.name,
            "total_keys": len(self.keys),
            "active_keys": active,
            "quarantined_keys": len(self.keys) - active,
        }


class GroqGateway:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = GROQ_BASE_URL,
        cooldown_seconds: float = 60.0,
    ) -> None:
        raw_keys = settings.GROQ_API_KEYS or api_key or settings.GROQ_API_KEY
        self.rotator = KeyRotator(raw_keys, name="groq", cooldown_seconds=cooldown_seconds)
        self.base_url = base_url
        self.openrouter_api_key = settings.OPENROUTER_API_KEY
        self.opencode_api_key = getattr(settings, "OPENCODE_API_KEY", "")
        self.opencode_base_url = getattr(settings, "OPENCODE_BASE_URL", OPENCODE_BASE_URL)
        self._client: httpx.AsyncClient | None = None
        self._clients: dict[str, httpx.AsyncClient] = {}

        self.last_error: str | None = None

    @property
    def api_key(self) -> str:
        k = self.rotator.get_key()
        return k or settings.GROQ_API_KEY

    def get_client(self, api_key: str | None = None, base_url: str | None = None) -> httpx.AsyncClient:
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # If caller provides a custom client or we need a configured client per request
        target_base = (base_url or self.base_url).rstrip("/")
        existing = self._clients.get(target_base)
        if existing is not None and not existing.is_closed:
            self._client = existing
        else:
            self._client = self._clients[target_base] = httpx.AsyncClient(
                base_url=target_base,
                headers=headers,
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._client

    @staticmethod
    def _auth(key: str | None) -> dict[str, str]:
        """Authorization is passed PER REQUEST. Mutating the shared client's default headers
        raced when two lanes ran concurrently on different rotated keys."""
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def close(self) -> None:
        for c in list(self._clients.values()):
            if not c.is_closed:
                await c.aclose()
        self._clients.clear()
        self._client = None

    def reasoning_fallback_model(self, model: str) -> str:
        """Target of the single reasoning-only retry (same model = retried at minimum effort)."""
        return (settings.LLM_REASONING_FALLBACK_MODEL or settings.MODEL_MAIN or model).strip()

    async def complete_json(
        self,
        *,
        prompt_key: str,
        schema: type[M],
        model: str,
        user: str,
        system: str | None = None,
        image_b64: str | None = None,
        timeout_s: float,
        temperature: float = 0.0,
        fmt_args: dict[str, Any] | None = None,
    ) -> M | None:
        """One call. JSON mode (response_format json_object).
        Supports round-robin key rotation and 429 cooldown.

        A reply that carries ONLY reasoning (empty content + reasoning field, a bare
        <think> block, or reasoning tokens and nothing else) is retried exactly once on
        reasoning_fallback_model() at the provider's minimum reasoning effort.
        """
        kwargs = dict(prompt_key=prompt_key, schema=schema, model=model, user=user, system=system,
                      image_b64=image_b64, timeout_s=timeout_s, temperature=temperature,
                      fmt_args=fmt_args)
        _reasoning_only.set(False)
        result = await self._complete_json_once(**kwargs)
        if result is None and _reasoning_only.get():
            fallback = self.reasoning_fallback_model(model)
            log_event("llm_reasoning_only_retry", prompt_key=prompt_key, model=model,
                      fallback=fallback, mode="json")
            _reasoning_only.set(False)
            result = await self._complete_json_once(**{**kwargs, "model": fallback},
                                                    minimal_reasoning=True)
            if result is None and _reasoning_only.get():
                log_event("llm_reasoning_only_exhausted", prompt_key=prompt_key, model=fallback,
                          mode="json")
        return result

    def _flag_reasoning_only(self, prompt_key: str, model: str, latency_ms: float) -> None:
        _reasoning_only.set(True)
        self.last_error = _set_err("reasoning_only: the model returned reasoning but no output")
        log_llm_failure(prompt_key, model, latency_ms, "reasoning-only reply (no content)")
        log_event("llm_reasoning_only", prompt_key=prompt_key, model=model)

    async def _complete_json_once(
        self,
        *,
        prompt_key: str,
        schema: type[M],
        model: str,
        user: str,
        system: str | None = None,
        image_b64: str | None = None,
        timeout_s: float,
        temperature: float = 0.0,
        fmt_args: dict[str, Any] | None = None,
        minimal_reasoning: bool = False,
    ) -> M | None:
        self.last_error = _set_err(None)
        version_key = ACTIVE.get(prompt_key, prompt_key)
        start_time = time.monotonic()

        # Prepare system prompt
        if system is None:
            try:
                system_text = get_prompt(prompt_key)
                if fmt_args:
                    system_text = system_text.format(**fmt_args)
            except Exception as e:
                logger.error(f"Error formatting system prompt for {prompt_key}: {e}")
                self.last_error = _set_err(f"Prompt format error: {e}")
                return None
        else:
            system_text = system.format(**fmt_args) if fmt_args else system

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text}
        ]

        if image_b64:
            user_content: list[dict[str, Any]] = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        # Log LLM call start to terminal and Logfire
        log_llm_start(prompt_key, model, prompt_len=len(user) + len(system_text))

        # Helper to extract JSON from possible markdown wrapping
        def _parse_json(text: str) -> M | None:
            return schema.model_validate(extract_json_object(text))

        # Check if model is targeted for OpenCode Go
        is_opencode = is_opencode_model(model, self.opencode_api_key)
        if is_opencode:
            norm_model = normalize_opencode_model(model)
            use_responses_api = norm_model.startswith("muse-") or "spark" in norm_model.lower()
            endpoint_url = "/responses" if use_responses_api else "/chat/completions"
            oc_client = self.get_client(api_key=self.opencode_api_key, base_url=self.opencode_base_url)
            oc_headers = {
                "Authorization": f"Bearer {self.opencode_api_key}",
                "x-opencode-session": str(uuid.uuid4()),
            }

            if use_responses_api:
                oc_payload: dict[str, Any] = {
                    "model": norm_model,
                    "instructions": system_text,
                    "input": user,
                    "temperature": temperature,
                    **reasoning_params(model, "opencode_responses", minimal_reasoning),
                }
            else:
                # Zen chat-completions models (deepseek-v4.1-flash) think by default. Send
                # reasoning_effort="none" to keep first-token latency low, and cap output.
                oc_payload = dict(payload)
                oc_payload["model"] = norm_model
                oc_payload.update(reasoning_params(model, "opencode_chat", minimal_reasoning))
                oc_payload["max_tokens"] = settings.LLM_MAX_TOKENS

            for attempt in range(2):
                try:
                    resp = await oc_client.post(
                        endpoint_url,
                        json=oc_payload,
                        timeout=timeout_s,
                        headers=oc_headers,
                    )
                    latency_ms = (time.monotonic() - start_time) * 1000

                    # Transient 503 retry
                    if resp.status_code == 503 and attempt == 0:
                        logger.warning(f"OpenCode returned 503 for {norm_model}, retrying in 1s...")
                        await asyncio.sleep(1.0)
                        continue

                    # Retry once on a 400. If the error names response_format, drop it and lean
                    # on extract_json_object (fences/prose already handled); otherwise Zen's
                    # upstream occasionally returns a transient "Invalid request parameters".
                    if resp.status_code == 400 and attempt == 0 and not use_responses_api:
                        if "response_format" in resp.text:
                            logger.info(f"OpenCode rejected response_format for {prompt_key}; retrying without it...")
                            oc_payload = {k: v for k, v in oc_payload.items() if k != "response_format"}
                        else:
                            logger.info(f"OpenCode returned 400 for {prompt_key}; retrying once...")
                        await asyncio.sleep(0.4)
                        continue

                    if resp.status_code == 200:
                        data = resp.json()
                        if use_responses_api:
                            raw_json = ""
                            for item in data.get("output", []):
                                if item.get("type") == "message":
                                    for part in item.get("content", []):
                                        if part.get("type") == "output_text":
                                            raw_json += part.get("text", "")
                            reasoning_only = (not visible_text(raw_json) and any(
                                item.get("type") == "reasoning" for item in data.get("output", [])))
                        else:
                            choices = data.get("choices", [])
                            message = choices[0].get("message", {}) if choices else {}
                            raw_json = message.get("content", "") or ""
                            reasoning_only = message_is_reasoning_only(message, data.get("usage"))
                        if reasoning_only:
                            self._flag_reasoning_only(prompt_key, model, latency_ms)
                            return None

                        try:
                            result = _parse_json(raw_json)
                            usage = data.get("usage", {})
                            tokens_usage = {
                                "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens", 0),
                                "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens", 0),
                                "total_tokens": usage.get("total_tokens", 0),
                            }
                            log_llm_success(prompt_key, model, latency_ms, tokens=tokens_usage, preview=raw_json[:80])
                            return result
                        except Exception as e:
                            self.last_error = _set_err(summarize_validation_error(e))
                            log_llm_failure(prompt_key, model, latency_ms, f"Pydantic validation error: {e}", raw_response=raw_json)
                            return None
                    else:
                        try:
                            err_msg = resp.json().get("error", {}).get("message", resp.text)
                        except Exception:
                            err_msg = resp.text
                        self.last_error = _set_err(f"OpenCode error ({resp.status_code}): {err_msg}")
                        log_llm_failure(prompt_key, model, latency_ms, f"OpenCode HTTP {resp.status_code}: {err_msg}")
                        logger.warning(f"OpenCode HTTP error {resp.status_code}: {resp.text}")
                        return None
                except Exception as e:
                    latency_ms = (time.monotonic() - start_time) * 1000
                    self.last_error = _set_err(f"OpenCode connection error: {e}")
                    log_llm_failure(prompt_key, model, latency_ms, str(e))
                    logger.warning(f"OpenCode complete_json failed: {e}")
                    return None
            return None

        # Check if model is specifically an OpenRouter-only model (Nvidia, etc.)
        # Note: qwen/qwen3.8-27b is natively hosted on GroqCloud!
        is_openrouter = self.openrouter_api_key and (
            model.startswith("nvidia/")
            or (model.startswith("qwen/") and model != "qwen/qwen3.8-27b")
            or model.startswith("anthropic/")
            or model.startswith("google/")
        )

        if is_openrouter:
            try:
                or_client = self.get_client(api_key=self.openrouter_api_key, base_url=OPENROUTER_BASE_URL)
                resp = await or_client.post("/chat/completions", json=payload, timeout=timeout_s,
                                           headers=self._auth(self.openrouter_api_key))
                latency_ms = (time.monotonic() - start_time) * 1000
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        raw_json = message.get("content", "") or ""
                        if message_is_reasoning_only(message, data.get("usage")):
                            self._flag_reasoning_only(prompt_key, model, latency_ms)
                            return None
                        try:
                            result = _parse_json(raw_json)
                            log_llm_success(prompt_key, model, latency_ms, preview=raw_json[:80])
                            return result
                        except Exception as e:
                            self.last_error = _set_err(summarize_validation_error(e))
                            log_llm_failure(prompt_key, model, latency_ms, f"Pydantic validation error: {e}", raw_response=raw_json)
                            return None
                else:
                    try:
                        err_msg = resp.json().get("error", {}).get("message", resp.text)
                    except Exception:
                        err_msg = resp.text
                    self.last_error = _set_err(f"OpenRouter error ({resp.status_code}): {err_msg}")
                    log_llm_failure(prompt_key, model, latency_ms, f"OpenRouter HTTP {resp.status_code}: {err_msg}")
                    logger.warning(f"OpenRouter HTTP error {resp.status_code}: {resp.text}")
            except Exception as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                self.last_error = _set_err(f"OpenRouter connection error: {e}")
                log_llm_failure(prompt_key, model, latency_ms, str(e))
                logger.warning(f"OpenRouter complete_json failed: {e}")
            return None

        # Try active Groq keys with round-robin and 429 rotation
        groq_payload = {**payload, **reasoning_params(model, "groq", minimal_reasoning)}
        keys_to_try = [k for k in self.rotator.keys] if self.rotator.keys else [self.api_key]
        outcome = "failure"
        tokens_usage: dict[str, int] = {}
        latency_ms: float = 0.0

        for _ in range(max(1, len(keys_to_try))):
            current_key = self.rotator.get_key() or self.api_key
            rem_cd = self.rotator.get_remaining_cooldown(current_key)
            if rem_cd > 0 and rem_cd <= 4.0:
                await asyncio.sleep(rem_cd + 0.1)

            client = self.get_client(api_key=current_key, base_url=self.base_url)

            try:
                with trace_span("groq_complete_json", {
                    "prompt_key": prompt_key,
                    "version_key": version_key,
                    "model": model,
                    "key_suffix": current_key[-4:] if len(current_key) >= 4 else "none",
                }):
                    resp = await client.post(
                        "/chat/completions",
                        json=groq_payload,
                        timeout=timeout_s,
                        headers=self._auth(current_key),
                    )
                    latency_ms = (time.monotonic() - start_time) * 1000

                    if resp.status_code == 429:
                        self.rotator.report_rate_limit(current_key)
                        log_llm_rate_limit(prompt_key, current_key[-4:])
                        log_event("gateway_rate_limited", key_suffix=current_key[-4:], prompt_key=prompt_key)
                        continue

                    # If strict json_object validation failed on Groq, retry without response_format constraint
                    if resp.status_code == 400 and "json_validate_failed" in resp.text:
                        logger.info(f"Groq strict JSON validation failed for {prompt_key}; retrying without response_format...")
                        fallback_payload = dict(groq_payload)
                        fallback_payload.pop("response_format", None)
                        resp = await client.post(
                            "/chat/completions",
                            json=fallback_payload,
                            timeout=timeout_s,
                            headers=self._auth(current_key),
                        )
                        latency_ms = (time.monotonic() - start_time) * 1000

                    if resp.status_code != 200:
                        log_llm_failure(prompt_key, model, latency_ms, f"Groq HTTP {resp.status_code}: {resp.text}")
                        logger.warning(f"Groq HTTP error {resp.status_code}: {resp.text}")
                        return None

                    data = resp.json()
                    usage = data.get("usage", {})
                    tokens_usage = {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }

                    choices = data.get("choices", [])
                    if not choices:
                        log_llm_failure(prompt_key, model, latency_ms, "Groq returned empty choices")
                        return None

                    message = choices[0].get("message", {})
                    raw_json = message.get("content", "") or ""
                    if message_is_reasoning_only(message, usage):
                        outcome = "reasoning_only"
                        self._flag_reasoning_only(prompt_key, model, latency_ms)
                        return None
                    try:
                        result = _parse_json(raw_json)
                        outcome = "success"
                        log_llm_success(prompt_key, model, latency_ms, tokens=tokens_usage, preview=raw_json[:80])
                        return result
                    except Exception as e:
                        outcome = "validation_or_network_error"
                        self.last_error = _set_err(summarize_validation_error(e))
                        log_llm_failure(prompt_key, model, latency_ms, f"Pydantic validation error: {e}", raw_response=raw_json)
                        logger.warning(f"Groq complete_json failed for prompt {prompt_key}: {e}")
                        return None

            except httpx.TimeoutException:
                latency_ms = (time.monotonic() - start_time) * 1000
                outcome = "timeout"
                log_llm_failure(prompt_key, model, latency_ms, f"Timeout after {timeout_s}s")
                logger.warning(f"Groq call timed out after {timeout_s}s for prompt {prompt_key}")
                return None
            except Exception as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                outcome = "validation_or_network_error"
                log_llm_failure(prompt_key, model, latency_ms, str(e))
                logger.warning(f"Groq complete_json failed for prompt {prompt_key}: {e}")
                return None
            finally:
                if outcome == "success" or outcome == "timeout":
                    log_event(
                        "gateway_call",
                        prompt_key=prompt_key,
                        version_key=version_key,
                        model=model,
                        tokens=tokens_usage,
                        latency_ms=round(latency_ms, 2),
                        outcome=outcome,
                    )

        return None

    async def complete_json_ex(self, **kwargs: Any) -> tuple[Any, str | None]:
        """Like complete_json but returns (result, error_text) for THIS call.

        The repair loop needs the rejection reason; reading ``self.last_error`` after the
        await would race with concurrent calls on this shared singleton."""
        _call_error.set(None)
        result = await self.complete_json(**kwargs)
        if result is not None:
            return result, None
        return None, (_call_error.get() or "no valid JSON returned")

    async def stream_text(
        self,
        *,
        prompt_key: str,
        messages: list[dict[str, Any]],
        model: str,
        timeout_s: float,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Streaming chat completion with OpenCode Go, OpenRouter, and Groq support.

        A stream that ends with no usable content but with reasoning (or cut off by the
        output limit) is retried ONCE on reasoning_fallback_model() at minimum reasoning. Safe
        to do transparently: nothing usable was yielded to the caller yet. Without the retry,
        such a stream yields 0 chunks -> 0 steps -> FALLBACK_LINE.
        """
        stats = _new_stream_stats()
        async for chunk in self._stream_text_once(prompt_key=prompt_key, messages=messages,
                                                  model=model, timeout_s=timeout_s,
                                                  temperature=temperature, stats=stats):
            yield chunk
        if not stream_was_reasoning_only(stats):
            return
        fallback = self.reasoning_fallback_model(model)
        log_event("llm_reasoning_only_retry", prompt_key=prompt_key, model=model,
                  fallback=fallback, mode="stream", reasoning_chars=stats["reasoning"],
                  finish=stats["finish"])
        retry = _new_stream_stats()
        async for chunk in self._stream_text_once(prompt_key=prompt_key, messages=messages,
                                                  model=fallback, timeout_s=timeout_s,
                                                  temperature=temperature, stats=retry,
                                                  minimal_reasoning=True):
            yield chunk
        if stream_was_reasoning_only(retry) or not visible_text(retry["text"]):
            log_event("llm_reasoning_only_exhausted", prompt_key=prompt_key, model=fallback,
                      mode="stream")

    async def _stream_text_once(
        self,
        *,
        prompt_key: str,
        messages: list[dict[str, Any]],
        model: str,
        timeout_s: float,
        temperature: float = 0.3,
        stats: dict[str, Any],
        minimal_reasoning: bool = False,
    ) -> AsyncIterator[str]:
        """One streaming attempt; content/reasoning/finish accounting goes into `stats`."""
        version_key = ACTIVE.get(prompt_key, prompt_key)
        start_time = time.monotonic()

        log_llm_stream_start(prompt_key, model)

        # Check OpenCode Go
        is_opencode = is_opencode_model(model, self.opencode_api_key)
        if is_opencode:
            norm_model = normalize_opencode_model(model)
            use_responses_api = norm_model.startswith("muse-") or "spark" in norm_model.lower()
            oc_client = self.get_client(api_key=self.opencode_api_key, base_url=self.opencode_base_url)
            oc_headers = {
                "Authorization": f"Bearer {self.opencode_api_key}",
                "x-opencode-session": str(uuid.uuid4()),
            }

            if use_responses_api:
                sys_msgs = [m.get("content", "") for m in messages if m.get("role") == "system"]
                instructions = "\n\n".join(sys_msgs) if sys_msgs else None
                other_msgs = [m for m in messages if m.get("role") != "system"]

                oc_payload: dict[str, Any] = {
                    "model": norm_model,
                    "stream": True,
                    "input": other_msgs if other_msgs else "",
                    "temperature": temperature,
                    **reasoning_params(model, "opencode_responses", minimal_reasoning),
                }
                if instructions:
                    oc_payload["instructions"] = instructions
                endpoint_url = "/responses"
            else:
                oc_payload = {
                    "model": norm_model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": True,
                    **reasoning_params(model, "opencode_chat", minimal_reasoning),
                    "max_tokens": settings.LLM_MAX_TOKENS,
                }
                endpoint_url = "/chat/completions"

            try:
                async with oc_client.stream(
                    "POST",
                    endpoint_url,
                    json=oc_payload,
                    timeout=timeout_s,
                    headers=oc_headers,
                ) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        err_text = err_body.decode("utf-8", errors="ignore")
                        log_llm_failure(prompt_key, model, (time.monotonic() - start_time) * 1000, f"HTTP {response.status_code}: {err_text}")
                        raise GatewayStreamError(f"HTTP {response.status_code}: {err_text}")

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                if use_responses_api:
                                    c_type = chunk.get("type") or ""
                                    if c_type == "response.output_text.delta":
                                        delta = chunk.get("delta", "")
                                        if delta:
                                            stats["text"] += delta
                                            yield delta
                                    elif "reasoning" in c_type and c_type.endswith(".delta"):
                                        stats["reasoning"] += len(chunk.get("delta", "") or "")
                                    elif c_type == "response.completed":
                                        break
                                else:
                                    delta = _chat_delta(chunk, stats)
                                    if delta:
                                        yield delta
                            except json.JSONDecodeError:
                                continue

                    latency_ms = (time.monotonic() - start_time) * 1000
                    log_llm_stream_done(prompt_key, model, latency_ms)
                    return
            except httpx.TimeoutException as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, f"Streaming timed out after {timeout_s}s")
                log_event("gateway_stream_timeout", prompt_key=prompt_key, latency_ms=latency_ms)
                raise GatewayStreamError(f"Streaming timed out after {timeout_s}s") from e
            except Exception as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, str(e))
                log_event("gateway_stream_error", prompt_key=prompt_key, latency_ms=latency_ms, error=str(e))
                if isinstance(e, GatewayStreamError):
                    raise e
                raise GatewayStreamError(f"OpenCode streaming failed: {e}") from e
            finally:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_event(
                    "gateway_stream_completed",
                    prompt_key=prompt_key,
                    version_key=version_key,
                    model=model,
                    latency_ms=round(latency_ms, 2),
                )

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        # Check OpenRouter
        is_openrouter = self.openrouter_api_key and (
            model.startswith("nvidia/")
            or (model.startswith("qwen/") and model != "qwen/qwen3.8-27b")
            or model.startswith("anthropic/")
            or model.startswith("google/")
        )
        if is_openrouter:
            or_client = self.get_client(api_key=self.openrouter_api_key, base_url=OPENROUTER_BASE_URL)
            try:
                async with or_client.stream(
                    "POST",
                    "/chat/completions",
                    json=payload,
                    timeout=timeout_s,
                    headers=self._auth(self.openrouter_api_key),
                ) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        err_text = err_body.decode("utf-8", errors="ignore")
                        log_llm_failure(prompt_key, model, (time.monotonic() - start_time) * 1000, f"HTTP {response.status_code}: {err_text}")
                        raise GatewayStreamError(f"HTTP {response.status_code}: {err_text}")

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = _chat_delta(chunk, stats)
                                if delta:
                                    yield delta
                            except json.JSONDecodeError:
                                continue

                    latency_ms = (time.monotonic() - start_time) * 1000
                    log_llm_stream_done(prompt_key, model, latency_ms)
                    return
            except httpx.TimeoutException as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, f"Streaming timed out after {timeout_s}s")
                log_event("gateway_stream_timeout", prompt_key=prompt_key, latency_ms=latency_ms)
                raise GatewayStreamError(f"Streaming timed out after {timeout_s}s") from e
            except Exception as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, str(e))
                log_event("gateway_stream_error", prompt_key=prompt_key, latency_ms=latency_ms, error=str(e))
                if isinstance(e, GatewayStreamError):
                    raise e
                raise GatewayStreamError(f"OpenRouter streaming failed: {e}") from e
            finally:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_event(
                    "gateway_stream_completed",
                    prompt_key=prompt_key,
                    version_key=version_key,
                    model=model,
                    latency_ms=round(latency_ms, 2),
                )

        groq_payload = {**payload, **reasoning_params(model, "groq", minimal_reasoning)}
        keys_to_try = [k for k in self.rotator.keys] if self.rotator.keys else [self.api_key]
        last_error: Exception | None = None

        for _ in range(max(1, len(keys_to_try))):
            current_key = self.rotator.get_key() or self.api_key
            client = self.get_client(api_key=current_key, base_url=self.base_url)

            try:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    json=groq_payload,
                    timeout=timeout_s,
                    headers=self._auth(current_key),
                ) as response:
                    if response.status_code == 429:
                        self.rotator.report_rate_limit(current_key)
                        log_llm_rate_limit(prompt_key, current_key[-4:])
                        log_event("gateway_stream_rate_limited", key_suffix=current_key[-4:], prompt_key=prompt_key)
                        continue

                    if response.status_code != 200:
                        err_body = await response.aread()
                        err_text = err_body.decode('utf-8', errors='ignore')
                        log_llm_failure(prompt_key, model, (time.monotonic() - start_time) * 1000, f"HTTP {response.status_code}: {err_text}")
                        raise GatewayStreamError(f"HTTP {response.status_code}: {err_text}")

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = _chat_delta(chunk, stats)
                                if delta:
                                    yield delta
                            except json.JSONDecodeError:
                                continue

                    latency_ms = (time.monotonic() - start_time) * 1000
                    log_llm_stream_done(prompt_key, model, latency_ms)
                    return  # Success, exit generator

            except httpx.TimeoutException as e:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, f"Streaming timed out after {timeout_s}s")
                log_event("gateway_stream_timeout", prompt_key=prompt_key, latency_ms=latency_ms)
                raise GatewayStreamError(f"Streaming timed out after {timeout_s}s") from e
            except Exception as e:
                last_error = e
                latency_ms = (time.monotonic() - start_time) * 1000
                log_llm_failure(prompt_key, model, latency_ms, str(e))
                log_event("gateway_stream_error", prompt_key=prompt_key, latency_ms=latency_ms, error=str(e))
                if isinstance(e, GatewayStreamError):
                    raise e
                continue
            finally:
                latency_ms = (time.monotonic() - start_time) * 1000
                log_event(
                    "gateway_stream_completed",
                    prompt_key=prompt_key,
                    version_key=version_key,
                    model=model,
                    latency_ms=round(latency_ms, 2),
                )

        if last_error:
            raise GatewayStreamError(f"Streaming failed: {last_error}") from last_error
        raise GatewayStreamError("All available gateway keys were rate limited.")


# Default singleton instance
gateway = GroqGateway()

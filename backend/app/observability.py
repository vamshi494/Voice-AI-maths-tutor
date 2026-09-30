# app/observability.py
import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("ai_math_tutor")
logging.basicConfig(level=logging.INFO)

# Check if logfire is available
try:
    import logfire
    _LOGFIRE_AVAILABLE = True
except ImportError:
    logfire = None  # type: ignore
    _LOGFIRE_AVAILABLE = False

_CONFIGURED = False


def setup_observability() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    from app.config import settings

    if settings.LOGFIRE_TOKEN and _LOGFIRE_AVAILABLE:
        try:
            logfire.configure(
                token=settings.LOGFIRE_TOKEN,
                service_name="ai-math-tutor",
                inspect_arguments=False,
                send_to_logfire="always",
            )
            try:
                logfire.instrument_httpx()
            except Exception as ie:
                logger.debug(f"Could not instrument httpx: {ie}")

            _CONFIGURED = True
            print("\033[92m🔥 [LOGFIRE] Configured successfully! Project dashboard active.\033[0m", flush=True)
            print("\033[96m🔗 [LOGFIRE DASHBOARD]\033[0m \033[4mhttps://logfire-us.pydantic.dev/vamshivamshi494/starter-project\033[0m", flush=True)
            logger.info("Logfire configured successfully")
        except Exception as e:
            logger.warning(f"Failed to configure Logfire: {e}")

    if settings.LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
        os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
        os.environ["LANGSMITH_TRACING"] = str(settings.LANGSMITH_TRACING).lower()
        os.environ["LANGSMITH_USE_DAEMON"] = "true"
        logger.info("LangSmith tracing environment set")


def instrument_fastapi_app(app: Any) -> None:
    """Instrument FastAPI application with Logfire."""
    if _LOGFIRE_AVAILABLE and logfire is not None:
        try:
            logfire.instrument_fastapi(app)
            logger.info("FastAPI instrumented with Logfire")
        except Exception as e:
            logger.debug(f"Could not instrument FastAPI with Logfire: {e}")


# Auto-configure on import
setup_observability()


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None) -> Generator[None, None, None]:
    """Context manager for tracing with Logfire and logger."""
    attrs = attributes or {}
    logger.info(f"SPAN START: {name} | {attrs}")
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            with logfire.span(name, **attrs):
                yield
        except Exception:
            raise
    else:
        yield
    logger.info(f"SPAN END: {name}")


def log_event(event_name: str, **kwargs: Any) -> None:
    """Log a named event (e.g. diagram_unverified_continue, authority_contradiction, etc.)."""
    logger.info(f"EVENT: {event_name} | {kwargs}")
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.info(event_name, **kwargs)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Terminal & Logfire LLM Call Monitors
# -----------------------------------------------------------------------------

def log_llm_start(prompt_key: str, model: str, prompt_len: int = 0) -> None:
    """Print clean visible notice when an LLM request is dispatched."""
    print(
        f"\033[94m🤖 [LLM CALL START]\033[0m \033[1m{prompt_key}\033[0m | Model: \033[33m{model}\033[0m"
        + (f" | Input size: {prompt_len} chars" if prompt_len else ""),
        flush=True,
    )
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.info("LLM Call Dispatched: {prompt_key}", prompt_key=prompt_key, model=model, prompt_len=prompt_len)
        except Exception:
            pass


def log_llm_success(
    prompt_key: str,
    model: str,
    latency_ms: float,
    tokens: dict[str, int] | None = None,
    preview: str = "",
) -> None:
    """Print visible green banner when an LLM call succeeds."""
    tok_str = ""
    if tokens:
        p_tok = tokens.get("prompt_tokens", 0)
        c_tok = tokens.get("completion_tokens", 0)
        t_tok = tokens.get("total_tokens", 0)
        tok_str = f" | Tokens: {c_tok} comp / {p_tok} prompt ({t_tok} total)"
    prev_str = f" | Output: {preview[:60]}..." if preview else ""
    print(
        f"\033[92m✅ [LLM CALL SUCCESS]\033[0m \033[1m{prompt_key}\033[0m | Model: \033[33m{model}\033[0m | Latency: \033[36m{latency_ms/1000.0:.2f}s\033[0m{tok_str}{prev_str}",
        flush=True,
    )
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.info(
                "LLM Call Succeeded: {prompt_key}",
                prompt_key=prompt_key,
                model=model,
                latency_ms=latency_ms,
                tokens=tokens or {},
            )
        except Exception:
            pass


def log_llm_failure(
    prompt_key: str,
    model: str,
    latency_ms: float,
    error: str,
    raw_response: str = "",
) -> None:
    """Print visible bright red banner when an LLM call fails with errors."""
    print(
        f"\033[91;1m❌ [LLM CALL FAILED]\033[0m \033[1m{prompt_key}\033[0m | Model: \033[33m{model}\033[0m | Latency: {latency_ms/1000.0:.2f}s",
        flush=True,
    )
    print(f"\033[91m   >>> Error: {error}\033[0m", flush=True)
    if raw_response:
        print(f"\033[90m   >>> Raw Response Snippet: {raw_response[:140]}...\033[0m", flush=True)
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.error(
                "LLM Call Failed: {prompt_key} - {error}",
                prompt_key=prompt_key,
                model=model,
                latency_ms=latency_ms,
                error=error,
                raw_preview=raw_response[:300],
            )
        except Exception:
            pass


def log_llm_rate_limit(prompt_key: str, key_suffix: str, wait_s: float = 60.0) -> None:
    """Print visible warning when rate limit occurs and rotation takes place."""
    print(
        f"\033[93m⚠️  [LLM 429 RATE LIMIT]\033[0m \033[1m{prompt_key}\033[0m | Key: ...{key_suffix} hit rate limit | Quarantining for {wait_s}s -> Rotating to next available key",
        flush=True,
    )
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.warn(
                "LLM Rate Limit (429): {prompt_key}",
                prompt_key=prompt_key,
                key_suffix=key_suffix,
                quarantine_s=wait_s,
            )
        except Exception:
            pass


def log_llm_stream_start(prompt_key: str, model: str) -> None:
    """Print visible stream start notice."""
    print(f"\033[96m🌊 [LLM STREAM START]\033[0m \033[1m{prompt_key}\033[0m | Model: \033[33m{model}\033[0m", flush=True)
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.info("LLM Stream Started: {prompt_key}", prompt_key=prompt_key, model=model)
        except Exception:
            pass


def log_llm_stream_done(prompt_key: str, model: str, latency_ms: float) -> None:
    """Print visible stream completion notice."""
    print(
        f"\033[92m🌊 [LLM STREAM COMPLETED]\033[0m \033[1m{prompt_key}\033[0m | Model: \033[33m{model}\033[0m | Latency: \033[36m{latency_ms/1000.0:.2f}s\033[0m",
        flush=True,
    )
    if _LOGFIRE_AVAILABLE and logfire is not None and _CONFIGURED:
        try:
            logfire.info("LLM Stream Done: {prompt_key}", prompt_key=prompt_key, model=model, latency_ms=latency_ms)
        except Exception:
            pass

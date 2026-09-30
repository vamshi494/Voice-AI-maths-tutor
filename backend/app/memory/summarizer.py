# app/memory/summarizer.py
"""Background page summarizer.

`summarize` is awaited only inside a task the manager spawns as a turn closes
(`_spawn_summary`), never on the speaking path; a cancelled or failed call is
dropped, the lesson itself is unaffected.
"""
import time

from app.config import settings
from app.contracts.memory import SummaryOut
from app.gateway.groq_client import GroqGateway
from app.observability import log_event

SUMMARIZE_TIMEOUT_S = 15.0


async def summarize(
    *,
    page_title: str,
    rows: list[dict[str, str]],
    heard_text: str,
    doubts: str,
    previous: str,
    gw: GroqGateway | None = None,
) -> SummaryOut | None:
    """One `memory.summary.v1` call (MODEL_FAST, 15 s); None on any failure."""
    gw_client = gw or GroqGateway()
    listing = "\n".join(f"{r.get('row_id', '')}: {r.get('text', '')}" for r in rows)
    fmt_args = {
        "previous_summary": previous or "none",
        "page_title": page_title or "none",
        "rows": listing or "none",
        "heard_text": heard_text or "none",
        "doubts": doubts or "none",
    }
    start = time.monotonic()
    try:
        out = await gw_client.complete_json(
            prompt_key="memory.summary",
            schema=SummaryOut,
            model=settings.MODEL_FAST,
            user="Update the session notes.",
            timeout_s=SUMMARIZE_TIMEOUT_S,
            temperature=0.0,
            fmt_args=fmt_args,
        )
    except Exception as e:
        log_event("summary_failed", error=str(e))
        return None
    log_event("summary_done", latency_ms=round((time.monotonic() - start) * 1000.0, 1))
    return out

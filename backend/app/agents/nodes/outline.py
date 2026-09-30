# app/agents/nodes/outline.py
"""Chapter outline node.

`wants_outline` is the deterministic gate (intent or topic cues minus equations);
`outline_lesson` is one MODEL_FAST call with the `outline.v1` prompt and OUTLINE_TIMEOUT_S.
It never raises and never blocks a lesson: any failure returns None and the caller keeps
today's single-page pipeline.
"""
import asyncio
import re

from app.config import settings
from app.contracts.lesson import LessonPlan
from app.gateway.groq_client import GroqGateway, gateway
from app.observability import log_event
from app.prompts.registry import ACTIVE

TOPIC_CUES = re.compile(r"\b(teach|explain|chapter|lesson on|introduce|what is|what are|theorem)\b", re.I)
EQUATION = re.compile(r"[a-z]\s*[\^²³]?\s*[+\-*/=]|=\s*[-\d]", re.I)


def wants_outline(question: str, intent: str) -> bool:
    """An explicit topic request always outlines; a spoken question only when it names a
    topic/theorem and carries no equation."""
    return intent == "topic" or (bool(TOPIC_CUES.search(question or ""))
                                 and not EQUATION.search(question or ""))


async def outline_lesson(question: str, memory_summary: str, gw: GroqGateway | None = None) -> LessonPlan | None:
    """MODEL_FAST outline; None on any failure (timeout, malformed JSON, zero pages)."""
    gw_client = gw or gateway
    try:
        plan, parse_error = await asyncio.wait_for(
            gw_client.complete_json_ex(
                prompt_key=ACTIVE["outline"],
                schema=LessonPlan,
                model=settings.MODEL_FAST,
                user=question,
                timeout_s=settings.OUTLINE_TIMEOUT_S,
                fmt_args={"memory_summary": memory_summary or "none"},
            ),
            timeout=settings.OUTLINE_TIMEOUT_S + 1.0,   # defence in depth over the HTTP timeout
        )
    except asyncio.TimeoutError:
        log_event("outline_failed", error="timeout", timeout_s=settings.OUTLINE_TIMEOUT_S)
        return None
    except Exception as e:
        log_event("outline_failed", error=f"{type(e).__name__}: {e}")
        return None
    if plan is None:
        log_event("outline_failed", error=(parse_error or "no JSON")[:200])
        return None
    log_event("outline_built", scope=plan.scope, pages=len(plan.pages))
    return plan

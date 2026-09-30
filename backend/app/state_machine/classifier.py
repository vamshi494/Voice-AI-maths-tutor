# app/state_machine/classifier.py
"""Interrupt classification and figure need detection.

classify_interrupt:
  - Input: topic, last_teacher_line, lesson_on_board, doubt_pending, utterance
  - Model: MODEL_CLASSIFIER, temperature=0, timeout CLASSIFIER_TIMEOUT_S
  - Fallback on timeout/error: "doubt" if lesson_on_board else "new_question"
  - Logging: interrupt_classified {label, latency_ms, prev_state}

classify_figure_need:
  - Input: on_board_entities, marked_reference, doubt_text
  - Fallback on timeout/error: requires_new_figure=False
"""
import re
import time
from typing import Any
from app.config import settings
from app.contracts.classifier import FigureNeedDecision, InterruptDecision, InterruptLabel
from app.gateway.groq_client import GroqGateway
from app.observability import log_event, logger


# The fast paths decide WITHOUT the LLM, so they must match the WHOLE utterance: matching a
# prefix would turn any "Hi, can you teach me ..." into a mic-check backchannel, and matching a
# phrase anywhere would end the session on "... the amount in the account after 2 years?".
_END_EXACT = {"end", "exit", "quit", "bye", "stop", "end call", "end the call", "close call",
              "hang up", "disconnect", "goodbye", "bye bye", "ok bye", "okay bye", "okay goodbye"}
_END_PHRASE_RE = re.compile(
    r"\b(end(\s+the)?\s+(call|class|session)|hang\s*up|stop(\s+the)?\s+(call|class|session)"
    r"|bye\s*bye|goodbye|done\s+for\s+today)\b")
_END_PHRASE_MAX_WORDS = 7
_MIC_CHECK_RE = re.compile(
    r"(?:(?:hello|hi|hii|hey|helo|namaste)\s*)*"
    r"(?:(?:sir|ma'?am|madam|teacher|vamshi|tutor|there)\s*)?"
    r"(?:can you hear me|am i audible|is someone there|is anyone there|are you there"
    r"|testing(?:\s+testing)*(?:\s+\d+)*|mic check|one two three)?")


def _normalize_utterance(utterance: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", (utterance or "").lower()).split())


def is_end_session_command(utterance: str) -> bool:
    norm = _normalize_utterance(utterance)
    if norm in _END_EXACT:
        return True
    return bool(_END_PHRASE_RE.search(norm)) and len(norm.split()) <= _END_PHRASE_MAX_WORDS


def is_mic_check(utterance: str) -> bool:
    norm = _normalize_utterance(utterance)
    return bool(norm) and _MIC_CHECK_RE.fullmatch(norm) is not None


async def classify_interrupt(
    *,
    topic: str,
    last_teacher_line: str,
    lesson_on_board: bool,
    doubt_pending: bool,
    utterance: str,
    pointed_target: str = "",
    prev_state: str = "",
    lesson_paused: bool = False,
    input_mode: str = "spoken",
    gateway: GroqGateway | None = None,
) -> InterruptLabel:
    """Classify student interrupt into one of 6 labels with safe fallback.

    `input_mode="typed"` skips the mic-check fast path: typed "hello" is a real message, and
    typed input is never a backchannel.
    """
    gw = gateway or GroqGateway()
    start_time = time.monotonic()

    utterance_text = utterance
    if pointed_target:
        utterance_text = f"{utterance} (Student pointed to on-board element: {pointed_target})"

    # Fast path for clear call-ending or exit requests (a short command, not a phrase in maths)
    if is_end_session_command(utterance):
        log_event(
            "interrupt_classified",
            label="end_session",
            latency_ms=0.0,
            prev_state=prev_state,
            outcome="fast_path",
        )
        return "end_session"

    # Fast path for mic checks / greetings / ambient vocalizations (spoken only: typed
    # "hello" is a real message, not a mic check).
    if input_mode != "typed" and is_mic_check(utterance):
        log_event(
            "interrupt_classified",
            label="backchannel",
            latency_ms=0.0,
            prev_state=prev_state,
            outcome="mic_check_backchannel",
        )
        return "backchannel"

    fmt_args = {
        "topic": topic or "this question",
        "last_teacher_line": last_teacher_line or "none",
        "lesson_on_board": str(lesson_on_board).lower(),
        "doubt_pending": str(doubt_pending).lower(),
        "lesson_paused": str(lesson_paused).lower(),
        "input_mode": input_mode,
        "utterance": utterance_text,
    }

    try:
        decision: InterruptDecision | None = await gw.complete_json(
            prompt_key="classifier.interrupt",
            schema=InterruptDecision,
            model=settings.MODEL_CLASSIFIER,
            user="Classify the student utterance.",
            timeout_s=settings.CLASSIFIER_TIMEOUT_S,
            temperature=0.0,
            fmt_args=fmt_args,
        )

        latency_ms = (time.monotonic() - start_time) * 1000.0

        if decision is not None and decision.label in (
            "backchannel", "affirmation", "doubt", "new_question", "end_session", "off_topic"
        ):
            # Safe tie-breaking rule:
            # When LESSON ON BOARD is false, there is no lesson on board to doubt, so any math inquiry is a new_question
            label = decision.label
            if not lesson_on_board and label == "doubt":
                label = "new_question"

            log_event(
                "interrupt_classified",
                label=label,
                latency_ms=latency_ms,
                prev_state=prev_state,
                outcome="success",
            )
            return label

    except Exception as e:
        logger.warning(f"Interrupt classification failed with error: {e}")

    # Fallback on timeout or error: doubt if lesson is on board, else new_question
    fallback_label: InterruptLabel = "doubt" if lesson_on_board else "new_question"
    latency_ms = (time.monotonic() - start_time) * 1000.0
    log_event(
        "interrupt_classified",
        label=fallback_label,
        latency_ms=latency_ms,
        prev_state=prev_state,
        outcome="fallback",
    )
    return fallback_label


async def classify_figure_need(
    *,
    on_board_entities: str,
    marked_reference: str,
    doubt_text: str,
    gateway: GroqGateway | None = None,
) -> bool:
    """Determine whether answering student's doubt needs a new figure."""
    gw = gateway or GroqGateway()
    fmt_args = {
        "on_board_entities": on_board_entities or "none",
        "marked_reference": marked_reference or "none",
        "doubt_text": doubt_text,
    }

    try:
        decision: FigureNeedDecision | None = await gw.complete_json(
            prompt_key="classifier.figure_need",
            schema=FigureNeedDecision,
            model=settings.MODEL_CLASSIFIER,
            user="Determine if new figure is needed.",
            timeout_s=settings.CLASSIFIER_TIMEOUT_S,
            temperature=0.0,
            fmt_args=fmt_args,
        )

        if decision is not None:
            log_event("figure_need_classified", requires_new_figure=decision.requires_new_figure, reason=decision.reason)
            return decision.requires_new_figure

    except Exception as e:
        logger.warning(f"Figure need classification failed with error: {e}")

    # Fallback on timeout or error: use requires_new_figure=False
    log_event("figure_need_classified", requires_new_figure=False, reason="fallback")
    return False

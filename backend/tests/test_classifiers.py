# backend/tests/test_classifiers.py
import pytest
from unittest.mock import AsyncMock

from app.contracts.classifier import FigureNeedDecision, InterruptDecision
from app.state_machine.classifier import classify_figure_need, classify_interrupt


@pytest.mark.asyncio
async def test_classify_interrupt_success():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(
        return_value=InterruptDecision(label="doubt")
    )

    label = await classify_interrupt(
        topic="quadratic_equations",
        last_teacher_line="Let's find the discriminant.",
        lesson_on_board=True,
        doubt_pending=False,
        utterance="Wait, why is b squared positive?",
        gateway=mock_gw,
    )
    assert label == "doubt"


@pytest.mark.asyncio
async def test_classify_interrupt_fallback_with_lesson():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(return_value=None)

    label = await classify_interrupt(
        topic="quadratic_equations",
        last_teacher_line="Let's calculate.",
        lesson_on_board=True,
        doubt_pending=False,
        utterance="xyz not clear",
        gateway=mock_gw,
    )
    assert label == "doubt"


@pytest.mark.asyncio
async def test_classify_interrupt_fallback_without_lesson():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(side_effect=Exception("Timeout"))

    label = await classify_interrupt(
        topic="",
        last_teacher_line="",
        lesson_on_board=False,
        doubt_pending=False,
        utterance="solve 2x + 3 = 7",
        gateway=mock_gw,
    )
    assert label == "new_question"


@pytest.mark.asyncio
async def test_classify_interrupt_doubt_mapped_to_new_question_when_no_lesson():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(
        return_value=InterruptDecision(label="doubt")
    )

    label = await classify_interrupt(
        topic="",
        last_teacher_line="",
        lesson_on_board=False,
        doubt_pending=False,
        utterance="How to find roots of equality questions?",
        gateway=mock_gw,
    )
    assert label == "new_question"


@pytest.mark.asyncio
async def test_typed_input_skips_mic_check():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(return_value=InterruptDecision(label="new_question"))

    label = await classify_interrupt(
        topic="",
        last_teacher_line="",
        lesson_on_board=False,
        doubt_pending=False,
        utterance="hello can you hear me",
        input_mode="typed",
        gateway=mock_gw,
    )
    assert label == "new_question"
    mock_gw.complete_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_figure_need_success():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(
        return_value=FigureNeedDecision(requires_new_figure=True, reason="obtuse triangle needed")
    )

    need = await classify_figure_need(
        on_board_entities="A: point\nB: point",
        marked_reference="triangle",
        doubt_text="what if the triangle were obtuse?",
        gateway=mock_gw,
    )
    assert need is True


@pytest.mark.asyncio
async def test_classify_figure_need_fallback():
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(return_value=None)

    need = await classify_figure_need(
        on_board_entities="A: point",
        marked_reference="none",
        doubt_text="what does step 2 mean?",
        gateway=mock_gw,
    )
    assert need is False


async def _spoken(utterance: str, llm_label: str = "new_question", lesson_on_board: bool = False):
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(return_value=InterruptDecision(label=llm_label))
    label = await classify_interrupt(topic="", last_teacher_line="", lesson_on_board=lesson_on_board,
                                     doubt_pending=False, utterance=utterance, gateway=mock_gw)
    return label, mock_gw.complete_json.await_count


@pytest.mark.asyncio
async def test_greeting_prefixed_question_reaches_the_classifier():
    """A question that starts with hi/hey/hello still reaches the classifier; it is not taken
    for a mic-check backchannel."""
    for q in ("Hi, can you teach me the Pythagoras theorem?",
              "Hello what is the area of a circle of radius 7",
              "hey teacher, explain similar triangles"):
        label, calls = await _spoken(q)
        assert (label, calls) == ("new_question", 1), q


@pytest.mark.asyncio
async def test_real_mic_checks_still_fast_path():
    for q in ("hello", "Hi!", "hello, can you hear me?", "hey are you there", "testing testing",
              "mic check", "hello sir can you hear me"):
        label, calls = await _spoken(q)
        assert (label, calls) == ("backchannel", 0), q


@pytest.mark.asyncio
async def test_maths_with_end_words_is_not_end_session():
    """End-session phrases inside a maths problem (such as "in the account") do not end the
    session."""
    for q in ("Rahul deposits 5000 rupees, what is the amount in the account after 2 years?",
              "why does the line disconnect at x equals 2 in this graph",
              "the train stops the journey after 3 hours, find its speed"):
        label, calls = await _spoken(q, llm_label="doubt", lesson_on_board=True)
        assert label != "end_session", q
        assert calls == 1, q


@pytest.mark.asyncio
async def test_real_goodbyes_still_fast_path():
    for q in ("bye", "End the call.", "okay goodbye", "stop the class", "I'm done for today",
              "hang up", "please end the session"):
        label, calls = await _spoken(q, lesson_on_board=True)
        assert (label, calls) == ("end_session", 0), q

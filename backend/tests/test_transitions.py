# backend/tests/test_transitions.py
from app.contracts.agent_state import ConvState, AgentState, PausedLesson
from app.state_machine.states import ConvEvent, find_matching_rule


def test_row_1_new_question():
    rule = find_matching_rule(ConvState.IDLE, ConvEvent.TYPED_QUESTION)
    assert rule is not None
    assert rule.rule_id == 1
    assert rule.to_state == ConvState.GRAPH_RUNNING

    rule2 = find_matching_rule(ConvState.IDLE, ConvEvent.CLS_NEW_QUESTION)
    assert rule2 is not None
    assert rule2.rule_id == 1


def test_row_2_and_3_speech_cycle():
    r2 = find_matching_rule(ConvState.GRAPH_RUNNING, ConvEvent.SPEECH_STARTED)
    assert r2 is not None
    assert r2.rule_id == 2
    assert r2.to_state == ConvState.AGENT_SPEAKING

    r3 = find_matching_rule(ConvState.AGENT_SPEAKING, ConvEvent.SPEECH_ENDED)
    assert r3 is not None
    assert r3.rule_id == 3
    assert r3.to_state == ConvState.IDLE


def test_row_4_and_5_interrupt_detection():
    for st in [ConvState.AGENT_SPEAKING, ConvState.GRAPH_RUNNING, ConvState.TASK_CORRECTING, ConvState.IDLE]:
        r4 = find_matching_rule(st, ConvEvent.VAD_START)
        assert r4 is not None
        assert r4.rule_id == 4
        assert r4.to_state == ConvState.INTERRUPT_DETECTED

    r5 = find_matching_rule(ConvState.INTERRUPT_DETECTED, ConvEvent.USER_TURN_DONE)
    assert r5 is not None
    assert r5.rule_id == 5
    assert r5.to_state == ConvState.INTERRUPT_CLASSIFYING


def test_row_8_and_9_affirmation_predicate():
    class DummyContext:
        doubt_awaiting_resolution = True

    # When awaiting resolution -> RESUMING (Row 8)
    r8 = find_matching_rule(ConvState.INTERRUPT_CLASSIFYING, ConvEvent.CLS_AFFIRMATION, DummyContext())
    assert r8 is not None
    assert r8.rule_id == 8
    assert r8.to_state == ConvState.RESUMING

    # When not awaiting -> prev_state (Row 9)
    DummyContext.doubt_awaiting_resolution = False
    r9 = find_matching_rule(ConvState.INTERRUPT_CLASSIFYING, ConvEvent.CLS_AFFIRMATION, DummyContext())
    assert r9 is not None
    assert r9.rule_id == 9
    assert r9.to_state is None


def test_row_11_marked_doubt_from_any_non_terminal():
    for st in ConvState:
        if st == ConvState.TASK_CANCELLED:
            continue
        r11 = find_matching_rule(st, ConvEvent.MARKED_DOUBT)
        assert r11 is not None
        assert r11.to_state == ConvState.TASK_PAUSED


def test_row_23_terminal_ignores_everything():
    for ev in ConvEvent:
        r23 = find_matching_rule(ConvState.TASK_CANCELLED, ev)
        assert r23 is not None
        assert r23.rule_id == 23
        assert r23.to_state == ConvState.TASK_CANCELLED


def test_unlisted_pair_ignored_and_logged_never_raises():
    # E.g. GRAPH_RUNNING + CLS_BACKCHANNEL is unlisted
    rule = find_matching_rule(ConvState.GRAPH_RUNNING, ConvEvent.CLS_BACKCHANNEL)
    assert rule is None

    # E.g. IDLE + REDIRECT_DONE is unlisted
    rule = find_matching_rule(ConvState.IDLE, ConvEvent.REDIRECT_DONE)
    assert rule is None

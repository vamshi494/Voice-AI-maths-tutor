# app/state_machine/states.py
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from app.contracts.agent_state import ConvState


class ConvEvent(str, Enum):
    TURN_REQUESTED = "turn_requested"          # manager dispatched a TurnRequest
    SPEECH_STARTED = "speech_started"          # agent_state_changed -> speaking
    SPEECH_ENDED = "speech_ended"              # turn's last step finished playing
    VAD_START = "vad_start"                    # user_state_changed -> speaking
    USER_TURN_DONE = "user_turn_done"          # on_user_turn_completed (final transcript)
    CLS_BACKCHANNEL = "cls_backchannel"
    CLS_AFFIRMATION = "cls_affirmation"
    CLS_DOUBT = "cls_doubt"
    CLS_NEW_QUESTION = "cls_new_question"
    CLS_END = "cls_end"
    CLS_OFF_TOPIC = "cls_off_topic"
    MARKED_DOUBT = "marked_doubt"              # rpc submit_doubt
    TYPED_QUESTION = "typed_question"          # rpc submit_question (new lesson / topic)
    TYPED_TEXT = "typed_text"                  # rpc submit_question while a lesson is on the board
    CONTINUE = "continue"                      # rpc continue_lesson
    MARKER_ARMED = "marker_armed"
    MARKER_DISARMED = "marker_disarmed"
    DOUBT_SETTLED = "doubt_settled"            # audio + pen settled or 2500 ms timeout
    REDIRECT_DONE = "redirect_done"
    END = "end"                                # rpc end_session


NON_TERMINAL_STATES = frozenset(
    s for s in ConvState if s != ConvState.TASK_CANCELLED
)


def lesson_unheard(st: Any) -> bool:
    """A lesson whose first step has not been heard yet (rules 10/29/30)."""
    return (getattr(st, "active_turn_kind", None) == "lesson"
            and getattr(st, "page_index", 0) == 0
            and getattr(st, "heard_step_index", -1) < 0
            and getattr(st, "paused_lesson", None) is None
            and getattr(st, "published_upto", -1) < 1)


def has_next_page(st: Any) -> bool:
    """The active chapter has a page after the current one (rule 29 vs 3)."""
    return bool(getattr(st, "has_next_page", False))


@dataclass(frozen=True)
class TransitionRule:
    rule_id: int
    from_states: frozenset[ConvState]
    event: ConvEvent
    to_state: ConvState | None                 # None if dynamically determined (e.g. prev_state)
    predicate: Callable[[Any], bool] | None = None
    description: str = ""


# The transition definitions.
TRANSITION_RULES: list[TransitionRule] = [
    # 1: TYPED_QUESTION / CLS_NEW_QUESTION -> GRAPH_RUNNING
    TransitionRule(
        rule_id=1,
        from_states=NON_TERMINAL_STATES,
        event=ConvEvent.TYPED_QUESTION,
        to_state=ConvState.GRAPH_RUNNING,
        description="Row 1a: TYPED_QUESTION -> GRAPH_RUNNING",
    ),
    TransitionRule(
        rule_id=1,
        from_states=frozenset({ConvState.IDLE, ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_NEW_QUESTION,
        to_state=ConvState.GRAPH_RUNNING,
        description="Row 1b: CLS_NEW_QUESTION -> GRAPH_RUNNING",
    ),
    # 2: GRAPH_RUNNING + SPEECH_STARTED -> AGENT_SPEAKING
    TransitionRule(
        rule_id=2,
        from_states=frozenset({ConvState.GRAPH_RUNNING, ConvState.TASK_REDIRECTED}),
        event=ConvEvent.SPEECH_STARTED,
        to_state=ConvState.AGENT_SPEAKING,
        description="Row 2: GRAPH_RUNNING + SPEECH_STARTED -> AGENT_SPEAKING",
    ),
    # 3: AGENT_SPEAKING / GRAPH_RUNNING / TASK_CORRECTING / TASK_REDIRECTED + SPEECH_ENDED -> IDLE
    #    Only when the chapter has no further page; rule 29 handles the page boundary.
    TransitionRule(
        rule_id=3,
        from_states=frozenset({
            ConvState.AGENT_SPEAKING,
            ConvState.GRAPH_RUNNING,
            ConvState.TASK_CORRECTING,
            ConvState.TASK_REDIRECTED,
            ConvState.RESUMING,
        }),
        event=ConvEvent.SPEECH_ENDED,
        to_state=ConvState.IDLE,
        predicate=lambda st: not has_next_page(st),
        description="Row 3: ... + SPEECH_ENDED -> IDLE (last page / single-page lesson)",
    ),
    # 4: AGENT_SPEAKING, GRAPH_RUNNING, TASK_CORRECTING, IDLE, INTERRUPT_DETECTED, TASK_REDIRECTED + VAD_START -> INTERRUPT_DETECTED
    TransitionRule(
        rule_id=4,
        from_states=frozenset({
            ConvState.AGENT_SPEAKING,
            ConvState.GRAPH_RUNNING,
            ConvState.TASK_CORRECTING,
            ConvState.TASK_REDIRECTED,
            ConvState.IDLE,
            ConvState.INTERRUPT_DETECTED,
            ConvState.RESUMING,
        }),
        event=ConvEvent.VAD_START,
        to_state=ConvState.INTERRUPT_DETECTED,
        description="Row 4: Speech detected -> INTERRUPT_DETECTED",
    ),
    # 5: INTERRUPT_DETECTED / IDLE / TASK_REDIRECTED + USER_TURN_DONE -> INTERRUPT_CLASSIFYING
    TransitionRule(
        rule_id=5,
        from_states=frozenset({
            ConvState.INTERRUPT_DETECTED,
            ConvState.IDLE,
            ConvState.TASK_REDIRECTED,
        }),
        event=ConvEvent.USER_TURN_DONE,
        to_state=ConvState.INTERRUPT_CLASSIFYING,
        description="Row 5: USER_TURN_DONE -> INTERRUPT_CLASSIFYING",
    ),
    # 6: INTERRUPT_CLASSIFYING + VAD_START / USER_TURN_DONE -> same
    TransitionRule(
        rule_id=6,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.VAD_START,
        to_state=ConvState.INTERRUPT_CLASSIFYING,
        description="Row 6a: More speech during classifying -> append transcript",
    ),
    TransitionRule(
        rule_id=6,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.USER_TURN_DONE,
        to_state=ConvState.INTERRUPT_CLASSIFYING,
        description="Row 6b: User turn done during classifying -> append transcript",
    ),
    # 7: INTERRUPT_CLASSIFYING + CLS_BACKCHANNEL -> INTERRUPT_IGNORED -> prev_state
    TransitionRule(
        rule_id=7,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_BACKCHANNEL,
        to_state=None,  # dynamically resolves to prev_state (via INTERRUPT_IGNORED)
        description="Row 7: Backchannel -> INTERRUPT_IGNORED -> prev_state",
    ),
    # 8: INTERRUPT_CLASSIFYING + CLS_AFFIRMATION, doubt_awaiting_resolution -> RESUMING
    TransitionRule(
        rule_id=8,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_AFFIRMATION,
        to_state=ConvState.RESUMING,
        predicate=lambda st: getattr(st, "doubt_awaiting_resolution", False),
        description="Row 8: Affirmation when doubt awaiting -> RESUMING",
    ),
    # 9: INTERRUPT_CLASSIFYING + CLS_AFFIRMATION, not awaiting -> prev_state
    TransitionRule(
        rule_id=9,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_AFFIRMATION,
        to_state=None,  # dynamically resolves to prev_state
        predicate=lambda st: not getattr(st, "doubt_awaiting_resolution", False),
        description="Row 9: Affirmation when not awaiting -> prev_state (no-op)",
    ),
    # 10: INTERRUPT_CLASSIFYING + CLS_DOUBT -> TASK_PAUSED (a real doubt, not an amendment)
    TransitionRule(
        rule_id=10,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_DOUBT,
        to_state=ConvState.TASK_PAUSED,
        predicate=lambda st: not lesson_unheard(st),
        description="Row 10: Doubt classified -> TASK_PAUSED",
    ),
    # 30: a doubt before the first heard step amends the lesson question instead
    TransitionRule(
        rule_id=30,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_DOUBT,
        to_state=ConvState.GRAPH_RUNNING,
        predicate=lesson_unheard,
        description="Row 30: Doubt before the first heard step -> amend the question",
    ),
    # 29: the page's speech ended and the chapter has a next page -> page turn
    TransitionRule(
        rule_id=29,
        from_states=frozenset({
            ConvState.AGENT_SPEAKING,
            ConvState.GRAPH_RUNNING,
            ConvState.RESUMING,
        }),
        event=ConvEvent.SPEECH_ENDED,
        to_state=ConvState.GRAPH_RUNNING,
        predicate=has_next_page,
        description="Row 29: page ended, chapter continues -> GRAPH_RUNNING (advance_page)",
    ),
    # 11: any non-terminal EXCEPT TASK_PAUSED + MARKED_DOUBT -> TASK_PAUSED
    TransitionRule(
        rule_id=11,
        from_states=NON_TERMINAL_STATES - {ConvState.TASK_PAUSED},
        event=ConvEvent.MARKED_DOUBT,
        to_state=ConvState.TASK_PAUSED,
        description="Row 11: Marked doubt -> TASK_PAUSED",
    ),
    # 12: TASK_PAUSED + DOUBT_SETTLED -> dynamic (aggregate; resume; else TASK_CORRECTING)
    TransitionRule(
        rule_id=12,
        from_states=frozenset({ConvState.TASK_PAUSED}),
        event=ConvEvent.DOUBT_SETTLED,
        to_state=None,
        description="Row 12: DOUBT_SETTLED -> aggregate / retract",
    ),
    # 13: TASK_CORRECTING + SPEECH_STARTED -> AGENT_SPEAKING
    TransitionRule(
        rule_id=13,
        from_states=frozenset({ConvState.TASK_CORRECTING}),
        event=ConvEvent.SPEECH_STARTED,
        to_state=ConvState.AGENT_SPEAKING,
        description="Row 13: Doubt speech started -> AGENT_SPEAKING (doubt_awaiting_resolution=True)",
    ),
    # 14: INTERRUPT_CLASSIFYING + CLS_NEW_QUESTION -> GRAPH_RUNNING
    TransitionRule(
        rule_id=14,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_NEW_QUESTION,
        to_state=ConvState.GRAPH_RUNNING,
        description="Row 14: New question -> GRAPH_RUNNING (supersede)",
    ),
    # 15: INTERRUPT_CLASSIFYING + CLS_OFF_TOPIC -> TASK_REDIRECTED
    TransitionRule(
        rule_id=15,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_OFF_TOPIC,
        to_state=ConvState.TASK_REDIRECTED,
        description="Row 15: Off-topic -> TASK_REDIRECTED (say template)",
    ),
    # 16: TASK_REDIRECTED + REDIRECT_DONE -> AGENT_SPEAKING or IDLE
    TransitionRule(
        rule_id=16,
        from_states=frozenset({ConvState.TASK_REDIRECTED}),
        event=ConvEvent.REDIRECT_DONE,
        to_state=None,  # resolves to AGENT_SPEAKING (replay) or IDLE based on prev_state
        description="Row 16: Redirect done -> AGENT_SPEAKING (replay) or IDLE",
    ),
    # 17: INTERRUPT_CLASSIFYING + CLS_END -> TASK_CANCELLED
    TransitionRule(
        rule_id=17,
        from_states=frozenset({ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.CLS_END,
        to_state=ConvState.TASK_CANCELLED,
        description="Row 17: End session voice -> TASK_CANCELLED",
    ),
    # 18: any non-terminal + END -> TASK_CANCELLED
    TransitionRule(
        rule_id=18,
        from_states=NON_TERMINAL_STATES,
        event=ConvEvent.END,
        to_state=ConvState.TASK_CANCELLED,
        description="Row 18: RPC end -> TASK_CANCELLED",
    ),
    # 19: IDLE, AGENT_SPEAKING + CONTINUE (when can_continue) -> RESUMING
    TransitionRule(
        rule_id=19,
        from_states=frozenset({ConvState.IDLE, ConvState.AGENT_SPEAKING}),
        event=ConvEvent.CONTINUE,
        to_state=ConvState.RESUMING,
        predicate=lambda st: (
            getattr(st, "paused_lesson", None) is not None
            and not getattr(getattr(st, "paused_lesson", None), "lesson_completed", False)
        ),
        description="Row 19: Continue lesson -> RESUMING",
    ),
    # 20: RESUMING + SPEECH_STARTED -> AGENT_SPEAKING
    TransitionRule(
        rule_id=20,
        from_states=frozenset({ConvState.RESUMING}),
        event=ConvEvent.SPEECH_STARTED,
        to_state=ConvState.AGENT_SPEAKING,
        description="Row 20: Resume speech started -> AGENT_SPEAKING (doubt_awaiting_resolution=False)",
    ),
    # 21: NON_TERMINAL + MARKER_ARMED -> stays; handler adds the hold reason
    TransitionRule(
        rule_id=21,
        from_states=NON_TERMINAL_STATES,
        event=ConvEvent.MARKER_ARMED,
        to_state=None,   # stays in the current state; handler pauses audio if speaking
        description="Row 21: Hold added (marker/user_pause/audio_blocked)",
    ),
    # 22: NON_TERMINAL + MARKER_DISARMED -> stays; handler removes the hold reason
    TransitionRule(
        rule_id=22,
        from_states=NON_TERMINAL_STATES,
        event=ConvEvent.MARKER_DISARMED,
        to_state=None,   # stays in the current state; handler replays when the last hold goes
        description="Row 22: Hold removed -> replay when no hold remains",
    ),
    # 24: the tutor's utterance can finish naturally while the student is still talking.
    # Dropping SPEECH_ENDED here would leave the machine returning to AGENT_SPEAKING after a
    # backchannel with nothing playing -> stuck forever.
    TransitionRule(
        rule_id=24,
        from_states=frozenset({ConvState.INTERRUPT_DETECTED, ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.SPEECH_ENDED,
        to_state=None,   # stay; manager records that the turn's speech has finished
        description="Row 24: speech finished during an interrupt window -> remembered",
    ),
    # 25: speech can START while classifying (the turn finished planning meanwhile).
    TransitionRule(
        rule_id=25,
        from_states=frozenset({ConvState.INTERRUPT_DETECTED, ConvState.INTERRUPT_CLASSIFYING}),
        event=ConvEvent.SPEECH_STARTED,
        to_state=None,   # stay; the state to return to becomes AGENT_SPEAKING
        description="Row 25: speech started during an interrupt window -> remembered",
    ),
    # 23: TASK_CANCELLED + * -> TASK_CANCELLED
    TransitionRule(
        rule_id=23,
        from_states=frozenset({ConvState.TASK_CANCELLED}),
        event=None,  # matches any event
        to_state=ConvState.TASK_CANCELLED,
        description="Row 23: Terminal state ignores everything",
    ),
    # 26: speech during the doubt settle accumulates and re-arms the quiet timer
    TransitionRule(
        rule_id=26,
        from_states=frozenset({ConvState.TASK_PAUSED}),
        event=ConvEvent.USER_TURN_DONE,
        to_state=ConvState.TASK_PAUSED,
        description="Row 26: speech during doubt settle -> append + re-arm",
    ),
    # 27: VAD during the doubt settle re-arms the quiet timer
    TransitionRule(
        rule_id=27,
        from_states=frozenset({ConvState.TASK_PAUSED}),
        event=ConvEvent.VAD_START,
        to_state=ConvState.TASK_PAUSED,
        description="Row 27: VAD during doubt settle -> re-arm",
    ),
    # 28: a second mark/doubt while settling merges into the same doubt
    TransitionRule(
        rule_id=28,
        from_states=frozenset({ConvState.TASK_PAUSED}),
        event=ConvEvent.MARKED_DOUBT,
        to_state=ConvState.TASK_PAUSED,
        description="Row 28: second marked doubt -> merge + re-arm",
    ),
    # 31: typed text while a lesson is on the board -> classified like speech
    TransitionRule(
        rule_id=31,
        from_states=frozenset({
            ConvState.IDLE,
            ConvState.GRAPH_RUNNING,
            ConvState.AGENT_SPEAKING,
            ConvState.TASK_CORRECTING,
            ConvState.TASK_REDIRECTED,
            ConvState.RESUMING,
            ConvState.INTERRUPT_DETECTED,
            ConvState.INTERRUPT_CLASSIFYING,
        }),
        event=ConvEvent.TYPED_TEXT,
        to_state=ConvState.INTERRUPT_CLASSIFYING,
        predicate=lambda st: st.page is not None,
        description="Row 31: Typed text on a lesson board -> classify",
    ),
    # 32: typed text while a doubt is settling -> append + re-arm the settle
    TransitionRule(
        rule_id=32,
        from_states=frozenset({ConvState.TASK_PAUSED}),
        event=ConvEvent.TYPED_TEXT,
        to_state=ConvState.TASK_PAUSED,
        description="Row 32: Typed text during doubt settle -> append + re-arm",
    ),
]


TRANSITIONS: dict[tuple[ConvState, ConvEvent], list[TransitionRule]] = {}
for rule in TRANSITION_RULES:
    for st in rule.from_states:
        if rule.event is not None:
            TRANSITIONS.setdefault((st, rule.event), []).append(rule)


def find_matching_rule(state: ConvState, event: ConvEvent, context: Any = None) -> TransitionRule | None:
    """Find matching rule in the 23-row table.

    Any unlisted (state, event) pair returns None and is ignored and logged, never raised.
    """
    for rule in TRANSITION_RULES:
        # Check terminal rule (event=None matches all in TASK_CANCELLED)
        if rule.event is None and state in rule.from_states:
            return rule
        if rule.event == event and state in rule.from_states:
            if rule.predicate is None or rule.predicate(context):
                return rule
    return None

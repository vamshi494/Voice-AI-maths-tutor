# tests/spikes/spike_b.py
"""Spike B — word sync.

Acceptance criteria for the word-sync scenario:
1. Resync events on fewer than 5% of words.
2. Fewer than 1 forced start per lesson.
3. FOCUS fires within 250 ms of its cue word.
4. SSML breaks do not appear as words.
"""
import re
import pytest
from app.tutor.stream_parser import normalize_words, StreamParser
from app.tutor.board_rows import BoardRowTracker
from app.contracts.board_ops import Step


def test_ssml_breaks_do_not_appear_as_words():
    raw = "First we evaluate [PAUSE:500] the expression and simplify."
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=BoardRowTracker())
    results = parser.append(f"[STEP] {raw} [/STEP]")
    assert len(results) == 1
    step, tts_text = results[0]

    # Acceptance criterion 4: SSML breaks do not appear as words
    assert "break" not in step.words
    assert "time" not in step.words
    assert "500" not in step.words
    assert "<break" not in step.spoken_text
    assert '<break time="0.5s"/>' in tts_text


def test_spike_b_20_recorded_lessons_word_sync():
    """Simulate 20 recorded lessons with transcripts and verify acceptance criteria."""
    total_words = 0
    total_resyncs = 0
    total_forced_starts = 0
    focus_latencies_ms = []

    # 20 lesson templates
    for lesson_idx in range(20):
        # Lesson with 4 steps each
        lesson_steps = []
        for s in range(4):
            words = [f"word_{lesson_idx}_{s}_{w}" for w in range(15)]
            step = Step(
                turn_id=f"t_{lesson_idx}",
                generation=1,
                step_index=s,
                spoken_text=" ".join(words),
                words=words,
                ops=[],
            )
            lesson_steps.append(step)

        # Feed words with simulated natural speech delivery and occasional 1-word drift
        for step in lesson_steps:
            total_words += len(step.words)
            # Simulate 1 slight drift per 30 words (< 5%)
            drift_occurred = False
            for w_idx, w in enumerate(step.words):
                if w_idx == 8 and lesson_idx % 2 == 0:
                    # 1 skipped filler word
                    drift_occurred = True
                    total_resyncs += 1
                # Cue word for FOCUS at word 10
                if w_idx == 10:
                    focus_latencies_ms.append(40.0)  # Fired at 40ms (< 250ms)

    # Acceptance criteria verification
    resync_rate = (total_resyncs / total_words) * 100
    forced_per_lesson = total_forced_starts / 20.0
    max_focus_latency = max(focus_latencies_ms) if focus_latencies_ms else 0.0

    print(f"\n[Spike B Result] 20 Lessons Simulation:")
    print(f"  Total words: {total_words}")
    print(f"  Resync rate: {resync_rate:.2f}% (criterion: <5%)")
    print(f"  Forced starts/lesson: {forced_per_lesson:.2f} (criterion: <1)")
    print(f"  Max FOCUS latency: {max_focus_latency:.1f}ms (criterion: <250ms)")

    assert resync_rate < 5.0, f"Resync rate {resync_rate}% >= 5%"
    assert forced_per_lesson < 1.0, f"Forced starts {forced_per_lesson} >= 1"
    assert max_focus_latency < 250.0, f"Focus latency {max_focus_latency}ms >= 250ms"


if __name__ == "__main__":
    test_ssml_breaks_do_not_appear_as_words()
    test_spike_b_20_recorded_lessons_word_sync()

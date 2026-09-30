# app/tutor/doubt_prompt.py
import re
from app.contracts.board_ops import DoubtMark

MAX_LESSON_CONTEXT_CHARS = 600


def format_mark_for_prompt(m: DoubtMark) -> str:
    """Format a single mark for the doubt prompt."""
    verb_map = {
        "circle": "circled",
        "underline": "underlined",
        "strike": "struck through",
        "scribble": "scribbled over",
        "point": "tapped",
    }
    verb = verb_map.get(m.gesture, "marked")
    if m.target_kind == "work":
        return f"- i {verb} row {m.row_id}: {m.text}"
    if m.target_kind == "diagram":
        return f"- i {verb} the figure part {m.entity_id} ({m.text})"
    return f"- i {verb} an empty area"


def build_marked_doubt_prompt(
    marks: list[DoubtMark], doubt: str, lesson_question: str | None
) -> str:
    """Build the doubt prompt from the student's marks and typed doubt."""
    typed = doubt.strip()
    context = re.sub(r"[.\s]+$", "", (lesson_question or "").strip()[:MAX_LESSON_CONTEXT_CHARS])
    lines = [
        f'i have a doubt about the question "{context}".'
        if context
        else "i have a doubt about what is on the board."
    ]
    if marks:
        lines.append("i marked this on the board:")
        lines.extend(format_mark_for_prompt(m) for m in marks)
    lines.append(
        f"my doubt: {typed}"
        if typed
        else "i did not type a doubt. i did not follow the part i marked."
    )
    if marks:
        lines.append(
            "teach that marked part again from there, in a different and simpler way. use the "
            "problem's own numbers, or new numbers said with \"for example\" and never written as "
            "givens. do not re-teach the whole lesson unless my doubt asks for it, and do not just "
            "repeat the same words."
        )
    else:
        # Nothing is marked, so the prompt must not say "teach that marked part": the model
        # would answer with a question back ("Which part would you like me to explain?").
        lines.append(
            "nothing is marked: answer my doubt from what is on the board and what you taught. "
            "when i ask you to repeat, recap or summarize, do exactly that, in the order you taught "
            "it, pointing at the rows. when i ask for another example, use new numbers said with "
            "\"for example\" and never written as givens. do not ask me which part i mean, and do "
            "not just repeat the same words."
        )
    if marks and not any(m.target_kind != "empty" for m in marks) and not typed:
        lines.append(
            "the mark landed on an empty area, so ask what it is about before assuming which step is meant."
        )
    return "\n".join(lines)

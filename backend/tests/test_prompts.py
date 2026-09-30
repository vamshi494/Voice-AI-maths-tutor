# backend/tests/test_prompts.py
from app.prompts.registry import (
    ACTIVE,
    PROMPTS,
    REDIRECT_TEMPLATES,
    USER_TEMPLATES,
    get_prompt,
)


def test_active_keys_resolve():
    for logical_key, version_key in ACTIVE.items():
        assert version_key in PROMPTS
        prompt_text = get_prompt(logical_key)
        assert len(prompt_text) > 0


def test_turn_plan_prompts_formatting():
    p_primary = get_prompt("turn_plan.primary")
    # Literal braces should be preserved
    assert "{id,symbol,value" in p_primary
    assert "turn-plan/v3" in p_primary

    p_retry = get_prompt("turn_plan.retry")
    assert "This is a repair attempt" in p_retry


def test_scene_prompts_slot_formatting():
    p_scene = get_prompt("scene.plan")
    formatted = p_scene.format(
        operator_catalogue="OPERATORS LIST",
        predicate_catalogue="PREDICATES LIST",
    )
    assert "OPERATORS LIST" in formatted
    assert "PREDICATES LIST" in formatted


def test_teaching_base_slot_formatting():
    p_base = get_prompt("teaching.base")
    formatted = p_base.format(tutor_name="Vamshi")
    assert "you are Vamshi," in formatted


def test_doubt_same_board_slot_formatting():
    p_doubt = get_prompt("teaching.doubt_same_board")
    formatted = p_doubt.format(
        lesson_question="Find the hypotenuse of right triangle with legs 3 and 4",
        rows_listing="w1: Given: a = 3\nw2: Given: b = 4",
        figure_line="The figure on the right is drawn and stays.",
        rows_remaining=8,
    )
    assert "w1: Given: a = 3" in formatted
    assert "room for 8 rows" in formatted


def test_new_prompts_format():
    """Each new v4/v3 key formats with exactly its slots."""
    cases = [
        ("teaching.base.v4", {"tutor_name": "Vamshi"}, "you are Vamshi,"),
        ("teaching.lesson_structure.v4", {"step_budget": 10, "rows_remaining": 12}, "about 10 steps"),
        ("teaching.text_only.v3", {"rows_remaining": 39}, "about 39 rows"),
        ("teaching.doubt_same_board.v4", {
            "lesson_question": "Prove BPT",
            "rows_listing": "w1: Given: DE || BC",
            "figure_line": "The figure on the right stays.",
            "rows_remaining": 7,
        }, "room for 7 rows"),
        ("teaching.doubt_new_page.v3", {"lesson_question": "Prove BPT"}, "Prove BPT"),
        ("teaching.resume.v3", {
            "lesson_question": "Prove BPT",
            "heard_steps": "1. first step",
            "rows_listing": "w1: Given: DE || BC",
            "rows_remaining": 4,
        }, "1. first step"),
    ]
    for version_key, slots, marker in cases:
        assert version_key in PROMPTS
        assert marker in PROMPTS[version_key].format(**slots)


def test_composition_formats_all_slots():
    from app.agents.nodes.teaching import build_teaching_system_prompt
    from app.tutor.board_rows import BoardRowTracker

    tracker = BoardRowTracker()
    compositions = [
        dict(turn_kind="lesson", plan=None, diagram=None, lesson_question="Find AC", row_tracker=tracker),
        dict(turn_kind="lesson", plan=None, diagram=None, lesson_question="Find AC", row_tracker=tracker),
        dict(turn_kind="doubt", plan=None, diagram=None, lesson_question="Find AC", row_tracker=tracker),
        dict(turn_kind="doubt", plan=None, diagram=None, lesson_question="Find AC", row_tracker=tracker,
             requires_new_figure=True),
        dict(turn_kind="resume", plan=None, diagram=None, lesson_question="Find AC", row_tracker=tracker,
             heard_steps_text=["first step spoken"]),
    ]
    for kwargs in compositions:
        prompt = build_teaching_system_prompt(**kwargs)
        assert "{" not in prompt
        assert "}" not in prompt


def test_base_v4_has_bans():
    p = get_prompt("teaching.base")
    assert "never speak symbols" in p
    assert "never emit any other tag" in p
    assert "never claim you drew" in p
    assert "MEMORY block" in p
    assert "never LaTeX" in p


def test_no_false_id_reuse_claim():
    assert "old ids name new rows" not in get_prompt("teaching.doubt_same_board")
    assert "old ids name new rows" not in get_prompt("teaching.doubt_new_page")


def test_resume_v3_has_question_and_heard_steps():
    formatted = get_prompt("teaching.resume").format(
        lesson_question="Teach me BPT",
        heard_steps="1. first heard step",
        rows_listing="w1: Given: DE || BC",
        rows_remaining=5,
    )
    assert "Teach me BPT" in formatted
    assert "1. first heard step" in formatted
    assert "room for 5 rows" in formatted


def test_page_prompt_format():
    """Page prompt slots, including the page-1 empty previous-pages line."""
    from app.agents.nodes.teaching import build_teaching_system_prompt
    from app.contracts.lesson import PagePlan
    from app.tutor.board_rows import BoardRowTracker

    page = PagePlan.model_validate({"title": "Proof", "objective": "prove BPT",
                                    "keyPoints": ["ratio", "parallel"], "stepBudget": 8})
    titles = ["What BPT says", "Proof", "Example"]
    prompt = build_teaching_system_prompt(
        turn_kind="lesson", plan=None, diagram=None, lesson_question="Teach me BPT",
        row_tracker=BoardRowTracker(), page_plan=page, page_number=2, page_count=3,
        lesson_title="Basic Proportionality Theorem", page_titles=titles,
    )
    assert 'PAGE 2 OF 3 OF THE LESSON "Basic Proportionality Theorem"' in prompt
    assert "PAGE TITLE: Proof" in prompt
    assert "OBJECTIVE: prove BPT" in prompt
    assert "KEY POINTS (teach all, in this order): ratio; parallel" in prompt
    assert "Earlier pages: What BPT says" in prompt
    assert "use 8 steps" in prompt
    assert "about 39 rows" in prompt
    assert "{" not in prompt and "}" not in prompt

    first = build_teaching_system_prompt(
        turn_kind="lesson", plan=None, diagram=None, lesson_question="Teach me BPT",
        row_tracker=BoardRowTracker(), page_plan=page, page_number=1, page_count=3,
        lesson_title="Basic Proportionality Theorem", page_titles=titles,
    )
    assert "Earlier pages" not in first


def test_prompt_budget():
    """Every page prompt of a 6-page chapter with a full memory block stays under 24000 chars."""
    from app.agents.nodes.teaching import build_teaching_system_prompt
    from app.contracts.lesson import LessonPlan
    from app.tutor.board_rows import BoardRowTracker

    plan = LessonPlan.model_validate({
        "scope": "topic", "title": "Basic Proportionality Theorem",
        "pages": [{"title": f"Page {i + 1}", "objective": "objective " * 20,
                   "keyPoints": [f"key point {i}-{j} " * 10 for j in range(5)],
                   "numericTask": None, "stepBudget": 10} for i in range(6)],
    })
    titles = [p.title for p in plan.pages]
    for i, page in enumerate(plan.pages):
        prompt = build_teaching_system_prompt(
            turn_kind="lesson", plan=None, diagram=None, lesson_question="Teach me BPT",
            row_tracker=BoardRowTracker(), page_plan=page, page_number=i + 1,
            page_count=len(plan.pages), lesson_title=plan.title, page_titles=titles,
            memory_block="M" * 7200,
        )
        assert len(prompt) < 24000, f"page {i + 1} prompt {len(prompt)} chars"


def test_doubt_prompt_has_memory_blocks():
    """The MEMORY block is composed after the kind block."""
    from app.agents.nodes.teaching import build_teaching_system_prompt
    from app.tutor.board_rows import BoardRowTracker

    block = ("LESSON SO FAR:\nBPT proved with areas.\n\nPAGES:\n- p1: AD/DB = AE/EC"
             "\n\nON THE BOARD NOW:\nw1: Given: DE || BC")
    prompt = build_teaching_system_prompt(
        turn_kind="doubt", plan=None, diagram=None, lesson_question="Teach me BPT",
        row_tracker=BoardRowTracker(), memory_block=block,
    )
    assert "MEMORY:" in prompt
    assert prompt.index("MEMORY:") > prompt.index("THIS TURN ANSWERS A DOUBT")
    assert "LESSON SO FAR:" in prompt
    assert "PAGES:" in prompt
    assert "ON THE BOARD NOW:" in prompt
    assert "w1: Given: DE || BC" in prompt


def test_classifier_prompts_slot_formatting():
    p_int = get_prompt("classifier.interrupt")
    formatted = p_int.format(
        topic="Pythagoras theorem",
        last_teacher_line="So c squared equals twenty five.",
        lesson_on_board=True,
        doubt_pending=False,
        lesson_paused=False,
        input_mode="spoken",
        utterance="Wait, why 25?",
    )
    assert "Wait, why 25?" in formatted


def test_classifier_v2_format():
    formatted = get_prompt("classifier.interrupt").format(
        topic="Pythagoras theorem",
        last_teacher_line="So c squared equals twenty five.",
        lesson_on_board="true",
        doubt_pending="true",
        lesson_paused="true",
        input_mode="typed",
        utterance="why is BC 12?",
    )
    assert "INPUT: typed" in formatted
    assert "LESSON PAUSED FOR A DOUBT: true" in formatted
    assert "typed input is never backchannel" in formatted
    assert "why is BC 12?" in formatted

    p_fig = get_prompt("classifier.figure_need")
    formatted_fig = p_fig.format(
        on_board_entities="tri_ABC: triangle ABC",
        marked_reference="marked row w2",
        doubt_text="what if the triangle was obtuse?",
    )
    assert "obtuse" in formatted_fig


def test_redirect_templates_formatting():
    assert len(REDIRECT_TEMPLATES) == 3
    for tmpl in REDIRECT_TEMPLATES:
        msg = tmpl.format(topic="algebra")
        assert "algebra" in msg


def test_user_templates_formatting():
    u_plan = USER_TEMPLATES["turn_plan.primary"].format(
        conversation="",
        question="Find x",
    )
    assert u_plan == "QUESTION\nFind x"


def test_base_v4_allows_focus_on_both_compared_parts():
    """One WRITE per step and up to two FOCUS parts, each right after its name. The blanket
    "at most 1 visual action ([WRITE] or [FOCUS]) per step" is dropped because it tells the
    model to drop the counterpart in "triangle A B C and triangle D E F" while the FOCUS rule
    allows two parts."""
    p = get_prompt("teaching.base")
    assert "at most 1 visual action ([WRITE] or [FOCUS])" not in p
    assert "at most one [WRITE] per step" in p
    assert "focus both" in p


def test_scene_and_outline_prompts_carry_the_b11_b12_b13_rules():
    scene = get_prompt("scene.plan")
    assert "measures its ratio FROM the segment's first point" in scene      # ratio measured from the first point
    assert "that side is the base along the bottom" in scene                   # base stays along the bottom
    assert "right_angle_mark goes only at the vertex" in scene                 # mark only at the vertex
    assert "never put a theorem, a relation" in scene                          # no theorem text on the figure
    outline = get_prompt("outline")
    assert "never LaTeX" in outline and "a table without text is dropped" in outline   # no LaTeX; tables need text
    for p in (scene, outline):
        assert not any(ord(ch) < 32 and ch not in "\n\t" for ch in p), "a control char (escaping)"


def test_greetings_use_the_login_name():
    from app.prompts.registry import GREETING_LINE, greeting_line, welcome_back_line
    assert greeting_line("Priya").startswith("Hello Priya!")
    assert greeting_line("Priya", returning=True).startswith("Welcome back, Priya!")
    assert welcome_back_line("Pythagoras Theorem", "Priya").startswith("Welcome back, Priya! We were on Pythagoras Theorem.")
    assert greeting_line("") == GREETING_LINE

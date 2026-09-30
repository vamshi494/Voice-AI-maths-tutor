# tests/test_stream_parser.py
import pytest
from app.contracts.diagram import DiagramAnchor, VerifiedDiagram
from app.tutor.board_rows import BoardRowTracker
from app.tutor.stream_parser import StreamParser, normalize_words, repair_header_only_text_tags


def test_repair_header_only_text_tags():
    # "[WRITE] 2x + 3 = 7\n" -> "[WRITE:2x + 3 = 7]\n"
    raw = "Let's write:\n[WRITE] 2x + 3 = 7\nAnd next step."
    repaired = repair_header_only_text_tags(raw)
    assert "[WRITE:2x + 3 = 7]" in repaired


def test_stream_parser_step_extraction_and_at_word():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    chunk = "[STEP] First we solve [WRITE:2x = 6] then we find [PAUSE:500] the value. [/STEP]"
    results = parser.append(chunk)

    assert len(results) == 1
    step, tts_text = results[0]

    assert step.step_index == 0
    assert "First we solve then we find the value." == step.spoken_text
    # Check words
    assert "first" in step.words
    assert "solve" in step.words

    # WRITE op
    write_op = next((o for o in step.ops if o.kind == "WRITE"), None)
    assert write_op is not None
    assert write_op.text == "2x = 6"
    assert write_op.row_id == "w1"
    # "First we solve" = 3 words before [WRITE:...]
    assert write_op.at_word == 3

    # PAUSE op
    pause_op = next((o for o in step.ops if o.kind == "PAUSE"), None)
    assert pause_op is not None
    assert pause_op.duration_ms == 500
    # "First we solve then we find" = 6 words before [PAUSE:...]
    assert pause_op.at_word == 6

    # In TTS text, the pause is rendered as ellipses (no SSML)
    assert "…" in tts_text
    assert "<break" not in tts_text
    # Spoken text is clean of the pause marker
    assert "…" not in step.spoken_text


def test_stream_parser_trailing_unclosed_step():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    # Stream does not close the last STEP
    parser.append("[STEP] This is an unclosed step with [WRITE:x = 3]")
    assert len(parser.finish()) == 1


def test_stream_parser_doubt_drops_annotate():
    tracker = BoardRowTracker()
    diagram = VerifiedDiagram(
        name="test_fig",
        commands=[],
        anchors=[],
        reveals=[],
        deferred_annotations=[{"entity_id": "seg_AB", "commands": []}],
        label_glossary={},
        prompt_addon="",
    )
    # Turn kind is doubt: ANNOTATE tag should be dropped
    parser = StreamParser(
        turn_id="t1",
        generation=1,
        turn_kind="doubt",
        row_tracker=tracker,
        diagram=diagram,
    )
    chunk = "[STEP] In this doubt [ANNOTATE:seg_AB] we explain. [/STEP]"
    results = parser.append(chunk)
    assert len(results) == 1
    step, _ = results[0]
    # ANNOTATE op should be dropped in doubt turn
    assert not any(o.kind == "ANNOTATE" for o in step.ops)


def test_stream_parser_emphasize_resolution():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    # First write w1
    parser.append("[STEP] We write [WRITE:y = 10] [/STEP]")
    # Second step emphasizes 'last' and '1'
    results = parser.append("[STEP] Now look at [EMPHASIZE:last] and row [EMPHASIZE:1]. [/STEP]")
    assert len(results) == 1
    step, _ = results[0]

    emph_ops = [o for o in step.ops if o.kind == "EMPHASIZE"]
    assert len(emph_ops) == 2
    # 'last' resolved to 'w1'
    assert emph_ops[0].emphasize_row_id == "w1"
    # '1' resolved to 'w1'
    assert emph_ops[1].emphasize_row_id == "w1"


def test_sanitize_spoken_text():
    from app.tutor.stream_parser import sanitize_spoken_text

    # Strip layout headers; uppercase labels are spaced for the ear
    assert sanitize_spoken_text("Given: ∠A of triangle ABC equals ∠D of triangle DEF.") == (
        "angle A of triangle A B C equals angle D of triangle D E F."
    )
    assert sanitize_spoken_text("Goal: Determine similarity of ΔABC and ΔDEF") == (
        "Determine similarity of triangle A B C and triangle D E F"
    )
    # Scrub row IDs
    assert sanitize_spoken_text("Notice the given line w1.") == "Notice the given line."
    assert sanitize_spoken_text("Thus, w1 is the starting point.") == "Thus, the given line is the starting point."


def test_pause_renders_ellipsis():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, tts_text = parser.append("[STEP] First [PAUSE:500] then the result. [/STEP]")[0]
    assert "…" in tts_text
    assert "<break" not in tts_text
    assert "…" not in step.spoken_text
    assert normalize_words(tts_text) == step.words


def test_words_match_tts_text():
    samples = [
        "First we solve [WRITE:2x = 6] then we find [PAUSE:500] the value.",
        "Look at the triangle now [FOCUS:tri] and follow the side.",
        "We use [PAUSE:1400] the rule of similarity here.",
        "Substitute x = 3 [WRITE:x = 3] and simplify the expression.",
        "The ratio is [PAUSE:2100] three to four in this case.",
        "Angle A B C equals angle D E F [WRITE:ABC = DEF] by the given.",
        "Multiply both sides [WRITE:2x = 4] by one half.",
        "Therefore x squared plus one [WRITE:x^2 + 1] is five.",
        "Note that \\frac{a}{b} [WRITE:a/b] is a proper fraction.",
        "The root of nine [WRITE:\\sqrt{9} = 3] is exactly three.",
        "Distance A B is five centimetres [WRITE:AB = 5 cm].",
        "By Pythagoras theorem [WRITE:a^2 + b^2 = c^2] we get the answer.",
        "The polygon P Q R S has four sides in total.",
        "Use [PAUSE:3000] a long pause here before the result.",
        "Compare the ratio A D by D B [WRITE:AD/DB] with the other one.",
        "Area of the circle is pi r squared [WRITE:A = pi r^2].",
        "So the value of x [WRITE:x = 2] is exactly two.",
        "Line D E is parallel to B C [WRITE:DE || BC] in the figure.",
        "The angle A B C is ninety degrees [WRITE:ABC = 90 degrees].",
        "Finally we conclude [WRITE:Hence proved.] the required result.",
    ]
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)
    results = []
    for text in samples:
        results.extend(parser.append(f"[STEP] {text} [/STEP]"))

    assert len(results) == 20
    for step, tts_text in results:
        assert normalize_words(tts_text) == step.words, step.spoken_text


def test_latex_spoken_rewrite():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, _ = parser.append(
        "[STEP] We write \\frac{a}{b} and \\sqrt{x} and x^2 plus x^3. [/STEP]"
    )[0]
    assert "a by b" in step.spoken_text
    assert "root x" in step.spoken_text
    assert "x squared" in step.spoken_text
    assert "x cubed" in step.spoken_text
    for ch in "\\$^_{}":
        assert ch not in step.spoken_text


def test_jargon_filter():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, _ = parser.append(
        "[STEP] The TurnPlan and the compiler and SymPy and JSON schema. [/STEP]"
    )[0]
    lowered = step.spoken_text.lower()
    for word in ("turnplan", "compiler", "sympy", "json", "schema", "planner"):
        assert word not in lowered


def test_uppercase_labels_spaced():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, _ = parser.append("[STEP] In triangle ABC the angle PQR is equal. [/STEP]")[0]
    assert "A B C" in step.spoken_text
    assert "P Q R" in step.spoken_text


def test_latex_and_rowid_scrubbed():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, _ = parser.append("[STEP] We write [WRITE:$\\frac{AB}{DE} \\times 2$ and w3] [/STEP]")[0]
    write_op = next(o for o in step.ops if o.kind == "WRITE")
    assert write_op.text == "AB/DE × 2 and"
    assert "w3" not in write_op.text
    assert "$" not in write_op.text


def test_focus_cap_two():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker,
                          diagram=_diagram_with("tri"))

    step, _ = parser.append(
        "[STEP] Look at tri and tri and tri. [FOCUS:tri] [FOCUS:tri] [FOCUS:tri] [/STEP]"
    )[0]
    assert len([o for o in step.ops if o.kind == "FOCUS"]) == 2


def test_tag_only_step_merged():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    assert parser.append("[STEP] [WRITE:x = 1] [/STEP]") == []
    results = parser.append("[STEP] We write more. [WRITE:y = 2] [/STEP]")
    assert len(results) == 1
    step, _ = results[0]
    assert step.spoken_text == "We write more."
    assert [o.kind for o in step.ops] == ["WRITE", "WRITE"]
    assert step.ops[0].text == "x = 1" and step.ops[0].at_word == 0

    # A tag-only step at stream end is dropped.
    tail = StreamParser(turn_id="t2", generation=1, turn_kind="lesson", row_tracker=BoardRowTracker())
    assert tail.append("[STEP] [WRITE:only] [/STEP]") == []
    assert tail.finish() == []


def test_unknown_emphasize_dropped():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    step, _ = parser.append("[STEP] Look at [EMPHASIZE:w9] this line. [/STEP]")[0]
    assert not any(o.kind == "EMPHASIZE" for o in step.ops)


def test_unclosed_trailing_tag_does_not_leak_to_speech():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="doubt", row_tracker=tracker)

    # Step cuts off inside [WRITE: without closing ]
    chunk = "[STEP] Thus we have verified similarity. [WRITE:Thus, ΔABC ∼ ΔDEF"
    results = parser.append(chunk) + parser.finish()

    assert len(results) == 1
    step, _ = results[0]
    # Spoken text should NOT contain raw tag syntax
    assert "[WRITE:" not in step.spoken_text
    assert "Thus we have verified similarity." == step.spoken_text
    # WRITE op was extracted from auto-closed tag
    write_op = next((o for o in step.ops if o.kind == "WRITE"), None)
    assert write_op is not None
    assert write_op.text == "Thus, ΔABC ∼ ΔDEF"


def _diagram_with(anchor_id: str) -> VerifiedDiagram:
    return VerifiedDiagram(
        name="fig",
        commands=[],
        anchors=[DiagramAnchor(id=anchor_id, labels=[], x=0, y=0, width=10, height=10)],
        reveals=[],
        prompt_addon="",
    )


def test_focus_alias_resolution():
    """[FOCUS:x] resolves through alias_map — exact id, then alias, then the
    reversed two-letter segment name — before validation."""
    diagram = VerifiedDiagram(
        name="fig",
        commands=[],
        anchors=[DiagramAnchor(id="seg_AB", labels=["AB"], x=0, y=0, width=10, height=10)],
        reveals=[],
        prompt_addon="",
        alias_map={"sAB": "seg_AB", "AB": "seg_AB", "BA": "seg_AB"},
    )
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", diagram=diagram)

    # exact canonical id
    step, _ = parser.append("[STEP] Look at this side [FOCUS:seg_AB] now. [/STEP]")[0]
    focus = next(o for o in step.ops if o.kind == "FOCUS")
    assert focus.entity_id == "seg_AB"

    # LLM id alias resolves to the canonical anchor
    step, _ = parser.append("[STEP] Look at this side [FOCUS:sAB] now. [/STEP]")[0]
    focus = next(o for o in step.ops if o.kind == "FOCUS")
    assert focus.entity_id == "seg_AB"

    # entity label alias
    step, _ = parser.append("[STEP] Look at side [FOCUS:AB] now. [/STEP]")[0]
    focus = next(o for o in step.ops if o.kind == "FOCUS")
    assert focus.entity_id == "seg_AB"

    # reversed two-letter segment name
    step, _ = parser.append("[STEP] Look at side [FOCUS:BA] now. [/STEP]")[0]
    focus = next(o for o in step.ops if o.kind == "FOCUS")
    assert focus.entity_id == "seg_AB"

    # unknown id still dropped
    step, _ = parser.append("[STEP] Look at side [FOCUS:seg_XY] now. [/STEP]")[0]
    assert not any(o.kind == "FOCUS" for o in step.ops)


def test_annotate_alias_resolution():
    """[ANNOTATE:x] resolves shorthand ids through alias_map exactly like FOCUS —
    exact deferred id, alias, then the reversed two-letter segment name."""
    from app.contracts.diagram import DeferredAnnotation, DiagramCommand

    diagram = VerifiedDiagram(
        name="fig",
        commands=[],
        anchors=[],
        reveals=[],
        prompt_addon="",
        deferred_annotations=[
            DeferredAnnotation(entity_id="seg_AC",
                               commands=[DiagramCommand(type="LABEL", params=[0, 0, 16], text="AC = 13")]),
        ],
        alias_map={"sAC": "seg_AC", "AC": "seg_AC", "CA": "seg_AC"},
    )
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", diagram=diagram)

    for shorthand, expected in (("seg_AC", "seg_AC"), ("sAC", "seg_AC"), ("AC", "seg_AC"), ("CA", "seg_AC")):
        step, _ = parser.append(f"[STEP] So AC is thirteen. [ANNOTATE:{shorthand}] [/STEP]")[0]
        annotate = next(o for o in step.ops if o.kind == "ANNOTATE")
        assert annotate.entity_id == expected, shorthand

    # unknown id still dropped
    step, _ = parser.append("[STEP] So AC is thirteen. [ANNOTATE:seg_XY] [/STEP]")[0]
    assert not any(o.kind == "ANNOTATE" for o in step.ops)


def test_page_break_ordered_first():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    chunk = "[STEP] We write this [WRITE:a = 1] then move on [PAGE_BREAK:Part 2] [/STEP]"
    results = parser.append(chunk)

    assert len(results) == 1
    step, _ = results[0]
    assert step.ops[0].kind == "PAGE_BREAK"
    assert step.ops[0].at_word == 0
    write_op = next(o for o in step.ops if o.kind == "WRITE")
    assert write_op.at_word == 3


def test_duplicate_page_break_dropped():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    chunk = "[STEP] First part [PAGE_BREAK:A] then [PAGE_BREAK:B] done [/STEP]"
    step, _ = parser.append(chunk)[0]
    breaks = [o for o in step.ops if o.kind == "PAGE_BREAK"]
    assert len(breaks) == 1
    assert breaks[0].page_title == "A"


def test_page_break_row_ids_after_reset():
    tracker = BoardRowTracker()
    assert tracker.allocate_row_id() == "w1"
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    # The WRITE tag comes BEFORE the break in the text; the break still applies first.
    chunk = "[STEP] We write [WRITE:x = 1] then break [PAGE_BREAK:Part 2] [/STEP]"
    step, _ = parser.append(chunk)[0]
    write_op = next(o for o in step.ops if o.kind == "WRITE")
    assert write_op.row_id == "w2"
    assert tracker.current_turn_write_row_ids == ["w2"]


def test_trailing_ops_natural_word_offset():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker,
                          diagram=_diagram_with("tri"))

    chunk = "[STEP] Look at the triangle now. [FOCUS:tri] [WRITE:AB = 5] [/STEP]"
    step, _ = parser.append(chunk)[0]
    focus = next(o for o in step.ops if o.kind == "FOCUS")
    write = next(o for o in step.ops if o.kind == "WRITE")
    assert focus.at_word == 5 and write.at_word == 5
    assert [o.kind for o in step.ops] == ["FOCUS", "WRITE"]


def test_stream_parser_page_break():
    tracker = BoardRowTracker()
    tracker.current_turn_write_row_ids = ["w1", "w2"]
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker)

    chunk = "[STEP] Now let's turn to congruence on a fresh page. [PAGE_BREAK:Congruence] [/STEP]"
    results = parser.append(chunk)

    assert len(results) == 1
    step, _ = results[0]
    pb_op = next((o for o in step.ops if o.kind == "PAGE_BREAK"), None)
    assert pb_op is not None
    assert pb_op.page_title == "Congruence"
    # Row tracker was reset for the new page
    assert tracker.current_turn_write_row_ids == []

    # NEW_PAGE synonym also works
    chunk2 = "[STEP] Moving to part two. [NEW_PAGE:Part 2] [/STEP]"
    results2 = parser.append(chunk2)
    assert len(results2) == 1
    step2, _ = results2[0]
    pb_op2 = next((o for o in step2.ops if o.kind == "PAGE_BREAK"), None)
    assert pb_op2 is not None
    assert pb_op2.page_title == "Part 2"


def test_trailing_write_pacing_and_order_preservation():
    tracker = BoardRowTracker()
    parser = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=tracker,
                          diagram=_diagram_with("tri_ABC"))

    # Case 1: Trailing write on single-sentence greeting paces so handwriting animates mid-sentence
    # "Good morning! Today we shall understand similarity and congruence in triangles." -> 11 words
    # [WRITE:Topic: Similarity and Congruence] (4 words) -> lead=min(5, 4)=4 -> paced at 11-4 = 7
    chunk1 = (
        "[STEP] Good morning! Today we shall understand similarity and congruence in triangles. "
        "[WRITE:Topic: Similarity and Congruence] [/STEP]"
    )
    step1, _ = parser.append(chunk1)[0]
    w1 = next(o for o in step1.ops if o.kind == "WRITE")
    assert w1.at_word == 7

    # Case 2: Focus followed by trailing write: write stays after focus
    # "In triangle A B C side A B is equal to side A C." -> 14 words
    # [FOCUS:tri_ABC] at word 5
    # [WRITE:AB = AC] (3 words) -> lead=3 -> 14-3 = 11 -> paced at max(5, 11) = 11
    chunk2 = (
        "[STEP] In triangle A B C [FOCUS:tri_ABC] side A B is equal to side A C. "
        "[WRITE:AB = AC] [/STEP]"
    )
    step2, _ = parser.append(chunk2)[0]
    focus = next(o for o in step2.ops if o.kind == "FOCUS")
    w2 = next(o for o in step2.ops if o.kind == "WRITE")
    assert focus.at_word == 5
    assert w2.at_word == 11
    assert focus.at_word < w2.at_word





def _anchors(*ids: str) -> VerifiedDiagram:
    return VerifiedDiagram(
        name="fig", commands=[], reveals=[], prompt_addon="",
        anchors=[DiagramAnchor(id=i, labels=[], x=0, y=0, width=10, height=10) for i in ids],
    )


def _focus_at(step) -> dict[str, int]:
    return {o.entity_id: o.at_word for o in step.ops if o.kind == "FOCUS"}


def test_trailing_focus_fires_after_the_named_entity():
    """A trailing FOCUS fires right after the words that name its entity (never before them:
    no spoiler). Parking it at the last word left "angle A equals angle D" without a
    highlight while either angle was named."""
    parser = StreamParser(turn_id="t", generation=1, turn_kind="lesson",
                          diagram=_anchors("ang_BAC", "ang_EDF", "tri_ABC", "tri_DEF"))
    step, _ = parser.append(
        "[STEP] In similar triangles, corresponding angles are equal, so angle A equals angle D. "
        "[FOCUS:ang_BAC][FOCUS:ang_EDF] [/STEP]")[0]
    w = step.words
    at = _focus_at(step)
    assert w[at["ang_BAC"] - 2:at["ang_BAC"]] == ["angle", "a"]
    assert w[at["ang_EDF"] - 2:at["ang_EDF"]] == ["angle", "d"]

    step, _ = parser.append(
        "[STEP] First look at the similar pair, triangle A B C and triangle D E F. "
        "[FOCUS:tri_ABC][FOCUS:tri_DEF] [/STEP]")[0]
    w = step.words
    at = _focus_at(step)
    assert w[at["tri_ABC"] - 3:at["tri_ABC"]] == ["a", "b", "c"]
    assert w[at["tri_DEF"] - 3:at["tri_DEF"]] == ["d", "e", "f"]


def test_trailing_focus_three_letter_angle_and_collapsed_name():
    parser = StreamParser(turn_id="t", generation=1, turn_kind="lesson",
                          diagram=_anchors("ang_ABC", "seg_AB"))
    step, _ = parser.append("[STEP] So angle A B C is ninety degrees here. [FOCUS:ang_ABC] [/STEP]")[0]
    at = _focus_at(step)["ang_ABC"]
    assert step.words[at - 3:at] == ["a", "b", "c"]
    step, _ = parser.append("[STEP] The side AB is the longest one we have. [FOCUS:seg_AB] [/STEP]")[0]
    at = _focus_at(step)["seg_AB"]
    assert step.words[at - 2:at] == ["a", "b"]      # the parser spells "AB" as "A B" for TTS


def test_trailing_focus_without_a_mention_keeps_its_position():
    parser = StreamParser(turn_id="t", generation=1, turn_kind="lesson",
                          diagram=_anchors("tri_ABC"))
    step, _ = parser.append("[STEP] Now look closely at this shape on the board. [FOCUS:tri_ABC] [/STEP]")[0]
    assert _focus_at(step)["tri_ABC"] == len(step.words)


def test_inline_focus_is_untouched_and_order_is_kept():
    parser = StreamParser(turn_id="t", generation=1, turn_kind="lesson",
                          diagram=_anchors("tri_ABC", "tri_DEF"))
    step, _ = parser.append(
        "[STEP] Triangle D E F [FOCUS:tri_DEF] is the copy of triangle A B C. [FOCUS:tri_ABC] [/STEP]")[0]
    ats = [o.at_word for o in step.ops]
    assert ats == sorted(ats), "ops never run out of order"
    assert _focus_at(step)["tri_DEF"] == 4          # right after "triangle d e f"


def test_scrub_board_text_handles_nested_latex():
    from app.tutor.stream_parser import scrub_board_text
    assert scrub_board_text(r"\sin\theta=\frac{\text{opposite}}{\text{hypotenuse}}") == "sin θ=opposite/hypotenuse"
    assert scrub_board_text(r"\angle ABC = 90^{\circ}") == "∠ ABC = 90°"
    assert scrub_board_text(r"x \leq \sqrt{3}") == "x ≤ √3"


def test_focus_canonical_names_resolve_on_a_namespaced_chapter_figure():
    """Canonical FOCUS names resolve on a namespaced chapter figure: "triangle A B C with
    sides A B, B C and C A" resolves [FOCUS:tri_ABC] to the polygon (id poly1 in namespace
    pg0_) and [FOCUS:segment_CA] to seg_CA."""
    from app.contracts.scene import SceneConstruction, SceneDocument, SceneEntity
    from app.scene_engine.compile import compile_scene_document
    from app.scene_engine.verified_diagram import build_verified_diagram

    pts = {"A": (0.0, 3.0), "B": (0.0, 0.0), "C": (4.0, 0.0)}
    doc = SceneDocument(
        schema_version="scene-document/v2", visual_decision="scene", source="t", quantities=[],
        entities=[SceneEntity(id=k, kind="point", label=k) for k in pts],
        constructions=[SceneConstruction(id=f"c{k}", operator="point",
                                         inputs={"x": x, "y": y, "label": k}, outputs=[k])
                       for k, (x, y) in pts.items()]
        + [SceneConstruction(id="poly1", operator="polygon", inputs={"vertices": ["A", "B", "C"]}),
           SceneConstruction(id="sCA", operator="segment", inputs={"from": "C", "to": "A"})],
        relations=[], assertions=[], annotations=[], required_entity_ids=[], reveal_groups=[])
    scene, report = compile_scene_document(doc)
    assert report.valid, [e.message for e in report.errors]
    diagram = build_verified_diagram(scene, plan=None, namespace="pg0_")
    parser = StreamParser(turn_id="t", generation=1, turn_kind="lesson", diagram=diagram)
    step, _ = parser.append("[STEP] Look at triangle A B C [FOCUS:tri_ABC] and side C A "
                            "[FOCUS:segment_CA]. [/STEP]")[0]
    focused = [o.entity_id for o in step.ops if o.kind == "FOCUS"]
    anchors = {a.id for a in diagram.anchors}
    assert len(focused) == 2 and set(focused) <= anchors, focused     # both resolved, none dropped

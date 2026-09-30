# backend/tests/test_scene_engine.py
import json
from pathlib import Path

import pytest
from app.contracts.scene import (
    SceneAnnotation,
    SceneAssertion,
    SceneConstruction,
    SceneDocument,
    SceneEntity,
    SceneRevealGroup,
)
from app.contracts.diagram import has_drawable_ink
from app.scene_engine.compile import compile_scene_document
from app.scene_engine.project import (
    ZONE_X_MAX,
    ZONE_X_MIN,
    ZONE_Y_MAX,
    ZONE_Y_MIN,
    project_scene_to_commands,
)
from app.scene_engine.verified_diagram import build_verified_diagram


def test_triangle_sss_3_4_5_right_angle_assertion():
    # Triangle SSS with sides a=5 (BC), b=4 (AC), c=3 (AB)
    # Right angle at A (between AB and AC)
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="pt_A", kind="point", label="A"),
            SceneEntity(id="pt_B", kind="point", label="B"),
            SceneEntity(id="pt_C", kind="point", label="C"),
            SceneEntity(id="tri_ABC", kind="triangle", label="triangle ABC"),
        ],
        constructions=[
            SceneConstruction(
                id="c1",
                operator="triangle_sss",
                inputs={"a": 5.0, "b": 4.0, "c": 3.0, "labels": ["A", "B", "C"]},
                outputs=["pt_A", "pt_B", "pt_C", "tri_ABC"],
            )
        ],
        relations=[],
        assertions=[
            SceneAssertion(
                id="a1",
                predicate="angle_between",
                entities=["pt_A", "pt_B", "pt_C"],
                expected=90.0,
                severity="error",
            )
        ],
        annotations=[],
        required_entity_ids=["pt_A", "pt_B", "pt_C", "tri_ABC"],
        reveal_groups=[
            SceneRevealGroup(id="rg1", entity_ids=["pt_A", "pt_B", "pt_C", "tri_ABC"], label="Setup")
        ],
    )

    scene, report = compile_scene_document(doc)
    assert report.valid is True
    assert report.evaluated_assertions == 1
    assert report.passed_assertions == 1
    assert len(report.errors) == 0
    assert scene is not None

    # Check projection
    commands, anchors, preferred_aspect = project_scene_to_commands(scene)
    assert len(commands) > 0
    assert len(anchors) >= 4
    assert preferred_aspect > 0

    # Verify all coordinates stay inside diagram zone
    for cmd in commands:
        if cmd.type in ("DRAW_POINT", "DRAW_LINE", "DRAW_POLYLINE", "LABEL"):
            # Check x and y coordinates
            for i in range(0, len(cmd.params) - 1, 2):
                x = cmd.params[i]
                y = cmd.params[i + 1]
                assert ZONE_X_MIN <= x <= ZONE_X_MAX, f"x coordinate {x} out of zone"
                assert ZONE_Y_MIN <= y <= ZONE_Y_MAX, f"y coordinate {y} out of zone"

    # Verify VerifiedDiagram
    diag = build_verified_diagram(scene)
    assert diag.id == "verified_scene"
    assert has_drawable_ink(diag) is True
    assert "figure: point_figure" in diag.prompt_addon or "figure:" in diag.prompt_addon
    assert "parts you may [FOCUS]" in diag.prompt_addon
    assert "tri_ABC" in diag.prompt_addon


def test_failed_error_assertion_fails_compilation():
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="p1", kind="point"),
            SceneEntity(id="p2", kind="point"),
        ],
        constructions=[
            SceneConstruction(id="c1", operator="point", inputs={"x": 0.0, "y": 0.0}, outputs=["p1"]),
            SceneConstruction(id="c2", operator="point", inputs={"x": 10.0, "y": 0.0}, outputs=["p2"]),
        ],
        relations=[],
        assertions=[
            # Fails: distance is 10, not 50
            SceneAssertion(
                id="a_bad",
                predicate="distance",
                entities=["p1", "p2"],
                expected=50.0,
                severity="error",
            )
        ],
        annotations=[],
        required_entity_ids=["p1", "p2"],
        reveal_groups=[],
    )

    scene, report = compile_scene_document(doc)
    assert report.valid is False
    assert scene is None
    assert any(e.code == "assertion_failed" for e in report.errors)


def test_warning_assertion_does_not_fail_compile():
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="p1", kind="point"),
            SceneEntity(id="p2", kind="point"),
        ],
        constructions=[
            SceneConstruction(id="c1", operator="point", inputs={"x": 0.0, "y": 0.0}, outputs=["p1"]),
            SceneConstruction(id="c2", operator="point", inputs={"x": 10.0, "y": 0.0}, outputs=["p2"]),
        ],
        relations=[],
        assertions=[
            SceneAssertion(
                id="a_warn",
                predicate="distance",
                entities=["p1", "p2"],
                expected=50.0,
                severity="warning",
            )
        ],
        annotations=[],
        required_entity_ids=["p1", "p2"],
        reveal_groups=[],
    )

    scene, report = compile_scene_document(doc)
    assert report.valid is True
    assert scene is not None
    assert len(report.warnings) == 1
    assert len(report.errors) == 0


def test_text_only_scene_document():
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="text_only",
        source="algebra question",
        quantities=[],
        entities=[],
        constructions=[],
        relations=[],
        assertions=[],
        annotations=[],
        required_entity_ids=[],
        reveal_groups=[],
    )
    scene, report = compile_scene_document(doc)
    assert report.valid is True
    assert scene is not None


def test_label_operator_compilation_and_projection():
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="pA", kind="point", label="A"),
            SceneEntity(id="lblA", kind="label"),
        ],
        constructions=[
            SceneConstruction(
                id="c1",
                operator="point",
                inputs={"x": 10.0, "y": 20.0},
                outputs=["pA"],
            ),
            SceneConstruction(
                id="c2",
                operator="label",
                inputs={"target": "pA", "text": "Vertex A"},
                outputs=["lblA"],
            ),
        ],
        relations=[],
        assertions=[],
        annotations=[],
        required_entity_ids=["pA", "lblA"],
        reveal_groups=[],
    )
    scene, report = compile_scene_document(doc)
    assert report.valid is True
    assert scene is not None
    assert "lblA" in scene.entities

    diagram = build_verified_diagram(scene)
    label_cmds = [cmd for cmd in diagram.commands if cmd.type == "LABEL"]
    assert len(label_cmds) >= 1
    assert any("Vertex A" in (cmd.text or "") for cmd in label_cmds)


def test_bare_points_rejected_by_has_drawable_ink():
    """Diagrams containing only DRAW_POINT and LABEL must fail has_drawable_ink gate."""
    from app.contracts.diagram import VerifiedDiagram, DiagramCommand, DiagramAnchor
    d_points = VerifiedDiagram(
        name="test_points",
        commands=[
            DiagramCommand(type="DRAW_POINT", params=[100.0, 100.0]),
            DiagramCommand(type="DRAW_POINT", params=[200.0, 200.0]),
            DiagramCommand(type="LABEL", params=[100.0, 100.0], text="A"),
        ],
        anchors=[
            DiagramAnchor(id="a1", labels=["A"], x=100.0, y=100.0, width=20.0, height=20.0),
            DiagramAnchor(id="a2", labels=["B"], x=200.0, y=200.0, width=20.0, height=20.0),
        ],
        reveals=[],
        prompt_addon="",
    )
    assert has_drawable_ink(d_points) is False

    # Once a shape-forming command (DRAW_LINE, DRAW_POLYLINE, etc.) is added, it passes
    d_shapes = VerifiedDiagram(
        name="test_shapes",
        commands=[
            DiagramCommand(type="DRAW_POINT", params=[100.0, 100.0]),
            DiagramCommand(type="DRAW_LINE", params=[100.0, 100.0, 200.0, 200.0]),
        ],
        anchors=[DiagramAnchor(id="a1", labels=["A"], x=100.0, y=100.0, width=20.0, height=20.0)],
        reveals=[],
        prompt_addon="",
    )
    assert has_drawable_ink(d_shapes) is True


def test_equal_angle_assertion_accepts_angle_mark_entities():
    """Regression: `equal_angle` assertions reference AngleMark entities, not point tuples.

    The scene planner emits `assertions: [{predicate: equal_angle, entities: [ang_A, ang_D]}]`
    where ang_A is an AngleMark. Subscripting it (`ang1[0]`) raises
    "'AngleMark' object is not subscriptable", which would fail the whole scene -> visual_status
    retry_required -> diagram=null -> nothing rendered on the whiteboard.
    """
    def point(pid: str, x: float, y: float) -> SceneConstruction:
        return SceneConstruction(id=pid, operator="point", inputs={"x": x, "y": y, "label": pid}, outputs=[pid])

    def segment(sid: str, a: str, b: str) -> SceneConstruction:
        return SceneConstruction(id=sid, operator="segment", inputs={"from": a, "to": b}, outputs=[sid])

    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="A", kind="point", label="A"),
            SceneEntity(id="B", kind="point", label="B"),
            SceneEntity(id="C", kind="point", label="C"),
            SceneEntity(id="D", kind="point", label="D"),
            SceneEntity(id="E", kind="point", label="E"),
            SceneEntity(id="F", kind="point", label="F"),
            SceneEntity(id="seg_AB", kind="segment"),
            SceneEntity(id="seg_BC", kind="segment"),
            SceneEntity(id="seg_CA", kind="segment"),
            SceneEntity(id="seg_DE", kind="segment"),
            SceneEntity(id="seg_EF", kind="segment"),
            SceneEntity(id="seg_FD", kind="segment"),
            SceneEntity(id="ang_A", kind="mark"),
            SceneEntity(id="ang_D", kind="mark"),
        ],
        constructions=[
            point("A", 0.0, 0.0), point("B", 3.0, 0.0), point("C", 0.0, 4.0),
            point("D", 6.0, 0.0), point("E", 9.0, 0.0), point("F", 6.0, 4.0),
            segment("seg_AB", "A", "B"), segment("seg_BC", "B", "C"), segment("seg_CA", "C", "A"),
            segment("seg_DE", "D", "E"), segment("seg_EF", "E", "F"), segment("seg_FD", "F", "D"),
            SceneConstruction(id="ang_A", operator="angle_mark",
                              inputs={"vertex": "A", "from": "B", "to": "C"}, outputs=["ang_A"]),
            SceneConstruction(id="ang_D", operator="angle_mark",
                              inputs={"vertex": "D", "from": "E", "to": "F"}, outputs=["ang_D"]),
        ],
        relations=[],
        assertions=[
            SceneAssertion(id="a3", predicate="equal_angle",
                           entities=["ang_A", "ang_D"], expected=True, severity="error"),
        ],
        annotations=[],
        required_entity_ids=["A", "B", "C", "D", "E", "F",
                             "seg_AB", "seg_BC", "seg_CA", "seg_DE", "seg_EF", "seg_FD",
                             "ang_A", "ang_D"],
        reveal_groups=[],
    )

    scene, report = compile_scene_document(doc)
    assert not any(e.code == "assertion_eval_error" for e in report.errors), [e.message for e in report.errors]
    assert report.valid is True, [e.message for e in report.errors]
    assert report.passed_assertions == 1
    assert scene is not None


def test_isolated_points_trigger_no_connected_geometry_error():
    """Compiling a scene with 3 points but zero connecting geometry must trigger no_connected_geometry."""
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="pA", kind="point", label="A"),
            SceneEntity(id="pB", kind="point", label="B"),
            SceneEntity(id="pC", kind="point", label="C"),
        ],
        constructions=[
            SceneConstruction(id="c1", operator="point", inputs={"x": 0.0, "y": 0.0}, outputs=["pA"]),
            SceneConstruction(id="c2", operator="point", inputs={"x": 10.0, "y": 0.0}, outputs=["pB"]),
            SceneConstruction(id="c3", operator="point", inputs={"x": 5.0, "y": 8.0}, outputs=["pC"]),
        ],
        relations=[],
        assertions=[],
        annotations=[],
        required_entity_ids=["pA", "pB", "pC"],
        reveal_groups=[],
    )
    scene, report = compile_scene_document(doc)
    assert report.valid is False
    assert any(e.code == "no_connected_geometry" for e in report.errors)


def test_default_rect_output_identical():
    """Projecting with the default target rect must be byte-identical to the
    committed fixture (proves FIGURE_RECT_DEFAULT reproduces the legacy ZONE_* output
    until a layout change regenerates the fixture)."""
    from tests.make_frontend_fixture import build

    fixture = Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures/diagram_commits.json"
    assert fixture.exists(), "run backend/tests/make_frontend_fixture.py"
    events = build()
    assert json.dumps(events, indent=1) == fixture.read_text()


def test_canonical_ids():
    """Entities get canonical ids per compiled type and the alias map
    resolves LLM ids, entity labels and reversed segment names."""
    from tests.make_frontend_fixture import PLAN, SCENES

    scene, report = compile_scene_document(SceneDocument.model_validate(SCENES["triangle_and_circle"]),
                                           plan=PLAN)
    assert report.valid, [e.message for e in report.errors]
    diagram = build_verified_diagram(scene, plan=PLAN, namespace="d1_")

    ids = {a.id for a in diagram.anchors}
    for expected in ("d1_pt_A", "d1_pt_B", "d1_pt_C", "d1_seg_AB", "d1_seg_AC", "d1_tri_BAC",
                     "d1_ang_ABC", "d1_ang_BAC", "d1_cir_C", "d1_ray_CB", "d1_tickmark_tk"):
        assert expected in ids, sorted(ids)

    alias = diagram.alias_map or {}
    assert alias["sAB"] == "d1_seg_AB"
    assert alias["AB"] == "d1_seg_AB"
    assert alias["BA"] == "d1_seg_AB"
    assert alias["C"] == "d1_pt_C"

    seg_cmd = next(c for c in diagram.commands if c.anchor_id == "d1_seg_AB")
    assert seg_cmd.semantic_ref is not None
    assert seg_cmd.semantic_ref.entity_id == "d1_seg_AB"
    # glossary keys and reveal membership use canonical ids
    assert any(r.target_id == "d1_rg_marks" for r in diagram.reveals)
    assert "d1_seg_AB" in diagram.prompt_addon


def test_segment_aliases_from_endpoint_labels():
    """Constructed segments without a declared scene-doc label still resolve their
    two-letter names (and the reversed pair) through alias_map, so [FOCUS:AB] works."""
    from tests.make_frontend_fixture import PAGE_SCENES, PLAN, SCENES

    scene, report = compile_scene_document(SceneDocument.model_validate(PAGE_SCENES["wide_segments"]),
                                           plan=PLAN)
    assert report.valid, [e.message for e in report.errors]
    diagram = build_verified_diagram(scene, plan=PLAN)
    alias = diagram.alias_map or {}
    assert alias["sAB"] == "seg_AB"          # LLM id alias unchanged
    assert alias["AB"] == "seg_AB"           # derived from the endpoint letters
    assert alias["BA"] == "seg_AB"           # reversed two-letter name
    assert alias["BC"] == "seg_BC"

    # the fixture scene's undeclared sAC gains its letter aliases too
    tri, tri_report = compile_scene_document(SceneDocument.model_validate(SCENES["triangle_and_circle"]),
                                             plan=PLAN)
    assert tri_report.valid
    tri_alias = build_verified_diagram(tri, plan=PLAN, namespace="d1_").alias_map or {}
    assert tri_alias["AC"] == "d1_seg_AC"
    assert tri_alias["CA"] == "d1_seg_AC"



def test_failed_parallel_assertion_is_warning():
    """A failed parallel assertion produces a warning and allows the scene to compile."""
    pts = {"A": (0.0, 0.0), "B": (10.0, 0.0), "C": (0.0, 5.0), "D": (10.0, 8.0)}
    doc = SceneDocument(
        schema_version="scene-document/v2", visual_decision="scene", source="test", quantities=[],
        entities=[SceneEntity(id=k, kind="point") for k in pts],
        constructions=[SceneConstruction(id=f"c{k}", operator="point", inputs={"x": x, "y": y},
                                         outputs=[k]) for k, (x, y) in pts.items()]
        + [SceneConstruction(id="sAB", operator="segment", inputs={"from": "A", "to": "B"}),
           SceneConstruction(id="sCD", operator="segment", inputs={"from": "C", "to": "D"})],
        relations=[],
        assertions=[SceneAssertion(id="assert_parallel1", predicate="parallel",
                                   entities=["sAB", "sCD"], severity="warning")],
        annotations=[], required_entity_ids=[], reveal_groups=[],
    )
    scene, report = compile_scene_document(doc)
    assert report.valid is True and scene is not None
    assert any(e.code == "assertion_failed" for e in report.warnings)


def test_right_angle_mark_on_acute_vertex_auto_projects_with_warning():
    """Right angle marked at vertex with non-90 angle auto-projects arm and warns rather than aborting."""
    pts = {"A": (0.0, 0.0), "B": (4.0, 0.0), "C": (0.0, 3.0)}
    base = [SceneConstruction(id=f"c{k}", operator="point", inputs={"x": x, "y": y}, outputs=[k])
            for k, (x, y) in pts.items()] + [
        SceneConstruction(id=f"s{u}{v}", operator="segment", inputs={"from": u, "to": v})
        for u, v in (("A", "B"), ("B", "C"), ("C", "A"))]

    def doc(vertex, a, b):
        return SceneDocument(
            schema_version="scene-document/v2", visual_decision="scene", source="test",
            quantities=[], entities=[SceneEntity(id=k, kind="point") for k in pts],
            constructions=base + [SceneConstruction(id="mk", operator="right_angle_mark",
                                                    inputs={"vertex": vertex, "from": a, "to": b})],
            relations=[], assertions=[], annotations=[], required_entity_ids=[], reveal_groups=[])

    scene, report = compile_scene_document(doc("B", "A", "C"))
    assert report.valid is True
    assert any(w.code == "right_angle_auto_projected" for w in report.warnings)
    assert scene is not None
    # Verify the mark was auto-projected to an orthogonal arm
    mark = scene.entities["mk"]
    assert mark.p2.id.endswith("_auto_perp")

    _scene_good, good = compile_scene_document(doc("A", "B", "C"))
    assert good.valid is True
    assert not any(w.code == "right_angle_auto_projected" for w in good.warnings)


def test_label_entity_on_a_point_is_not_drawn_twice():
    """`label` constructions for A, B, C plus each point's own name label must not draw every
    vertex letter twice (label_lbl_A and pt_A, 15-20 px apart)."""
    from app.scene_engine.project import project_scene_to_commands

    pts = {"A": (0.0, 3.0), "B": (0.0, 0.0), "C": (4.0, 0.0)}
    doc = SceneDocument(
        schema_version="scene-document/v2", visual_decision="scene", source="test", quantities=[],
        entities=[SceneEntity(id=k, kind="point", label=k) for k in pts],
        constructions=[SceneConstruction(id=f"c{k}", operator="point",
                                         inputs={"x": x, "y": y, "label": k}, outputs=[k])
                       for k, (x, y) in pts.items()]
        + [SceneConstruction(id=f"s{u}{v}", operator="segment", inputs={"from": u, "to": v})
           for u, v in (("A", "B"), ("B", "C"), ("C", "A"))]
        + [SceneConstruction(id=f"lbl_{k}", operator="label", inputs={"target": k, "text": k})
           for k in pts],
        relations=[], assertions=[], annotations=[], required_entity_ids=[], reveal_groups=[])
    scene, report = compile_scene_document(doc)
    assert report.valid, [e.message for e in report.errors]
    commands = project_scene_to_commands(scene, namespace="")[0]
    for k in pts:
        assert sum(1 for c in commands if c.type == "LABEL" and c.text == k) == 1, k


def test_derivation_annotations_stay_off_the_figure():
    """Non-caption annotations must not become canvas LABELs at anchor centroids, or theorem
    sentences would be drawn across the triangle. Only short measurements go on the figure."""
    from app.scene_engine.verified_diagram import build_verified_diagram, is_figure_label_text
    from tests.make_frontend_fixture import PLAN, SCENES

    raw = dict(SCENES["triangle_and_circle"])
    sentence = "In triangle ABE, DF ∥ AE gives BF/FE = BD/DA"
    raw["annotations"] = list(raw["annotations"]) + [
        {"targetIds": ["sAB"], "text": sentence},
        {"targetIds": ["sAC"], "text": "B, F, E, C are collinear"},
        {"targetIds": ["sAB"], "text": "7 cm"},
    ]
    scene, report = compile_scene_document(SceneDocument.model_validate(raw), plan=PLAN)
    assert report.valid
    texts = [c.text for c in build_verified_diagram(scene, plan=PLAN).commands if c.type == "LABEL"]
    assert sentence not in texts and "B, F, E, C are collinear" not in texts
    assert "7 cm" in texts
    assert is_figure_label_text("AB = 5 cm") and not is_figure_label_text("DE ∥ AC")

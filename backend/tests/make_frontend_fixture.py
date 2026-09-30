# backend/tests/make_frontend_fixture.py
"""Writes frontend/src/__tests__/fixtures/*.json: REAL wire events produced by the backend.

- default mode: diagram_commits.json — DiagramCommit events covering every DiagramCommand
  type (rendered exactly by commandExecutor.test.ts / diagramContract.test.ts).
- --pages: page_commits.json — PageCommit events, cases one_figure, two_side_by_side,
  two_stacked, four_grid, figure_only, table_block, sticky_pair.
- --wire: wire_events.json — one instance per event wire shape.
- --all: every fixture. Run `--all` after any change to diagram output:
  PYTHONPATH=backend:. .venv/bin/python backend/tests/make_frontend_fixture.py --all
"""
import argparse
import json
from pathlib import Path

from app.contracts.messages import DiagramCommit, LessonPlanEvt, PageCommit, PageHeader
from app.contracts.scene import SceneDocument
from app.contracts.turn_plan import TurnPlan
from app.scene_engine.compile import compile_scene_document
from app.scene_engine.layout import BlockSpec, PageIntent, compute_layout
from app.scene_engine.page_commit import build_page_commit
from app.scene_engine.project import project_scene_to_commands
from app.scene_engine.verified_diagram import build_verified_diagram

FIXTURES = Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures"

PLAN = TurnPlan.model_validate({
    "givens": [{"id": "q_ab", "symbol": "AB", "value": 5, "unit": "cm"},
               {"id": "q_bc", "symbol": "BC", "value": 12, "unit": "cm"}],
    "derived": [{"id": "q_ac", "symbol": "AC", "value": 13, "unit": "cm", "provenance": "derived"}]})

SCENES = {
    "triangle_and_circle": {
        "entities": [{"id": "sAB", "kind": "segment", "label": "AB"}],
        "constructions": [
            {"id": "tri", "operator": "triangle_sas", "inputs": {"b": "q_bc", "angleDeg": 90, "c": "q_ab", "labels": ["B", "A", "C"]}},
            {"id": "mk", "operator": "right_angle_mark", "inputs": {"vertex": "B", "from": "A", "to": "C"}},
            {"id": "angA", "operator": "angle_mark", "inputs": {"vertex": "A", "from": "B", "to": "C"}},
            {"id": "sAB", "operator": "segment", "inputs": {"from": "A", "to": "B"}},
            {"id": "sAC", "operator": "segment", "inputs": {"from": "A", "to": "C"}},
            {"id": "tk", "operator": "tick_mark", "inputs": {"segment": "sAB", "count": 2}},
            {"id": "dim", "operator": "dimension", "inputs": {"from": "B", "to": "C", "text": "12 cm", "offset": 18}},
            {"id": "circ", "operator": "circle", "inputs": {"center": "C", "radius": 3}},
            {"id": "arcC", "operator": "arc", "inputs": {"center": "C", "from": "B", "to": "A"}},
            {"id": "rayCB", "operator": "ray", "inputs": {"from": "C", "through": "B"}},
        ],
        "annotations": [{"targetIds": ["sAB"], "quantityId": "q_ab"}, {"targetIds": ["sAC"], "quantityId": "q_ac"}],
        "revealGroups": [{"id": "rg_marks", "entityIds": ["mk", "angA", "tk"]}],
    },
    "graph": {"constructions": [
        {"id": "ax", "operator": "axes", "inputs": {"xRange": [-1, 4], "yRange": [-1, 7]}},
        {"id": "g", "operator": "function_curve", "inputs": {"expression": "6 - 2*x", "domain": [0, 3]}}]},
    "number_line": {"constructions": [
        {"id": "nl", "operator": "number_line", "inputs": {"range": [-3, 3], "tickStep": 1, "marks": [-2, 1.5]}}]},
}

# Page-commit-only scenes (kept out of SCENES so the diagram_commits fixture is unchanged).
PAGE_SCENES = {
    "wide_segments": {
        "entities": [
            {"id": "A", "kind": "point", "label": "A"},
            {"id": "B", "kind": "point", "label": "B"},
            {"id": "C", "kind": "point", "label": "C"},
        ],
        "constructions": [
            {"id": "c1", "operator": "point", "inputs": {"x": 0, "y": 0, "label": "A"}, "outputs": ["A"]},
            {"id": "c2", "operator": "point", "inputs": {"x": 12, "y": 0, "label": "B"}, "outputs": ["B"]},
            {"id": "c3", "operator": "point", "inputs": {"x": 12, "y": 2, "label": "C"}, "outputs": ["C"]},
            {"id": "sAB", "operator": "segment", "inputs": {"from": "A", "to": "B"}},
            {"id": "sBC", "operator": "segment", "inputs": {"from": "B", "to": "C"}},
        ],
    },
}

TABLE_LINES = ["Side | Length", "AD | 1.5 cm", "DB | 3 cm"]


def _scene(name: str):
    raw = PAGE_SCENES[name] if name in PAGE_SCENES else SCENES[name]
    scene, report = compile_scene_document(SceneDocument.model_validate(raw), plan=PLAN)
    assert report.valid, (name, [e.message for e in report.errors])
    return scene


def _aspect(scene) -> float:
    """Preferred aspect for a figure = the projector value."""
    return project_scene_to_commands(scene, namespace="")[2]


def build() -> list[dict]:
    out = []
    for i, (name, raw) in enumerate(SCENES.items()):
        scene = _scene(name)
        diagram = build_verified_diagram(scene, plan=PLAN)
        evt = DiagramCommit(generation=1, turn_id=f"t_{name}", page_id="p1", diagram=diagram, seq=i + 1)
        out.append(json.loads(evt.model_dump_json(by_alias=True, exclude_none=True)))
    return out


def _commit(commit_id: str, layout, figures, texts, revealed, carried_ids) -> PageCommit:
    pc = build_page_commit("t_p2", "L_1a2b_p1", commit_id, layout, figures, texts, revealed, carried_ids)
    return pc.model_copy(update={"generation": 6})


def _one_figure_commit() -> PageCommit:
    tri = _scene("triangle_and_circle")
    intent = PageIntent(has_work=True,
                        blocks=[BlockSpec(id="fig_tri", role="figure", preferred_aspect=_aspect(tri))])
    return _commit("c_p1_1", compute_layout(intent), {"fig_tri": (tri, PLAN, "")}, {}, {}, [])


def _dump_pc(pc: PageCommit) -> dict:
    d = json.loads(pc.model_dump_json(by_alias=True, exclude_none=True))
    if pc.work_rect is None:
        # workRect is required (null = figure-only page); exclude_none would drop it.
        d["workRect"] = None
    return d


def build_pages() -> list[dict]:
    tri = _scene("triangle_and_circle")
    graph = _scene("graph")
    wide = _scene("wide_segments")
    tri_spec = lambda i, sticky=False: BlockSpec(                                  # noqa: E731
        id=f"fig{i}", role="figure", preferred_aspect=_aspect(tri), sticky=sticky)
    graph_spec = lambda i, sticky=False: BlockSpec(                                 # noqa: E731
        id=f"g{i}", role="figure", preferred_aspect=_aspect(graph), sticky=sticky)
    wide_spec = lambda i: BlockSpec(                                                # noqa: E731
        id=f"w{i}", role="figure", preferred_aspect=_aspect(wide))

    pages = [
        _one_figure_commit(),
        _commit("c_p1_2",
                compute_layout(PageIntent(has_work=True, hint="side_by_side",
                                          blocks=[graph_spec(0), graph_spec(1)])),
                {"g0": (graph, PLAN, ""), "g1": (graph, PLAN, "")}, {}, {}, []),
        _commit("c_p1_3",
                compute_layout(PageIntent(has_work=True, hint="stacked",
                                          blocks=[wide_spec(0), wide_spec(1)])),
                {"w0": (wide, PLAN, ""), "w1": (wide, PLAN, "")}, {}, {}, []),
        _commit("c_p1_4",
                compute_layout(PageIntent(has_work=True,
                                          blocks=[tri_spec(i) for i in range(4)])),
                {f"fig{i}": (tri, PLAN, "") for i in range(4)}, {}, {}, []),
        _commit("c_p1_5",
                compute_layout(PageIntent(has_work=False, blocks=[graph_spec(0)])),
                {"g0": (graph, PLAN, "")}, {}, {}, []),
        _commit("c_p1_6",
                compute_layout(PageIntent(has_work=True, blocks=[
                    BlockSpec(id="tbl_ratio", role="table",
                              preferred_aspect=(2 * 110) / (len(TABLE_LINES) * 36))])),
                {}, {"tbl_ratio": TABLE_LINES}, {}, []),
        _commit("c_p1_7",
                compute_layout(PageIntent(has_work=True, hint="side_by_side",
                                          blocks=[graph_spec(0, sticky=True), graph_spec(1)])),
                {"g0": (graph, PLAN, ""), "g1": (graph, PLAN, "")}, {}, {}, ["g0"]),
    ]
    return [_dump_pc(p) for p in pages]


def build_wire() -> list[dict]:
    # One instance per wire event shape: page_commit and lesson_plan.
    pc = _one_figure_commit().model_copy(update={"generation": 6, "seq": 90, "epoch": "9f2c1a0b"})
    plan = LessonPlanEvt(
        generation=5, seq=70, epoch="9f2c1a0b", lesson_id="L_1a2b",
        title="Basic Proportionality Theorem",
        pages=[PageHeader(page_id="L_1a2b_p0", index=0, title="What BPT says"),
               PageHeader(page_id="L_1a2b_p1", index=1, title="Proof")],
    )
    return [
        json.loads(pc.model_dump_json(by_alias=True, exclude_none=True)),
        json.loads(plan.model_dump_json(by_alias=True, exclude_none=True)),
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Regenerate the cross-stack frontend wire fixtures")
    parser.add_argument("--pages", action="store_true", help="write page_commits.json")
    parser.add_argument("--wire", action="store_true", help="write wire_events.json")
    parser.add_argument("--all", action="store_true", help="write every fixture (default + --pages + --wire)")
    args = parser.parse_args()

    if args.all or not (args.pages or args.wire):
        target = FIXTURES / "diagram_commits.json"
        events = build()
        target.write_text(json.dumps(events, indent=1))
        types = sorted({c["type"] for e in events for c in e["diagram"]["commands"]})
        print(f"wrote {target.name}: {len(events)} events, command types: {types}")
    if args.all or args.pages:
        target = FIXTURES / "page_commits.json"
        events = build_pages()
        target.write_text(json.dumps(events, indent=1))
        print(f"wrote {target.name}: {len(events)} events")
    if args.all or args.wire:
        target = FIXTURES / "wire_events.json"
        events = build_wire()
        target.write_text(json.dumps(events, indent=1))
        print(f"wrote {target.name}: {len(events)} events")

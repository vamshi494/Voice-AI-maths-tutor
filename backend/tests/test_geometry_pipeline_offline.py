# backend/tests/test_geometry_pipeline_offline.py
"""Geometry pipeline, end to end, with NO LLM calls.

Runs the REAL LangGraph (TurnPlan dual lane -> ProblemIR || Scene plan+compile -> reconcile
-> teaching stream) with a MockGateway that returns scripted RAW model text. The raw text
goes through the production path (extract_json_object + schema validation), so these tests
also pin the lenient-contract fixes: <think> preambles, prose around JSON, "5 cm" values,
extra keys, snake_case inputs and a model that wrongly says visualRequirement "none".

Why this file exists: the scene compiler must accept the most natural LLM output shapes,
and the frontend must not throw on the anchor shape before drawing. Every assertion below
corresponds to a link in that chain.
"""
import json
from typing import Any
from uuid import uuid4

import pytest

import app.agents.graph as graph_mod
from app.contracts.agent_state import AgentState, PageRecord, TurnRequest
from app.contracts.diagram import has_drawable_ink
from app.gateway.groq_client import GroqGateway, _set_err, extract_json_object, summarize_validation_error
from app.transport import PACKET_LIMIT_BYTES

QUESTION = "In triangle ABC, AB = 5 cm, BC = 12 cm and angle B = 90 degrees. Find AC."

TURN_PLAN_RAW = """<think>Right triangle, use Pythagoras.</think>
Here is the plan:
```json
{"schemaVersion": "turn-plan/v3", "question": "In triangle ABC, AB=5cm, BC=12cm, angle B=90. Find AC",
 "givens": [{"id": "q_ab", "symbol": "AB", "value": "5 cm", "unit": "cm"},
            {"id": "q_bc", "symbol": "BC", "value": 12, "unit": "cm"}],
 "unknowns": [{"id": "q_ac", "symbol": "AC"}],
 "derived": [{"id": "q_ac", "symbol": "AC", "unit": "cm", "provenance": "derived",
              "computation": "sqrt(5^2 + 12^2) = 13", "dependsOn": "q_ab, q_bc"}],
 "lawIds": ["pythagoras_theorem"], "visualRequirement": "none", "notes": "extra key"}
```"""

SCENE_BAD_REF = json.dumps({
    "constructions": [
        {"id": "tri", "operator": "triangle_sas",
         "inputs": {"b": "q_bc", "angle_deg": 90, "c": "q_ab", "labels": ["B", "A", "C"]}},
        {"id": "sAD", "operator": "segment", "inputs": {"from": "A", "to": "D"}},
    ]})

SCENE_GOOD = """Sure! {"schemaVersion": "v2", "visualDecision": "scene",
 "entities": [{"id": "A", "kind": "point", "label": "A"}],
 "constructions": [
   {"id": "tri", "operator": "triangle_sas",
    "inputs": {"b": "q_bc", "angle_deg": "90", "c": "q_ab", "labels": ["B", "A", "C"]}},
   {"id": "mk", "operator": "right_angle_mark", "inputs": {"vertex": "B", "from": "A", "to": "C"}},
   {"id": "sAB", "operator": "segment", "inputs": {"points": ["A", "B"]}},
   {"id": "sAC", "operator": "segment", "inputs": {"from": "A", "to": "C"}}],
 "assertions": [{"predicate": "angle_between", "entities": ["B", "A", "C"], "expected": 90}],
 "annotations": [{"targetIds": ["sAB"], "quantityId": "q_ab"}, {"targetIds": ["sAC"], "quantityId": "q_ac"}],
 "revealGroups": [{"id": "rg_setup", "entityIds": ["tri", "mk", "sAB"]}],
 "requiredEntityIds": "A, B, C"} Hope this helps."""

TEACHING = (
    "[STEP]Look at triangle ABC. [FOCUS:tri_BAC] The angle at B is a right angle. [FOCUS:ang_ABC] "
    "[WRITE:AB = 5 cm, BC = 12 cm][/STEP]"
    "[STEP]So we use Pythagoras. [WRITE:AC^2 = 5^2 + 12^2 = 169][FOCUS:not_a_real_anchor][/STEP]"
    "[STEP]So AC is thirteen centimetres. [ANNOTATE:seg_AC] [WRITE:AC = 13 cm][/STEP]"
)


class MockGateway(GroqGateway):
    """Scripted replies per schema name, parsed exactly like production replies."""

    def __init__(self, replies: dict[str, list[str]], teaching: str = TEACHING) -> None:
        super().__init__(api_key="offline")
        self.replies = {k: list(v) for k, v in replies.items()}
        self.teaching = teaching
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[Any] = []

    async def complete_json(self, *, prompt_key: str, schema: Any, user: str = "", **kw: Any) -> Any:
        self.calls.append({"prompt_key": prompt_key, "schema": schema.__name__, "user": user,
                           "fmt_args": kw.get("fmt_args") or {}})
        queue = self.replies.get(schema.__name__) or []
        if not queue:
            _set_err("no scripted reply")
            return None
        raw = queue.pop(0) if len(queue) > 1 else queue[0]
        try:
            return schema.model_validate(extract_json_object(raw))
        except Exception as e:
            self.last_error = _set_err(summarize_validation_error(e))
            return None

    async def stream_text(self, *, prompt_key: str, messages: Any, model: str, timeout_s: float, **kw: Any):
        self.stream_calls.append({"prompt_key": prompt_key, "messages": messages})
        for i in range(0, len(self.teaching), 23):          # arbitrary chunking, like SSE deltas
            yield self.teaching[i:i + 23]

    def calls_for(self, schema_name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["schema"] == schema_name]


async def _run(monkeypatch, gw: MockGateway, question: str = QUESTION, stale: bool = False):
    st = AgentState(session_id="s1", user_id="u1", board_id="b1", generation=3)
    turn_id = str(uuid4())
    st.page = PageRecord(board_id="b1", page_id="p_test", lesson_question=question, turn_kind="lesson", turn_id=turn_id)
    st.active_turn_id = turn_id
    st.active_turn_kind = "lesson"
    req = TurnRequest(kind="lesson", generation=st.generation, turn_id=turn_id, question=question)
    if stale:
        st.generation += 1                                   # superseded before the graph ran
    steps = [step async for step, _tts in graph_mod.iter_turn_steps(req, agent_state=st, gw=gw)]
    return st, req, steps


@pytest.mark.asyncio
async def test_geometry_question_produces_committed_verified_diagram(monkeypatch):
    gw = MockGateway({"TurnPlan": [TURN_PLAN_RAW], "SceneDocument": [SCENE_GOOD], "ProblemIR": []})
    st, req, steps = await _run(monkeypatch, gw)

    # The graph writes the page record; the manager publishes DiagramCommit (see
    # test_resume_parked.py::test_manager_publishes_commit_before_steps).
    d = st.page.diagram
    assert d is not None, "a geometric question must produce exactly one verified diagram"
    assert has_drawable_ink(d)

    labels = [c.text for c in d.commands if c.type == "LABEL"]
    assert {"A", "B", "C"} <= set(labels), "vertices must be lettered"
    assert "AB = 5 cm" in labels, "given measurements are drawn"
    assert not any("13" in (t or "") for t in labels), "the answer is withheld until [ANNOTATE]"
    assert [da.entity_id for da in d.deferred_annotations] == ["seg_AC"]
    assert d.deferred_annotations[0].commands[0].text == "AC = 13 cm"
    assert {"DRAW_POLYLINE", "DRAW_RIGHT_ANGLE_MARK"} <= {c.type for c in d.commands}
    for c in d.commands:                                      # inside the board's diagram zone
        assert 420 <= c.params[0] <= 1140 and 40 <= c.params[1] <= 640, (c.type, c.params)

    # Wire contract the React client depends on (see frontend types/events.ts)
    wire = json.loads(d.model_dump_json(by_alias=True, exclude_none=True))
    assert len(json.dumps(wire).encode()) < PACKET_LIMIT_BYTES
    for a in wire["anchors"]:
        assert {"id", "x", "y", "width", "height", "labels"} <= set(a) and "box" not in a

    # Page record: what this page was taught from (doubts inherit it)
    assert st.page.figure_drawn is True
    assert st.page.turn_plan is not None and st.page.turn_plan.visual_requirement == "optional"
    assert st.lesson_topic == "pythagoras theorem"


@pytest.mark.asyncio
async def test_teaching_ops_reference_only_real_anchors(monkeypatch):
    gw = MockGateway({"TurnPlan": [TURN_PLAN_RAW], "SceneDocument": [SCENE_GOOD], "ProblemIR": []})
    st, req, steps = await _run(monkeypatch, gw)
    anchor_ids = {a.id for a in st.page.diagram.anchors}
    focus = [op for s in steps for op in s.ops if op.kind == "FOCUS"]
    assert [op.entity_id for op in focus] == ["tri_BAC", "ang_ABC"], "unknown FOCUS ids are dropped server-side"
    assert all(op.entity_id in anchor_ids for op in focus)
    annotate = [op for s in steps for op in s.ops if op.kind == "ANNOTATE"]
    assert [op.entity_id for op in annotate] == ["seg_AC"]
    # the teaching prompt was told the anchors in the descriptive form (canonical ids)
    prompt_text = json.dumps(gw.stream_calls[0]["messages"])
    assert "seg_AB: side AB (AB = 5 cm)" in prompt_text and "ang_ABC: right angle ABC" in prompt_text


@pytest.mark.asyncio
async def test_scene_repair_attempt_receives_the_compile_error(monkeypatch):
    gw = MockGateway({"TurnPlan": [TURN_PLAN_RAW], "SceneDocument": [SCENE_BAD_REF, SCENE_GOOD], "ProblemIR": []})
    st, _req, _steps = await _run(monkeypatch, gw)
    scene_calls = gw.calls_for("SceneDocument")
    assert len(scene_calls) == 2
    assert "unresolved_reference" in scene_calls[1]["user"] and "'D'" in scene_calls[1]["user"]
    assert st.page.diagram is not None


@pytest.mark.asyncio
async def test_scene_repair_attempt_receives_the_parse_error(monkeypatch):
    gw = MockGateway({"TurnPlan": [TURN_PLAN_RAW],
                      "SceneDocument": ["I am not able to produce JSON for this.", SCENE_GOOD], "ProblemIR": []})
    st, _req, _steps = await _run(monkeypatch, gw)
    repair_prompt = gw.calls_for("SceneDocument")[1]["user"]
    assert "[invalid_json]" in repair_prompt and "no JSON object" in repair_prompt
    assert st.page.diagram is not None


@pytest.mark.asyncio
async def test_geometry_figure_survives_a_failed_turn_plan(monkeypatch):
    """Scene is decoupled from the numeric plan: a geometry question still gets its figure."""
    literal_scene = SCENE_GOOD.replace('"q_bc"', "12").replace('"q_ab"', "5")
    gw = MockGateway({"TurnPlan": ["not json at all"], "SceneDocument": [literal_scene], "ProblemIR": []})
    st, _req, steps = await _run(monkeypatch, gw)
    assert st.page.diagram is not None
    assert steps, "teaching still happens"


@pytest.mark.asyncio
async def test_non_geometric_question_skips_the_scene(monkeypatch):
    plan = json.dumps({"question": "Solve 2x + 3 = 11", "givens": [], "unknowns": [{"id": "x", "symbol": "x"}],
                       "derived": [{"id": "x", "symbol": "x", "value": 4, "sourceText": "(11 - 3) / 2 = 4"}],
                       "visualRequirement": "none"})
    gw = MockGateway({"TurnPlan": [plan], "SceneDocument": [SCENE_GOOD], "ProblemIR": []},
                     teaching="[STEP]Subtract three from both sides. [WRITE:2x = 8][/STEP]")
    st, _req, steps = await _run(monkeypatch, gw, question="Solve 2x + 3 = 11")
    assert gw.calls_for("SceneDocument") == [] and st.page.diagram is None
    assert len(steps) == 1


@pytest.mark.asyncio
async def test_superseded_turn_commits_nothing(monkeypatch):
    gw = MockGateway({"TurnPlan": [TURN_PLAN_RAW], "SceneDocument": [SCENE_GOOD], "ProblemIR": []})
    st, _req, steps = await _run(monkeypatch, gw, stale=True)
    assert st.page.diagram is None and steps == [], "a stale turn must not write into the current page"

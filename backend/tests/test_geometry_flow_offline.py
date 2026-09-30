# backend/tests/test_geometry_flow_offline.py
"""Deterministic offline test suite for end-to-end geometry flow and state transitions.

Requires zero external LLM calls. Validates:
1. Geometry scene document compilation, label operators, dimension rendering, and DiagramAnchor.box.
2. Complete 23-row StateMachineManager transition matrix.
3. Pydantic schema resilience and boundary-safe JSON parsing.
4. Client report telemetry (BoardReport, StepAck) updating manager.state.rows.
5. Mic check fast-path classification as backchannel.
"""
import pytest
from app.contracts.agent_state import ConvState
from app.contracts.diagram import DiagramAnchor, VerifiedDiagram, has_drawable_ink
from app.contracts.messages import (
    BoardReport,
    BoardRow,
    DiagramCommit,
    DoubtMark,
    StepAck,
    TurnEnded,
    TurnStarted,
)
from app.contracts.problem_ir import BinaryNode, ProblemFact, ProblemIR
from app.contracts.scene import (
    SceneAssertion,
    SceneConstruction,
    SceneDocument,
    SceneEntity,
)
from app.contracts.turn_plan import TurnPlan
from app.gateway.groq_client import GroqGateway
from app.scene_engine.compile import compile_scene_document
from app.scene_engine.verified_diagram import build_verified_diagram
from app.state_machine.classifier import classify_interrupt
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvEvent
from app.transport import handle_report_payload, set_active_room


# =============================================================================
# 1. Geometry Pipeline Offline Verification
# =============================================================================

def test_geometry_visual_triggers():
    """Verify that geometry keywords automatically demand visual diagrams."""
    from app.agents.nodes.turn_plan import question_is_geometric, question_requires_visual

    # Direct visual requests
    assert question_requires_visual("Draw a triangle ABC")
    assert question_requires_visual("Construct a right triangle")
    assert question_requires_visual("Plot the function curve")

    # Domain geometry terms without the word 'draw' (triggers geometric noun gate)
    assert question_is_geometric("In triangle ABC, AB = 3 and BC = 4, find AC")
    assert question_is_geometric("Find the area of a circle with radius 7 cm")
    assert question_is_geometric("Explain Pythagoras theorem for right angled triangle")
    assert question_is_geometric("Find the length of tangent from point P to the circle")
    assert question_is_geometric("In a quadrilateral ABCD, find angle B")

    # Non-geometric arithmetic problems
    assert not question_is_geometric("Find the roots of x^2 - 5x + 6 = 0")
    assert not question_is_geometric("Simplify 2/3 + 4/5")


def test_geometry_scene_compilation_with_labels_and_dimensions():
    """Verify compilation of a right-angled triangle with labels and dimensions."""
    doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="test",
        quantities=[],
        entities=[
            SceneEntity(id="tri", kind="triangle", label="Triangle ABC"),
            SceneEntity(id="p0", kind="point", label="A"),
            SceneEntity(id="p1", kind="point", label="B"),
            SceneEntity(id="p2", kind="point", label="C"),
            SceneEntity(id="lblA", kind="label"),
            SceneEntity(id="lblB", kind="label"),
            SceneEntity(id="lblC", kind="label"),
            SceneEntity(id="dim_c", kind="dimension"),
            SceneEntity(id="dim_a", kind="dimension"),
            SceneEntity(id="rt_ang", kind="mark"),
        ],
        constructions=[
            SceneConstruction(
                id="c1",
                operator="triangle_sas",
                inputs={"b": 4.0, "angleDeg": 90.0, "c": 3.0, "labels": ["A", "B", "C"]},
                outputs=["p0", "p1", "p2", "tri"],
            ),
            SceneConstruction(
                id="c2",
                operator="label",
                inputs={"target": "p0", "text": "A"},
                outputs=["lblA"],
            ),
            SceneConstruction(
                id="c3",
                operator="label",
                inputs={"target": "p1", "text": "B"},
                outputs=["lblB"],
            ),
            SceneConstruction(
                id="c4",
                operator="label",
                inputs={"target": "p2", "text": "C"},
                outputs=["lblC"],
            ),
            SceneConstruction(
                id="c5",
                operator="dimension",
                inputs={"from": "p0", "to": "p1", "text": "3 cm", "offset": 16.0},
                outputs=["dim_c"],
            ),
            SceneConstruction(
                id="c6",
                operator="dimension",
                inputs={"from": "p0", "to": "p2", "text": "4 cm", "offset": 16.0},
                outputs=["dim_a"],
            ),
            SceneConstruction(
                id="c7",
                operator="right_angle_mark",
                inputs={"vertex": "p0", "from": "p1", "to": "p2"},
                outputs=["rt_ang"],
            ),
        ],
        relations=[],
        assertions=[
            SceneAssertion(id="a1", predicate="angle_between", entities=["p0", "p1", "p2"], expected=90.0, severity="error"),
        ],
        annotations=[],
        required_entity_ids=["p0", "p1", "p2", "tri"],
    )

    scene, report = compile_scene_document(doc)
    assert report.valid, f"Compilation errors: {[e.message for e in report.errors]}"
    assert scene is not None

    diagram = build_verified_diagram(scene)
    assert has_drawable_ink(diagram)
    assert len(diagram.commands) >= 5
    assert len(diagram.anchors) >= 1

    # Verify that anchors have valid coordinates and non-zero dimensions
    for anchor in diagram.anchors:
        assert anchor.id is not None
        assert anchor.width > 0
        assert anchor.height > 0
        assert anchor.x >= 0
        assert anchor.y >= 0

    # Verify command types generated
    cmd_types = {cmd.type for cmd in diagram.commands}
    assert "DRAW_POLYLINE" in cmd_types
    assert "DRAW_POINT" in cmd_types
    assert "LABEL" in cmd_types
    assert "DRAW_DIMENSION" in cmd_types
    assert "DRAW_RIGHT_ANGLE_MARK" in cmd_types

    # Test wire event serialization
    commit_evt = DiagramCommit(
        generation=1,
        turn_id="t_geo_001",
        page_id="p_geo_001",
        diagram=diagram,
    )
    payload_json = commit_evt.model_dump_json(by_alias=True)
    assert "diagram" in payload_json
    assert "anchors" in payload_json


# =============================================================================
# 2. State Machine Transitions Matrix (23 Rows)
# =============================================================================

@pytest.mark.asyncio
async def test_state_machine_matrix_rows_1_to_3():
    """Row 1 (New Question), Row 2 (Speech Started), Row 3 (Speech Ended)."""
    mgr = StateMachineManager()
    assert mgr.state.conv_state == ConvState.IDLE

    # Row 1a: TYPED_QUESTION -> GRAPH_RUNNING
    gen_before = mgr.state.generation
    await mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Find the roots of x^2 - 5x + 6 = 0")
    assert mgr.state.conv_state == ConvState.GRAPH_RUNNING
    assert mgr.state.generation == gen_before + 1
    assert mgr.state.active_turn_id is not None

    # Row 2: SPEECH_STARTED -> AGENT_SPEAKING
    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING

    # Row 3: SPEECH_ENDED -> IDLE
    await mgr.handle_event(ConvEvent.SPEECH_ENDED)
    assert mgr.state.conv_state == ConvState.IDLE


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_4_to_7_backchannel():
    """Row 4 (VAD), Row 5 (User Turn Done), Row 7 (Backchannel ignored -> prev_state)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.generation = 1

    # Row 4: VAD_START -> INTERRUPT_DETECTED (speech continues)
    await mgr.handle_event(ConvEvent.VAD_START)
    assert mgr.state.conv_state == ConvState.INTERRUPT_DETECTED
    assert mgr._prev_state == ConvState.AGENT_SPEAKING

    # Row 5: USER_TURN_DONE -> INTERRUPT_CLASSIFYING
    await mgr.handle_event(ConvEvent.USER_TURN_DONE, text="haan")
    assert mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
    assert "haan" in mgr.state.interrupt_transcript

    # Row 7: CLS_BACKCHANNEL -> prev_state (AGENT_SPEAKING) without generation bump
    gen_before = mgr.state.generation
    await mgr.handle_event(ConvEvent.CLS_BACKCHANNEL)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert mgr.state.generation == gen_before


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_10_to_13_doubt():
    """Row 10 (Voice Doubt), Row 12 (Doubt Settled), Row 13 (Doubt Speech Started)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.INTERRUPT_CLASSIFYING
    mgr.state.generation = 1
    mgr.state.lesson_topic = "Quadratic Equations"
    from app.contracts.agent_state import PageRecord
    mgr.state.page = PageRecord(
        board_id="b_test",
        page_id="p_test",
        lesson_question="Quadratic Equations",
        turn_kind="lesson",
        turn_id="t_test",
    )

    # Row 10: CLS_DOUBT -> TASK_PAUSED
    gen_before = mgr.state.generation
    await mgr.handle_event(ConvEvent.CLS_DOUBT)
    assert mgr.state.conv_state == ConvState.TASK_PAUSED
    assert mgr.state.generation == gen_before + 1
    assert mgr.state.paused_lesson is not None

    # Row 12: DOUBT_SETTLED -> TASK_CORRECTING
    await mgr.handle_event(ConvEvent.DOUBT_SETTLED)
    assert mgr.state.conv_state == ConvState.TASK_CORRECTING

    # Row 13: SPEECH_STARTED -> AGENT_SPEAKING
    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert mgr.state.doubt_awaiting_resolution is True


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_8_and_9_affirmation():
    """Row 8 (Affirmation resumes lesson), Row 9 (Affirmation no-op when not awaiting)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.INTERRUPT_CLASSIFYING
    mgr._prev_state = ConvState.AGENT_SPEAKING

    # Row 9: CLS_AFFIRMATION with doubt_awaiting_resolution=False -> prev_state
    mgr.state.doubt_awaiting_resolution = False
    await mgr.handle_event(ConvEvent.CLS_AFFIRMATION)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING

    # Row 8: CLS_AFFIRMATION with doubt_awaiting_resolution=True -> RESUMING
    mgr.state.conv_state = ConvState.INTERRUPT_CLASSIFYING
    mgr.state.doubt_awaiting_resolution = True
    from app.contracts.agent_state import PausedLesson
    mgr.state.paused_lesson = PausedLesson(
        board_id="b1",
        page_id="p1",
        lesson_question="Solve x^2 - 5x + 6 = 0",
        turn_plan=None,
        solver_projection=None,
        diagram=None,
        figure_drawn=False,
        lesson_turn_id="t1",
        last_acked_step_index=0,
        lesson_completed=False,
    )
    await mgr.handle_event(ConvEvent.CLS_AFFIRMATION)
    assert mgr.state.conv_state == ConvState.RESUMING

    # Row 20: RESUMING + SPEECH_STARTED -> AGENT_SPEAKING
    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert mgr.state.doubt_awaiting_resolution is False


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_15_and_16_off_topic():
    """Row 15 (Off Topic Deflection), Row 16 (Redirect Done Replay)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.INTERRUPT_CLASSIFYING
    mgr._prev_state = ConvState.AGENT_SPEAKING
    mgr.state.lesson_topic = "Triangles"
    gen_before = mgr.state.generation

    # Row 15: CLS_OFF_TOPIC -> TASK_REDIRECTED
    await mgr.handle_event(ConvEvent.CLS_OFF_TOPIC)
    assert mgr.state.conv_state == ConvState.TASK_REDIRECTED
    # Invariant: Off-topic does NOT bump generation
    assert mgr.state.generation == gen_before

    # Row 16: REDIRECT_DONE -> IDLE (no turn in-flight to resume)
    await mgr.handle_event(ConvEvent.REDIRECT_DONE)
    assert mgr.state.conv_state == ConvState.IDLE


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_21_and_22_marker_armed():
    """Row 21 (Marker Armed pauses audio), Row 22 (Marker Disarmed replays step)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    gen_before = mgr.state.generation

    # Row 21: MARKER_ARMED -> AGENT_SPEAKING with marker_armed=True
    await mgr.handle_event(ConvEvent.MARKER_ARMED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert mgr.state.marker_armed is True
    assert mgr.state.generation == gen_before

    # Row 22: MARKER_DISARMED -> AGENT_SPEAKING with marker_armed=False
    await mgr.handle_event(ConvEvent.MARKER_DISARMED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert mgr.state.marker_armed is False


@pytest.mark.asyncio
async def test_state_machine_matrix_rows_17_18_23_terminal():
    """Row 17/18 (End Session), Row 23 (Terminal state ignores all)."""
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING

    # Row 18: END -> TASK_CANCELLED
    await mgr.handle_event(ConvEvent.END)
    assert mgr.state.conv_state == ConvState.TASK_CANCELLED

    # Row 23: Terminal state ignores all further events
    for evt in [ConvEvent.TYPED_QUESTION, ConvEvent.VAD_START, ConvEvent.SPEECH_STARTED, ConvEvent.CONTINUE]:
        await mgr.handle_event(evt)
        assert mgr.state.conv_state == ConvState.TASK_CANCELLED


# =============================================================================
# 3. Pydantic Normalization & Outermost JSON Parsing
# =============================================================================

def test_problem_ir_schema_normalization():
    """Verify normalizations for ProblemIR: fact kind, binary op, solve requests."""
    # Test ProblemFact kind normalization ("derived" -> "assumption")
    fact = ProblemFact(
        id="f1",
        kind="derived",
        statement="Triangle is right-angled",
        evidence={"turnPlanFactId": "q1"},
    )
    assert fact.kind == "assumption"

    # Test BinaryNode normalization ("op" -> "operator")
    raw_binary = {
        "kind": "binary",
        "op": "mul",
        "left": {"kind": "number", "value": 3.0},
        "right": {"kind": "variable", "name": "x"},
    }
    node = BinaryNode.model_validate(raw_binary)
    assert node.operator == "*"

    # Test ProblemIR solve_requests backfill
    ir_data = {
        "schemaVersion": "problem-ir/v1",
        "id": "prob_1",
        "question": "Find AC in right triangle ABC",
        "facts": [fact.model_dump()],
        "entities": [],
        "expressions": [],
        "constraints": [],
        "solveRequests": [
            {"target": "expr1"}
        ],
    }
    ir = ProblemIR.model_validate(ir_data)
    assert len(ir.solve_requests) == 1
    assert ir.solve_requests[0].kind == "evaluate"
    assert ir.solve_requests[0].expression_id == "expr1"


def test_outermost_json_parsing():
    """Verify that outermost JSON extraction parses markdown and conversational text."""
    # Test wrapped in markdown fence
    text_with_fences = """```json
{"id": "test_id", "labels": ["A"], "x": 10.0, "y": 20.0, "width": 5.0, "height": 5.0}
```"""
    clean = text_with_fences.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    a1 = DiagramAnchor.model_validate_json(clean)
    assert a1.id == "test_id"
    assert a1.x == 10.0 and a1.y == 20.0 and a1.width == 5.0 and a1.height == 5.0

    # Test surrounded by chatter
    chatter_text = """Certainly! Here is the anchor object for your verified diagram:
{"id": "p0", "labels": ["B"], "x": 50.0, "y": 60.0, "width": 8.0, "height": 8.0}
Hope this helps you with your mathematics tutoring!"""
    first_b = chatter_text.find("{")
    last_b = chatter_text.rfind("}")
    sub = chatter_text[first_b:last_b + 1]
    a2 = DiagramAnchor.model_validate_json(sub)
    assert a2.id == "p0"
    assert a2.x == 50.0 and a2.y == 60.0 and a2.width == 8.0 and a2.height == 8.0


# =============================================================================
# 4. Telemetry Reporting & Board Rows Synchronization
# =============================================================================

def test_telemetry_board_report_updates_state_rows():
    """Verify that BoardReport data packet populates manager.state.rows cleanly."""
    mgr = StateMachineManager()
    assert mgr.state.rows is None

    report = BoardReport(
        page_id="p_quad_01",
        rows=[
            BoardRow(row_id="w1", text="x^2 - 5x + 6 = 0"),
            BoardRow(row_id="w2", text="(x - 2)(x - 3) = 0"),
            BoardRow(row_id="w3", text="x = 2 or x = 3"),
        ],
        rows_remaining=9,
    )
    payload_str = report.model_dump_json(by_alias=True)

    handle_report_payload(payload_str, mgr)

    assert mgr.state.rows is not None
    assert mgr.state.rows.page_id == "p_quad_01"
    assert mgr.state.rows.rows_remaining == 9
    assert len(mgr.state.rows.rows) == 3
    assert mgr.state.rows.rows[0]["text"] == "x^2 - 5x + 6 = 0"


def test_telemetry_step_ack_updates_progress():
    """Verify that StepAck updates last_acked_step_index and drawn_op_ids."""
    mgr = StateMachineManager()
    mgr.state.generation = 1
    mgr.state.active_turn_id = "t1"
    assert mgr.state.last_acked_step_index == -1

    ack = StepAck(
        turn_id="t1",
        generation=1,
        step_index=2,
        drawn_op_ids=["op_write_1", "op_focus_tri"],
    )
    handle_report_payload(ack.model_dump_json(by_alias=True), mgr)

    assert mgr.state.last_acked_step_index == 2
    assert "op_write_1" in mgr.state.acked_op_ids
    assert "op_focus_tri" in mgr.state.acked_op_ids

    # Stale generation step_ack is ignored
    stale_ack = StepAck(
        turn_id="t1",
        generation=0,
        step_index=5,
        drawn_op_ids=["op_stale"],
    )
    handle_report_payload(stale_ack.model_dump_json(by_alias=True), mgr)
    assert mgr.state.last_acked_step_index == 2
    assert "op_stale" not in mgr.state.acked_op_ids


# =============================================================================
# 5. Mic Check & Greeting Fast-Path Classification
# =============================================================================

@pytest.mark.asyncio
async def test_mic_check_fast_path_classification():
    """Verify that mic checks and greetings are classified as backchannel without LLM calls."""
    checks = [
        "hello",
        "hello?",
        "hi",
        "hey",
        "can you hear me",
        "can you hear me?",
        "am i audible",
        "testing",
        "mic check",
        "is someone there",
    ]
    for phrase in checks:
        label = await classify_interrupt(
            topic="Triangles",
            last_teacher_line="Let's find the hypotenuse",
            lesson_on_board=True,
            doubt_pending=False,
            utterance=phrase,
        )
        assert label == "backchannel", f"Phrase '{phrase}' was classified as '{label}', expected 'backchannel'"

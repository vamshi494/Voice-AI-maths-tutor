# backend/tests/test_contracts.py
import pytest
from pydantic import ValidationError
from app.contracts.base import CamelModel
from app.contracts.turn_plan import (
    TurnPlan,
    Quantity,
    Unknown,
    QualitativeClaim,
    PlanIssue,
    NUMERIC_CONTRADICTION_CODES,
)
from app.contracts.problem_ir import (
    ProblemIR,
    ProblemFact,
    Evidence,
    ProblemEntity,
    ProblemExpression,
    NumberNode,
    BinaryNode,
    EquationConstraint,
    EvaluateReq,
    ResultBinding,
)
from app.contracts.diagram import (
    VerifiedDiagram,
    DiagramCommand,
    DiagramAnchor,
    has_drawable_ink,
)
from app.contracts.board_ops import BoardOp, Step, DoubtMark
from app.contracts.messages import (
    TurnStarted,
    DiagramCommit,
    StepEvt,
    StepAck,
    RpcSubmitQuestion,
    RpcSubmitDoubt,
)
from app.contracts.classifier import InterruptDecision, FigureNeedDecision
from app.contracts.agent_state import AgentState, ConvState, PausedLesson


def test_camel_model_alias_and_extra_forbid():
    class TestModel(CamelModel):
        first_name: str
        item_count: int

    # Can parse camelCase from wire
    m = TestModel.model_validate({"firstName": "Alice", "itemCount": 5})
    assert m.first_name == "Alice"
    assert m.item_count == 5

    # Can serialize to camelCase
    dumped = m.model_dump(by_alias=True)
    assert dumped == {"firstName": "Alice", "itemCount": 5}

    # Can also populate by python name
    m2 = TestModel(first_name="Bob", item_count=10)
    assert m2.model_dump(by_alias=True) == {"firstName": "Bob", "itemCount": 10}

    # extra='forbid' raises on extra keys
    with pytest.raises(ValidationError):
        TestModel.model_validate({"firstName": "Eve", "itemCount": 1, "extraKey": True})


def test_turn_plan_finite_validator():
    # Valid quantity
    q = Quantity(
        id="q1",
        symbol="x",
        value=10.5,
        provenance="given",
    )
    assert q.value == 10.5

    # Infinite value raises ValueError
    with pytest.raises(ValidationError) as exc:
        Quantity(
            id="q2",
            symbol="inf",
            value=float("inf"),
            provenance="given",
        )
    assert "non_finite" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        Quantity(
            id="q3",
            symbol="nan",
            value=float("nan"),
            provenance="given",
        )
    assert "non_finite" in str(exc.value)


def test_turn_plan_serialization():
    plan_data = {
        "schemaVersion": "turn-plan/v3",
        "question": "Find the area of a circle with radius 7",
        "givens": [
            {
                "id": "q1",
                "symbol": "r",
                "value": 7.0,
                "unit": "cm",
                "provenance": "given",
                "dependsOn": [],
            }
        ],
        "unknowns": [
            {"id": "u1", "symbol": "A", "unit": "cm^2"}
        ],
        "derived": [
            {
                "id": "q2",
                "symbol": "A",
                "value": 154.0,
                "unit": "cm^2",
                "provenance": "derived",
                "sourceText": "pi * r^2 = 154",
                "dependsOn": ["q1"],
            }
        ],
        "qualitativeClaims": [
            {
                "id": "c1",
                "claim": "area_is_positive",
                "expected": True,
                "relatedQuantityIds": ["q2"],
                "relatedEntityHints": [],
            }
        ],
        "lawIds": ["area_of_circle"],
        "assumptions": ["standard Euclidean plane"],
        "visualRequirement": "optional",
        "teachingSequenceHints": ["state formula", "substitute r", "calculate result"],
    }
    plan = TurnPlan.model_validate(plan_data)
    assert plan.schema_version == "turn-plan/v3"
    assert len(plan.givens) == 1
    assert plan.derived[0].source_text == "pi * r^2 = 154"
    assert plan.model_dump(by_alias=True, exclude_none=True) == plan_data


def test_problem_ir_discriminator():
    ir_data = {
        "schemaVersion": "problem-ir/v1",
        "id": "prob1",
        "question": "Find x if 2x = 6",
        "facts": [
            {
                "id": "f1",
                "kind": "given",
                "statement": "2x = 6",
                "evidence": {"source": "question", "start": 10, "end": 16, "quote": "2x = 6"},
            }
        ],
        "entities": [
            {"id": "e1", "kind": "line", "evidenceFactIds": ["f1"]}
        ],
        "expressions": [
            {
                "id": "expr1",
                "valueType": "scalar",
                "root": {
                    "kind": "binary",
                    "operator": "*",
                    "left": {"kind": "number", "value": 2.0},
                    "right": {"kind": "variable", "name": "x"},
                },
                "evidenceFactIds": ["f1"],
            }
        ],
        "constraints": [
            {
                "id": "c1",
                "kind": "equation",
                "leftExpressionId": "expr1",
                "rightExpressionId": "expr1",
                "evidenceFactIds": ["f1"],
            }
        ],
        "representationIntents": [
            {
                "id": "intent1",
                "kind": "geometric_figure",
                "entityIds": ["e1"],
                "evidenceFactIds": ["f1"],
            }
        ],
        "solveRequests": [
            {
                "id": "sr1",
                "kind": "evaluate",
                "expressionId": "expr1",
                "resultBinding": {
                    "turnPlanQuantityId": "u1",
                    "symbol": "x",
                    "evidenceFactIds": ["f1"],
                },
            }
        ],
    }
    ir = ProblemIR.model_validate(ir_data)
    assert ir.schema_version == "problem-ir/v1"
    assert ir.expressions[0].root.kind == "binary"
    assert isinstance(ir.expressions[0].root, BinaryNode)
    assert ir.solve_requests[0].kind == "evaluate"
    assert isinstance(ir.solve_requests[0], EvaluateReq)


def test_verified_diagram_and_drawable_ink():
    diag_no_ink = VerifiedDiagram(
        name="empty_figure",
        commands=[DiagramCommand(type="LABEL", params=[100, 100, 14], text="Note")],
        anchors=[],
        reveals=[],
        prompt_addon="empty",
    )
    assert not has_drawable_ink(diag_no_ink)

    diag_with_ink = VerifiedDiagram(
        name="triangle_figure",
        commands=[
            DiagramCommand(type="DRAW_LINE", params=[450, 100, 600, 300]),
            DiagramCommand(type="LABEL", params=[450, 90, 14], text="A"),
        ],
        anchors=[DiagramAnchor(id="seg_AB", labels=["side AB"], x=450, y=100, width=150, height=200)],
        reveals=[],
        prompt_addon="figure: triangle_figure",
    )
    assert has_drawable_ink(diag_with_ink)


def test_board_ops_and_steps():
    op = BoardOp(
        op_id="turn_1:0:0",
        kind="WRITE",
        at_word=0,
        text="Given: r = 7 cm",
        row_id="w1",
    )
    step = Step(
        turn_id="turn_1",
        generation=1,
        step_index=0,
        spoken_text="First we note that the radius is seven centimetres.",
        words=["first", "we", "note", "that", "the", "radius", "is", "seven", "centimetres"],
        ops=[op],
    )
    assert step.ops[0].row_id == "w1"
    dumped = step.model_dump(by_alias=True)
    assert dumped["turnId"] == "turn_1"
    assert dumped["ops"][0]["opId"] == "turn_1:0:0"


def test_agent_state_defaults():
    st = AgentState(
        session_id="sess_1",
        user_id="user_1",
        board_id="board_1",
    )
    assert st.conv_state == ConvState.IDLE
    assert st.generation == 0
    assert st.tts_speed == 1.0
    assert st.active_turn_id is None


def test_paused_lesson_minimal_doc_validates():
    pl = PausedLesson.model_validate({"board_id": "b", "page_id": "p", "lesson_question": "q"})
    assert pl.turn_plan is None
    assert pl.figure_drawn is False
    assert pl.lesson_turn_id == ""
    assert pl.last_acked_step_index == -1
    assert pl.heard_steps_text == []


def test_client_reports_parse():
    """Every report shape the browser can send parses. New shapes append one fixture."""
    import json
    from pathlib import Path
    from app.contracts.messages import BoardReport, PageTurned, ResyncRequest, StepProgress

    mapping = {"step_ack": StepAck, "step_progress": StepProgress, "board_report": BoardReport,
               "page_turned": PageTurned, "resync_request": ResyncRequest}
    fixture = Path(__file__).resolve().parent / "fixtures" / "client_reports.json"
    reports = json.loads(fixture.read_text())
    assert reports, "the client_reports fixture must hold one instance per report shape"
    for entry in reports:
        model = mapping.get(entry["type"])
        assert model is not None, f"unmapped report type {entry['type']}"
        parsed = model.model_validate(entry)
        assert parsed.type == entry["type"]


def test_every_reveal_has_target_id():
    """DiagramReveal.target_id is required and serialized as targetId;
    every reveal the builder produces must carry one."""
    from app.contracts.diagram import DiagramReveal

    reveals = [
        DiagramReveal(target_id="rg_a", command_indices=[0]),
        DiagramReveal(target_id="rg_b", command_indices=[1], narration="group b"),
    ]
    diagram = VerifiedDiagram(
        name="x",
        commands=[DiagramCommand(type="DRAW_POINT", params=[0.0, 0.0])],
        anchors=[],
        reveals=reveals,
        prompt_addon="",
    )
    assert all(r.target_id for r in diagram.reveals)
    wire = diagram.model_dump_json(by_alias=True, exclude_none=True)
    assert '"targetId":"rg_a"' in wire
    assert '"targetId":"rg_b"' in wire


def test_page_commit_shapes():
    """Every committed page_commits.json entry and the wire_events.json page_commit
    instance validate as PageCommit (CamelModel, extra=forbid) with the expected shapes."""
    import json
    from pathlib import Path
    from app.contracts.messages import Block, PageCommit

    fixture_dir = Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures"
    commits = json.loads((fixture_dir / "page_commits.json").read_text())
    assert len(commits) == 7, "one case per fixture row"
    for entry in commits:
        pc = PageCommit.model_validate(entry)
        assert pc.type == "page_commit"
        assert pc.page_id and pc.commit_id
        for b in pc.blocks:
            assert {"x", "y", "width", "height"} <= set(b.rect.model_dump(by_alias=True))
            if b.role == "figure":
                assert b.commands and b.anchors, "figure blocks carry the projected diagram"
            else:
                assert b.text_lines, "table/text blocks carry textLines"

    wire = json.loads((fixture_dir / "wire_events.json").read_text())
    page_commits = [e for e in wire if e["type"] == "page_commit"]
    assert page_commits, "wire_events.json must hold one page_commit instance"
    for entry in page_commits:
        PageCommit.model_validate(entry)

    # camelCase serialization round-trips
    pc = PageCommit.model_validate(commits[0])
    dumped = json.loads(pc.model_dump_json(by_alias=True, exclude_none=True))
    assert "workRect" in dumped and "commitId" in dumped and "blocks" in dumped

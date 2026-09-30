# backend/tests/test_persistence_models.py
import pytest
from pydantic import ValidationError
from app.persistence.models import Board, Turn, Segment, SceneArtifacts
from app.contracts.turn_plan import TurnPlan
from app.contracts.diagram import VerifiedDiagram


def test_scene_artifacts_validation():
    art_dict = {
        "kind": "lesson",
        "pageId": "p_root",
        "continuesBoard": False,
        "status": "complete",
        "turnPlan": None,
        "solverProjection": None,
        "verifiedDiagram": None,
        "pausedNote": None,
    }
    art = SceneArtifacts.model_validate(art_dict)
    assert art.kind == "lesson"
    assert art.page_id == "p_root"
    assert art.status == "complete"

    # Invalid kind raises ValidationError
    with pytest.raises(ValidationError):
        SceneArtifacts.model_validate({**art_dict, "kind": "invalid_kind"})


def test_turn_model_artifact_validation_on_read_write():
    turn = Turn(
        id="a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
        board_id="b1",
        user_id="u1",
        order_index=0,
        question="What is 2+2?",
        raw_response="[STEP]It is 4.[/STEP]",
    )

    art = SceneArtifacts(
        kind="lesson",
        page_id="p1",
        continues_board=False,
        status="complete",
        turn_plan=None,
        solver_projection={"q1": "4"},
        verified_diagram=None,
    )

    turn.validate_and_set_artifacts(art)
    assert turn.scene_artifacts is not None
    assert turn.scene_artifacts["pageId"] == "p1"
    assert turn.scene_artifacts["solverProjection"] == {"q1": "4"}

    # Read back and validate
    read_art = turn.get_validated_artifacts()
    assert isinstance(read_art, SceneArtifacts)
    assert read_art.page_id == "p1"
    assert read_art.status == "complete"

    # Corrupt artifact in database causes validation failure on read
    turn.scene_artifacts = {"kind": "unsupported_kind"}
    with pytest.raises(ValidationError):
        turn.get_validated_artifacts()

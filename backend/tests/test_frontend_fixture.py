# backend/tests/test_frontend_fixture.py
"""The frontend renders fixtures/diagram_commits.json in its contract test. This test fails when
the backend's wire shape drifts from that committed fixture, so neither side can change the
DiagramCommit contract without the other noticing. Regenerate with make_frontend_fixture.py."""
import json
from pathlib import Path

from tests.make_frontend_fixture import build

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures/diagram_commits.json"


def _shape(events: list[dict]) -> list:
    return [
        (e["type"], sorted(e["diagram"]),
         [(c["type"], len(c["params"]), "text" in c) for c in e["diagram"]["commands"]],
         [sorted(a) for a in e["diagram"]["anchors"]])
        for e in events
    ]


def test_committed_frontend_fixture_matches_backend_output():
    assert FIXTURE.exists(), "run backend/tests/make_frontend_fixture.py"
    assert _shape(json.loads(FIXTURE.read_text())) == _shape(build())

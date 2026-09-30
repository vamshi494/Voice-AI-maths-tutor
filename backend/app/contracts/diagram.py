# app/contracts/diagram.py
from typing import Literal
from .base import CamelModel

DrawCommandType = Literal[   # render vocabulary for the scene engine
    "DRAW_POINT", "DRAW_LINE", "DRAW_RAY", "DRAW_POLYLINE", "DRAW_CIRCLE", "DRAW_ARC",
    "DRAW_ANGLE_MARK", "DRAW_RIGHT_ANGLE_MARK", "DRAW_TICK", "DRAW_DIMENSION",
    "DRAW_AXES", "DRAW_CURVE", "DRAW_NUMBER_LINE", "LABEL",
]


class VisualStyle(CamelModel):
    stroke_role: Literal["primary", "construction", "trace"] | None = None
    stroke_width: float | None = None
    dashed: bool | None = None
    fill_role: Literal["region"] | None = None
    corresponding_family: Literal[1, 2, 3] | None = None


class SemanticRef(CamelModel):
    entity_id: str | None = None
    primitive_id: str | None = None
    action_id: str | None = None


class DiagramCommand(CamelModel):
    type: DrawCommandType
    params: list[float]           # board logical coords (1200x700), per command type
    text: str | None = None
    anchor_id: str | None = None
    visual_style: VisualStyle | None = None
    semantic_ref: SemanticRef | None = None


class DiagramAnchor(CamelModel):
    id: str                        # entity id — the ONLY ids FOCUS may use
    labels: list[str]
    x: float
    y: float
    width: float
    height: float


class DiagramGroup(CamelModel):
    id: str
    entity_ids: list[str]


class DiagramReveal(CamelModel):
    narration: str = ""            # optional; the client RevealGroup.narration is optional too
    command_indices: list[int]
    kind: Literal["reveal", "focus", "annotate"] | None = None
    target_id: str                 # required (matches the TS RevealGroup.targetId)


class DeferredAnnotation(CamelModel):
    entity_id: str                 # ANNOTATE target id
    commands: list[DiagramCommand]


class LabelFact(CamelModel):
    symbol: str
    title: str
    value: str | None = None
    provenance: Literal["given", "derived", "assumed"] | None = None
    detail: str | None = None


class VerifiedDiagram(CamelModel):
    id: Literal["verified_scene"] = "verified_scene"
    name: str
    commands: list[DiagramCommand]
    anchors: list[DiagramAnchor]
    reveals: list[DiagramReveal]
    prompt_addon: str
    groups: list[DiagramGroup] | None = None
    caption: str | None = None
    deferred_annotations: list[DeferredAnnotation] | None = None
    label_glossary: dict[str, LabelFact] | None = None
    alias_map: dict[str, str] | None = None    # LLM id / entity label / reversed segment name -> canonical id
    namespace: str = ""            # "" for lesson figures, "d1_" etc. for doubt figures


# Commands that form actual visible geometry on the board — not just dots or labels
_SHAPE_FORMING_COMMANDS: frozenset[str] = frozenset({
    "DRAW_LINE", "DRAW_RAY", "DRAW_POLYLINE", "DRAW_CIRCLE", "DRAW_ARC",
    "DRAW_CURVE", "DRAW_AXES", "DRAW_NUMBER_LINE", "DRAW_DIMENSION",
    "DRAW_ANGLE_MARK", "DRAW_RIGHT_ANGLE_MARK", "DRAW_TICK",
})


def has_drawable_ink(d: VerifiedDiagram) -> bool:   # gate: the diagram must draw visible geometry
    """A diagram qualifies as drawable when it contains at least one shape-forming
    command (segment, polygon, circle, arc, curve, axes, etc.) AND has anchors.

    DRAW_POINT alone is NOT sufficient — 3 isolated dots do not constitute a
    triangle or any meaningful geometric figure. This forces the repair loop to
    re-emit proper polygon/segment constructions when the LLM produces only points.
    """
    has_shape = any(c.type in _SHAPE_FORMING_COMMANDS for c in d.commands)
    return has_shape and len(d.anchors) > 0

# app/scene_engine/project.py
"""World geometry -> board logical coordinates (1200x700) and DiagramCommands.

The projection keeps figures inside the diagram zone and clear of the student's working:
* The diagram zone matches the client layout: sections 2+3 of the board are x 420..1140
  (boardLayout.ts), so figures never cross the chalk divider at x=395.
* Lines and rays are clipped to the diagram zone (Liang-Barsky), so they stop at the zone
  edge rather than crossing the board and the student's handwritten working.
* Arc sweeps are computed in SCREEN space: the world has y up, the canvas has y down.
* Right-angle marks carry both arm directions [vx, vy, size, dir1, dir2], so the renderer
  always draws the square on the correct side of the angle.
* World bounding box covers every entity type (axes, curves, number lines, dimensions);
  a figure containing only a number line still gets a real box instead of a 0..100 fallback.
* Vertex letters come from the scene entity label OR the operator-provided Point.label,
  so a triangle built by triangle_sss is lettered even if the LLM declared no entities.
"""
import math
from typing import Any

from app.contracts.diagram import DiagramAnchor, DiagramCommand, SemanticRef, VisualStyle
from app.observability import log_event
from app.scene_engine.compile import RenderScene
from app.scene_engine.layout import Rect
from app.scene_engine.operators import (
    AngleMark, Arc, Axes, Circle, Curve, Dimension, Label, Line, NumberLine, Point, Polygon, Ray,
    Segment, TickMark,
)

ZONE_X_MIN = 420.0
ZONE_X_MAX = 1140.0
ZONE_Y_MIN = 40.0
ZONE_Y_MAX = 640.0
ZONE_PADDING = 24.0
LABEL_FONT = 18.0

TARGET_W = (ZONE_X_MAX - ZONE_X_MIN) - 2 * ZONE_PADDING
TARGET_H = (ZONE_Y_MAX - ZONE_Y_MIN) - 2 * ZONE_PADDING


# The default figure region (right of the work column), matching the ZONE_* constants above.
FIGURE_RECT_DEFAULT = Rect(x=ZONE_X_MIN, y=ZONE_Y_MIN, width=ZONE_X_MAX - ZONE_X_MIN, height=ZONE_Y_MAX - ZONE_Y_MIN)


class BoundingBox:
    def __init__(self) -> None:
        self.min_x = float("inf")
        self.max_x = float("-inf")
        self.min_y = float("inf")
        self.max_y = float("-inf")

    def add_point(self, x: float, y: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        self.min_x = min(self.min_x, x)
        self.max_x = max(self.max_x, x)
        self.min_y = min(self.min_y, y)
        self.max_y = max(self.max_y, y)

    @property
    def empty(self) -> bool:
        return self.min_x == float("inf")

    @property
    def width(self) -> float:
        return max(1e-6, self.max_x - self.min_x)

    @property
    def height(self) -> float:
        return max(1e-6, self.max_y - self.min_y)

    @property
    def center_x(self) -> float:
        return (self.min_x + self.max_x) / 2.0

    @property
    def center_y(self) -> float:
        return (self.min_y + self.max_y) / 2.0


class Projector:
    def __init__(self, world_bbox: BoundingBox, target: Rect = FIGURE_RECT_DEFAULT,
                 padding: float = ZONE_PADDING) -> None:
        # Degenerate extents (a single horizontal number line) get a sane minimum span so
        # the uniform scale is driven by the real extent, not by 1e-6.
        w = world_bbox.width
        h = world_bbox.height
        span = max(w, h)
        w = max(w, span * 0.25)
        h = max(h, span * 0.25)
        cx, cy = world_bbox.center_x, world_bbox.center_y
        pad = 0.08
        self.w_min_x, self.w_max_x = cx - w * (0.5 + pad), cx + w * (0.5 + pad)
        self.w_min_y, self.w_max_y = cy - h * (0.5 + pad), cy + h * (0.5 + pad)
        target_w = target.width - 2 * padding
        target_h = target.height - 2 * padding
        self.scale = min(target_w / (self.w_max_x - self.w_min_x), target_h / (self.w_max_y - self.w_min_y))
        self.zone_cx = target.x + target.width / 2.0
        self.zone_cy = target.y + target.height / 2.0
        self.w_cx, self.w_cy = cx, cy

    def transform(self, wx: float, wy: float) -> tuple[float, float]:
        """World y points up; board y points down."""
        lx = self.zone_cx + self.scale * (wx - self.w_cx)
        ly = self.zone_cy - self.scale * (wy - self.w_cy)
        return round(lx, 2), round(ly, 2)

    def transform_length(self, length: float) -> float:
        return round(length * self.scale, 2)


def clip_segment(x1: float, y1: float, x2: float, y2: float,
                 t0: float = 0.0, t1: float = 1.0,
                 target: Rect = FIGURE_RECT_DEFAULT) -> tuple[float, float, float, float] | None:
    """Liang-Barsky clip of the parametric segment P(t)=P1+t(P2-P1), t in [t0,t1], to the zone."""
    x_min, y_min = target.x, target.y
    x_max, y_max = target.x + target.width, target.y + target.height
    dx, dy = x2 - x1, y2 - y1
    for p, q in ((-dx, x1 - x_min), (dx, x_max - x1), (-dy, y1 - y_min), (dy, y_max - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    return (round(x1 + t0 * dx, 2), round(y1 + t0 * dy, 2), round(x1 + t1 * dx, 2), round(y1 + t1 * dy, 2))


def _screen_angle(cx: float, cy: float, px: float, py: float) -> float:
    return math.degrees(math.atan2(py - cy, px - cx)) % 360.0


def compute_world_bbox(entities: dict[str, Any]) -> BoundingBox:
    bbox = BoundingBox()
    for ent in entities.values():
        if isinstance(ent, Point):
            bbox.add_point(ent.x, ent.y)
        elif isinstance(ent, (Segment, Line, Dimension)):
            bbox.add_point(ent.p1.x, ent.p1.y)
            bbox.add_point(ent.p2.x, ent.p2.y)
        elif isinstance(ent, Ray):
            bbox.add_point(ent.origin.x, ent.origin.y)
            bbox.add_point(ent.through.x, ent.through.y)
        elif isinstance(ent, Polygon):
            for v in ent.vertices:
                bbox.add_point(v.x, v.y)
        elif isinstance(ent, (Circle, Arc)):
            bbox.add_point(ent.center.x - ent.radius, ent.center.y - ent.radius)
            bbox.add_point(ent.center.x + ent.radius, ent.center.y + ent.radius)
        elif isinstance(ent, Axes):
            bbox.add_point(ent.x_min, ent.y_min)
            bbox.add_point(ent.x_max, ent.y_max)
        elif isinstance(ent, Curve):
            for x, y in ent.points:
                bbox.add_point(x, y)
        elif isinstance(ent, NumberLine):
            bbox.add_point(ent.min_val, 0.0)
            bbox.add_point(ent.max_val, 0.0)
        elif isinstance(ent, AngleMark):
            bbox.add_point(ent.vertex.x, ent.vertex.y)
    if bbox.empty:
        bbox.add_point(0.0, 0.0)
        bbox.add_point(100.0, 100.0)
    return bbox


def _clamp_to_zone(x: float, y: float, target: Rect = FIGURE_RECT_DEFAULT) -> tuple[float, float]:
    return (min(max(x, target.x + 4), target.x + target.width - 16),
            min(max(y, target.y + 4), target.y + target.height - 4))


def nudge_label(x: float, y: float, placed: list[dict[str, Any]],
                target: Rect = FIGURE_RECT_DEFAULT) -> tuple[float, float]:
    """Resolve label overlap by trying the 8 compass nudges around the preferred spot."""
    directions = [(0, -12), (12, -12), (12, 0), (12, 12), (0, 12), (-12, 12), (-12, 0), (-12, -12)]
    cur_x, cur_y = x, y
    for i in range(len(directions) + 1):
        if not any(math.hypot(cur_x - p["x"], cur_y - p["y"]) < 18.0 for p in placed):
            return _clamp_to_zone(round(cur_x, 2), round(cur_y, 2), target)
        if i < len(directions):
            dx, dy = directions[i]
            cur_x, cur_y = x + dx, y + dy
    log_event("label_overlap", x=cur_x, y=cur_y)
    return _clamp_to_zone(round(cur_x, 2), round(cur_y, 2), target)


def project_scene_to_commands(
    scene: RenderScene, namespace: str = "", target: Rect = FIGURE_RECT_DEFAULT,
) -> tuple[list[DiagramCommand], list[DiagramAnchor], float]:
    """Project RenderScene into diagram-zone commands and anchors.

    Returns `(commands, anchors, preferred_aspect)` where `preferred_aspect =
    world_bbox.width / world_bbox.height` (feeds the layout engine's BlockSpec).
    """
    entities = scene.entities
    if not entities:
        return [], [], 1.0

    world_bbox = compute_world_bbox(entities)
    preferred_aspect = world_bbox.width / world_bbox.height
    proj = Projector(world_bbox, target)
    commands: list[DiagramCommand] = []
    bounds_by_id: dict[str, BoundingBox] = {}
    placed_labels: list[dict[str, Any]] = []

    poly_centroid_screen: dict[str, tuple[float, float]] = {}
    for ent in entities.values():
        if isinstance(ent, Polygon) and ent.vertices:
            cx = sum(v.x for v in ent.vertices) / len(ent.vertices)
            cy = sum(v.y for v in ent.vertices) / len(ent.vertices)
            poly_centroid_screen[ent.id] = proj.transform(cx, cy)

    def emit(cmd_type: str, params: list[float], ent_id: str, text: str | None = None,
             style: VisualStyle | None = None) -> None:
        commands.append(DiagramCommand(type=cmd_type, params=[round(float(p), 2) for p in params],
                                       text=text, anchor_id=ent_id, visual_style=style,
                                       semantic_ref=SemanticRef(entity_id=ent_id)))

    for ent_id, ent in entities.items():
        pid = f"{namespace}{ent_id}"
        b = BoundingBox()

        if isinstance(ent, Point):
            lx, ly = proj.transform(ent.x, ent.y)
            emit("DRAW_POINT", [lx, ly, 3.5], pid)
            b.add_point(lx - 4, ly - 4)
            b.add_point(lx + 4, ly + 4)

        elif isinstance(ent, Segment):
            x1, y1 = proj.transform(ent.p1.x, ent.p1.y)
            x2, y2 = proj.transform(ent.p2.x, ent.p2.y)
            emit("DRAW_LINE", [x1, y1, x2, y2], pid)
            b.add_point(x1, y1)
            b.add_point(x2, y2)

        elif isinstance(ent, (Line, Ray)):
            if isinstance(ent, Ray):
                ox, oy = proj.transform(ent.origin.x, ent.origin.y)
                tx, ty = proj.transform(ent.through.x, ent.through.y)
                clipped = clip_segment(ox, oy, tx, ty, 0.0, 1e6, target=target)
                cmd_type = "DRAW_RAY"
            else:
                ox, oy = proj.transform(ent.p1.x, ent.p1.y)
                tx, ty = proj.transform(ent.p2.x, ent.p2.y)
                clipped = clip_segment(ox, oy, tx, ty, -1e6, 1e6, target=target)
                cmd_type = "DRAW_LINE"
            if clipped is None:
                log_event("line_outside_zone", entity_id=pid)
                continue
            emit(cmd_type, list(clipped), pid)
            b.add_point(clipped[0], clipped[1])
            b.add_point(clipped[2], clipped[3])

        elif isinstance(ent, Polygon):
            coords: list[float] = []
            for v in ent.vertices:
                vx, vy = proj.transform(v.x, v.y)
                coords.extend([vx, vy])
                b.add_point(vx, vy)
            coords.extend([coords[0], coords[1]])      # closed
            emit("DRAW_POLYLINE", coords, pid)

        elif isinstance(ent, Circle):
            cx, cy = proj.transform(ent.center.x, ent.center.y)
            r = proj.transform_length(ent.radius)
            emit("DRAW_CIRCLE", [cx, cy, r], pid)
            b.add_point(cx - r, cy - r)
            b.add_point(cx + r, cy + r)

        elif isinstance(ent, Arc):
            cx, cy = proj.transform(ent.center.x, ent.center.y)
            r = proj.transform_length(ent.radius)
            # World CCW sweep s->e becomes, after the y flip, the screen range (-e)->(-s).
            start = (-ent.end_deg) % 360.0
            end = (-ent.start_deg) % 360.0
            emit("DRAW_ARC", [cx, cy, r, start, end], pid)
            b.add_point(cx - r, cy - r)
            b.add_point(cx + r, cy + r)

        elif isinstance(ent, AngleMark):
            vx, vy = proj.transform(ent.vertex.x, ent.vertex.y)
            p1x, p1y = proj.transform(ent.p1.x, ent.p1.y)
            p2x, p2y = proj.transform(ent.p2.x, ent.p2.y)
            a1 = _screen_angle(vx, vy, p1x, p1y)
            a2 = _screen_angle(vx, vy, p2x, p2y)
            if ent.is_right_angle:
                emit("DRAW_RIGHT_ANGLE_MARK", [vx, vy, 14.0, a1, a2], pid)
            else:
                emit("DRAW_ANGLE_MARK", [vx, vy, 22.0, a1, a2], pid)
            b.add_point(vx - 22, vy - 22)
            b.add_point(vx + 22, vy + 22)

        elif isinstance(ent, TickMark):
            p1x, p1y = proj.transform(ent.segment.p1.x, ent.segment.p1.y)
            p2x, p2y = proj.transform(ent.segment.p2.x, ent.segment.p2.y)
            mx, my = (p1x + p2x) / 2.0, (p1y + p2y) / 2.0
            seg_dir = math.degrees(math.atan2(p2y - p1y, p2x - p1x)) % 360.0
            emit("DRAW_TICK", [mx, my, seg_dir, float(max(1, min(3, ent.count)))], pid)
            b.add_point(mx - 8, my - 8)
            b.add_point(mx + 8, my + 8)

        elif isinstance(ent, Dimension):
            x1, y1 = proj.transform(ent.p1.x, ent.p1.y)
            x2, y2 = proj.transform(ent.p2.x, ent.p2.y)
            emit("DRAW_DIMENSION", [x1, y1, x2, y2, ent.offset], pid, text=ent.text)
            b.add_point(x1, y1)
            b.add_point(x2, y2)

        elif isinstance(ent, Axes):
            x0, y0 = proj.transform(ent.x_min, ent.y_min)
            x1, y1 = proj.transform(ent.x_max, ent.y_max)
            ox_w = min(max(0.0, ent.x_min), ent.x_max)
            oy_w = min(max(0.0, ent.y_min), ent.y_max)
            ox, oy = proj.transform(ox_w, oy_w)
            emit("DRAW_AXES", [x0, y0, x1, y1, ox, oy, proj.transform_length(1.0)], pid)
            b.add_point(x0, y0)
            b.add_point(x1, y1)

        elif isinstance(ent, Curve):
            pts: list[float] = []
            for wx, wy in ent.points:
                lx, ly = proj.transform(wx, wy)
                if target.y - 50 <= ly <= target.y + target.height + 50:
                    pts.extend([lx, ly])
                    b.add_point(lx, ly)
            if len(pts) >= 4:
                emit("DRAW_CURVE", pts, pid)

        elif isinstance(ent, NumberLine):
            x1, y = proj.transform(ent.min_val, 0.0)
            x2, _ = proj.transform(ent.max_val, 0.0)
            emit("DRAW_NUMBER_LINE", [x1, y, x2, ent.tick_step * proj.scale, ent.min_val, ent.tick_step], pid)
            for m in ent.marks:
                mx, _ = proj.transform(m, 0.0)
                emit("DRAW_POINT", [mx, y, 4.5], pid)
                emit("LABEL", [mx - 8, y - 32, LABEL_FONT], pid, text=f"{m:g}")
            b.add_point(x1, y - 20)
            b.add_point(x2, y + 20)

        elif isinstance(ent, Label):
            label_target = entities.get(ent.target_id)
            if isinstance(label_target, Point):
                lx, ly = proj.transform(label_target.x, label_target.y)
            else:
                lx, ly = proj.transform(ent.x, ent.y)
            nx, ny = nudge_label(lx + 6, ly - 22, placed_labels, target)
            placed_labels.append({"x": nx, "y": ny})
            emit("LABEL", [nx, ny, LABEL_FONT], pid, text=ent.text)
            b.add_point(nx, ny)
            b.add_point(nx + 10 * max(1, len(ent.text)), ny + LABEL_FONT)

        if not b.empty:
            bounds_by_id[pid] = b

    # ---- Entity name labels (vertex letters, side names) ---------------------------------
    declared_labels: dict[str, str] = {}
    if scene.doc:
        for s_ent in scene.doc.entities:
            if s_ent.label:
                declared_labels[s_ent.id] = s_ent.label
    # A Label entity attached to a point is drawn in the loop above; skip the point's own
    # name label here so every vertex letter does not appear twice, ~20 px apart.
    already_labelled = {(e.target_id, (e.text or "").strip().lower())
                        for e in entities.values() if isinstance(e, Label) and e.target_id}
    for ent_id, ent in entities.items():
        text = declared_labels.get(ent_id)
        if text is None and isinstance(ent, Point):
            text = ent.label
        if not text:
            continue
        if (ent_id, text.strip().lower()) in already_labelled:
            continue
        pid = f"{namespace}{ent_id}"
        if isinstance(ent, Point):
            lx, ly = proj.transform(ent.x, ent.y)
            dx, dy = 0.0, -1.0
            for poly_id, (pcx, pcy) in poly_centroid_screen.items():
                poly = entities.get(poly_id)
                if isinstance(poly, Polygon) and any(v.id == ent.id for v in poly.vertices):
                    vx, vy = lx - pcx, ly - pcy
                    d = math.hypot(vx, vy)
                    if d > 1e-6:
                        dx, dy = vx / d, vy / d
                    break
            lbl_x, lbl_y = lx + dx * 16 - 5, ly + dy * 16 - LABEL_FONT / 2
        elif isinstance(ent, Segment):
            x1, y1 = proj.transform(ent.p1.x, ent.p1.y)
            x2, y2 = proj.transform(ent.p2.x, ent.p2.y)
            sl = math.hypot(x2 - x1, y2 - y1) or 1.0
            nx, ny = -(y2 - y1) / sl, (x2 - x1) / sl
            lbl_x, lbl_y = (x1 + x2) / 2 + nx * 14 - 5, (y1 + y2) / 2 + ny * 14 - LABEL_FONT / 2
        else:
            bb = bounds_by_id.get(pid)
            if bb is None:
                continue
            lbl_x, lbl_y = bb.center_x, bb.min_y - LABEL_FONT - 4
        lbl_x, lbl_y = nudge_label(lbl_x, lbl_y, placed_labels, target)
        placed_labels.append({"x": lbl_x, "y": lbl_y})
        emit("LABEL", [lbl_x, lbl_y, LABEL_FONT], pid, text=text)
        bb = bounds_by_id.setdefault(pid, BoundingBox())
        bb.add_point(lbl_x, lbl_y)
        bb.add_point(lbl_x + 10 * len(text), lbl_y + LABEL_FONT)

    anchors: list[DiagramAnchor] = []
    for eid, b in bounds_by_id.items():
        labels = [c.text for c in commands if c.type == "LABEL" and c.anchor_id == eid and c.text]
        anchors.append(DiagramAnchor(
            id=eid, labels=labels,
            x=round(b.min_x, 2), y=round(b.min_y, 2),
            width=round(max(10.0, b.width), 2), height=round(max(10.0, b.height), 2),
        ))
    return commands, anchors, preferred_aspect

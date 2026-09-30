# app/scene_engine/operators.py
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Point:
    id: str
    x: float
    y: float
    label: str | None = None      # vertex letter drawn on the board (e.g. "A")

    def distance_to(self, other: "Point") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass
class Segment:
    id: str
    p1: Point
    p2: Point

    @property
    def length(self) -> float:
        return self.p1.distance_to(self.p2)


@dataclass
class Line:
    id: str
    p1: Point
    p2: Point


@dataclass
class Ray:
    id: str
    origin: Point
    through: Point


@dataclass
class Polygon:
    id: str
    vertices: list[Point]


@dataclass
class Circle:
    id: str
    center: Point
    radius: float


@dataclass
class Arc:
    id: str
    center: Point
    radius: float
    start_deg: float
    end_deg: float


@dataclass
class AngleMark:
    id: str
    vertex: Point
    p1: Point
    p2: Point
    radius: float = 20.0
    is_right_angle: bool = False


@dataclass
class TickMark:
    id: str
    segment: Segment
    count: int = 1


@dataclass
class Dimension:
    id: str
    p1: Point
    p2: Point
    text: str | None = None
    offset: float = 20.0


@dataclass
class Axes:
    id: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    origin: Point = field(default_factory=lambda: Point("orig", 0.0, 0.0))


@dataclass
class Curve:
    id: str
    points: list[tuple[float, float]]


@dataclass
class NumberLine:
    id: str
    min_val: float
    max_val: float
    tick_step: float
    marks: list[float]


@dataclass
class Label:
    id: str
    target_id: str
    text: str
    x: float = 0.0
    y: float = 0.0
    font_size: float = 16.0


# Operator functions
def op_point(op_id: str, x: float, y: float) -> Point:
    return Point(id=op_id, x=float(x), y=float(y))


def op_point_polar(op_id: str, base: Point, length: float, angle_deg: float) -> Point:
    rad = math.radians(angle_deg)
    return Point(
        id=op_id,
        x=base.x + length * math.cos(rad),
        y=base.y + length * math.sin(rad),
    )


def op_point_on_segment(op_id: str, seg: Segment, ratio: float) -> Point:
    r = max(0.0, min(1.0, float(ratio)))
    return Point(
        id=op_id,
        x=seg.p1.x + r * (seg.p2.x - seg.p1.x),
        y=seg.p1.y + r * (seg.p2.y - seg.p1.y),
    )


def op_segment(op_id: str, p1: Point, p2: Point) -> Segment:
    if p1.distance_to(p2) < 1e-9:
        raise ValueError(f"Degenerate segment '{op_id}': endpoints are identical")
    return Segment(id=op_id, p1=p1, p2=p2)


def op_ray(op_id: str, origin: Point, through: Point) -> Ray:
    if origin.distance_to(through) < 1e-9:
        raise ValueError(f"Degenerate ray '{op_id}': points are identical")
    return Ray(id=op_id, origin=origin, through=through)


def op_line(op_id: str, p1: Point, p2: Point) -> Line:
    if p1.distance_to(p2) < 1e-9:
        raise ValueError(f"Degenerate line '{op_id}': points are identical")
    return Line(id=op_id, p1=p1, p2=p2)


def op_polygon(op_id: str, vertices: list[Point]) -> Polygon:
    if len(vertices) < 3:
        raise ValueError(f"Polygon '{op_id}' must have at least 3 vertices")
    return Polygon(id=op_id, vertices=vertices)


def triangle_ids(
    out_ids: list[str], labels: list[str] | None, construction_id: str | None = None
) -> tuple[list[str], str, list[str]]:
    """Resolve (point_ids, polygon_id, vertex_labels) for a triangle construction.

    The natural LLM form
    ``{"id": "tri1", "outputs": ["tri1"], "inputs": {..., "labels": ["A","B","C"]}}`` must
    resolve the labels to point ids rather than turn "tri1" into point A: otherwise every
    later reference to "B" fails with unresolved_reference. Rules:
      * 4+ outputs -> first three are the vertex point ids, fourth is the polygon id
      * 3 outputs  -> vertex point ids; polygon id = construction id
      * 0-2 outputs-> vertex point ids = labels; polygon id = first output or construction id
    """
    lbls = [str(x) for x in (labels or [])][:3]
    while len(lbls) < 3:
        lbls.append("ABC"[len(lbls)])
    outs = [str(o) for o in (out_ids or [])]
    if len(outs) >= 4:
        return outs[:3], outs[3], lbls
    if len(outs) == 3:
        return outs, construction_id or f"tri_{''.join(lbls)}", lbls
    poly = outs[0] if outs else (construction_id or f"tri_{''.join(lbls)}")
    if poly in lbls:                      # never let the polygon steal a vertex id
        poly = f"tri_{''.join(lbls)}"
    return lbls, poly, lbls


def _make_triangle(ids: list[str], poly_id: str, labels: list[str],
                   a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]):
    pa = Point(id=ids[0], x=a[0], y=a[1], label=labels[0])
    pb = Point(id=ids[1], x=b[0], y=b[1], label=labels[1])
    pc = Point(id=ids[2], x=c[0], y=c[1], label=labels[2])
    return pa, pb, pc, Polygon(id=poly_id, vertices=[pa, pb, pc])


def op_triangle_sss(
    out_ids: list[str], a: float, b: float, c: float, labels: list[str] | None = None,
    construction_id: str | None = None,
) -> tuple[Point, Point, Point, Polygon]:
    """Vertices V0,V1,V2 (= labels). a = V1V2 (opposite V0), b = V0V2, c = V0V1."""
    if min(a, b, c) <= 0:
        raise ValueError(f"Triangle sides must be positive: a={a}, b={b}, c={c}")
    if not (a + b > c and b + c > a and c + a > b):
        raise ValueError(f"Triangle inequality violated: a={a}, b={b}, c={c}")
    ids, poly, lbls = triangle_ids(out_ids, labels, construction_id)
    cos_a = max(-1.0, min(1.0, (b * b + c * c - a * a) / (2.0 * b * c)))
    sin_a = math.sqrt(max(0.0, 1.0 - cos_a * cos_a))
    return _make_triangle(ids, poly, lbls, (0.0, 0.0), (float(c), 0.0), (b * cos_a, b * sin_a))


def op_triangle_sas(
    out_ids: list[str], b: float, angle_deg: float, c: float, labels: list[str] | None = None,
    construction_id: str | None = None,
) -> tuple[Point, Point, Point, Polygon]:
    """Included angle at V0 (first label), between V0V1 (length c) and V0V2 (length b).
    For "AB = 5, BC = 12, angle B = 90" use labels ["B","A","C"], c = 5, b = 12."""
    if b <= 0 or c <= 0 or not (0 < angle_deg < 180):
        raise ValueError(f"Invalid SAS triangle: b={b}, angleDeg={angle_deg}, c={c}")
    ids, poly, lbls = triangle_ids(out_ids, labels, construction_id)
    rad = math.radians(angle_deg)
    return _make_triangle(ids, poly, lbls, (0.0, 0.0), (float(c), 0.0), (b * math.cos(rad), b * math.sin(rad)))


def op_triangle_asa(
    out_ids: list[str], angle_b_deg: float, a: float, angle_c_deg: float, labels: list[str] | None = None,
    construction_id: str | None = None,
) -> tuple[Point, Point, Point, Polygon]:
    """Vertices A,B,C (= labels). Angle at B, side a = BC, angle at C.

    Previous version delegated to SAS with the angle placed at the FIRST vertex (A) and the
    wrong side lengths: asked for angle B = 60, BC = 10 it produced angle B = 50, BC = 9.2."""
    angle_a_deg = 180.0 - (angle_b_deg + angle_c_deg)
    if a <= 0 or angle_b_deg <= 0 or angle_c_deg <= 0 or angle_a_deg <= 0:
        raise ValueError(f"Invalid ASA triangle: B={angle_b_deg}, a={a}, C={angle_c_deg}")
    ids, poly, lbls = triangle_ids(out_ids, labels, construction_id)
    # Law of sines: c = AB = a*sin(C)/sin(A)
    ab = a * math.sin(math.radians(angle_c_deg)) / math.sin(math.radians(angle_a_deg))
    rb = math.radians(angle_b_deg)
    B = (0.0, 0.0)
    C = (float(a), 0.0)
    A = (ab * math.cos(rb), ab * math.sin(rb))
    return _make_triangle(ids, poly, lbls, A, B, C)


def op_rectangle(
    out_ids: list[str], corner: Point, width: float, height: float
) -> tuple[Point, Point, Point, Point, Polygon]:
    p0 = corner
    p1 = Point(id=out_ids[1] if len(out_ids) > 1 else f"{corner.id}_1", x=corner.x + width, y=corner.y)
    p2 = Point(id=out_ids[2] if len(out_ids) > 2 else f"{corner.id}_2", x=corner.x + width, y=corner.y + height)
    p3 = Point(id=out_ids[3] if len(out_ids) > 3 else f"{corner.id}_3", x=corner.x, y=corner.y + height)
    poly = Polygon(id=out_ids[4] if len(out_ids) > 4 else f"rect_{corner.id}", vertices=[p0, p1, p2, p3])
    return p0, p1, p2, p3, poly


def op_circle(op_id: str, center: Point, radius: float | None = None, through: Point | None = None) -> Circle:
    if radius is not None:
        r = float(radius)
    elif through is not None:
        r = center.distance_to(through)
    else:
        raise ValueError("Circle must specify either radius or through point")
    if r < 1e-9:
        raise ValueError(f"Degenerate circle '{op_id}': radius is zero")
    return Circle(id=op_id, center=center, radius=r)


def op_arc(op_id: str, center: Point, p_from: Point, p_to: Point) -> Arc:
    r = center.distance_to(p_from)
    start_deg = math.degrees(math.atan2(p_from.y - center.y, p_from.x - center.x)) % 360
    end_deg = math.degrees(math.atan2(p_to.y - center.y, p_to.x - center.x)) % 360
    return Arc(id=op_id, center=center, radius=r, start_deg=start_deg, end_deg=end_deg)


def op_midpoint(op_id: str, seg: Segment) -> Point:
    return Point(id=op_id, x=(seg.p1.x + seg.p2.x) / 2.0, y=(seg.p1.y + seg.p2.y) / 2.0)


def line_intersection(p1: Point, p2: Point, p3: Point, p4: Point) -> Point | None:
    denom = (p1.x - p2.x) * (p3.y - p4.y) - (p1.y - p2.y) * (p3.x - p4.x)
    if abs(denom) < 1e-9:
        return None  # Parallel or collinear
    t = ((p1.x - p3.x) * (p3.y - p4.y) - (p1.y - p3.y) * (p3.x - p4.x)) / denom
    ix = p1.x + t * (p2.x - p1.x)
    iy = p1.y + t * (p2.y - p1.y)
    return Point(id="inter", x=ix, y=iy)


def op_intersection(op_id: str, e1: Any, e2: Any, pick: int = 0) -> Point:
    # Segment or Line intersection
    p1 = getattr(e1, "p1", None)
    p2 = getattr(e1, "p2", None)
    p3 = getattr(e2, "p1", None)
    p4 = getattr(e2, "p2", None)

    if p1 and p2 and p3 and p4:
        pt = line_intersection(p1, p2, p3, p4)
        if pt is None:
            raise ValueError(f"Lines '{e1.id}' and '{e2.id}' do not intersect (parallel)")
        pt.id = op_id
        return pt

    # Line/Segment and Circle intersection
    line_ent = e1 if (p1 and p2) else (e2 if (p3 and p4) else None)
    circ_ent = e1 if isinstance(e1, Circle) else (e2 if isinstance(e2, Circle) else None)

    if line_ent and circ_ent:
        lp1, lp2 = line_ent.p1, line_ent.p2
        dx, dy = lp2.x - lp1.x, lp2.y - lp1.y
        fx, fy = lp1.x - circ_ent.center.x, lp1.y - circ_ent.center.y
        a_coeff = dx * dx + dy * dy
        b_coeff = 2 * (fx * dx + fy * dy)
        c_coeff = fx * fx + fy * fy - circ_ent.radius * circ_ent.radius
        disc = b_coeff * b_coeff - 4 * a_coeff * c_coeff
        if disc < -1e-9:
            raise ValueError(f"Line '{line_ent.id}' and Circle '{circ_ent.id}' do not intersect")
        disc = max(0.0, disc)
        t0 = (-b_coeff - math.sqrt(disc)) / (2 * a_coeff)
        t1 = (-b_coeff + math.sqrt(disc)) / (2 * a_coeff)
        t = t1 if pick == 1 else t0
        return Point(id=op_id, x=lp1.x + t * dx, y=lp1.y + t * dy)

    raise ValueError(f"Unsupported intersection between types {type(e1)} and {type(e2)}")


def op_project(op_id: str, point: Point, target: Any) -> Point:
    p1 = getattr(target, "p1")
    p2 = getattr(target, "p2")
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        raise ValueError("Cannot project onto degenerate segment/line")
    t = ((point.x - p1.x) * dx + (point.y - p1.y) * dy) / length_sq
    return Point(id=op_id, x=p1.x + t * dx, y=p1.y + t * dy)


def op_parallel_through(op_id: str, line: Any, point: Point) -> Line:
    dx = line.p2.x - line.p1.x
    dy = line.p2.y - line.p1.y
    p2 = Point(id=f"{op_id}_p2", x=point.x + dx, y=point.y + dy)
    return Line(id=op_id, p1=point, p2=p2)


def op_perpendicular_through(op_id: str, line: Any, point: Point) -> Line:
    dx = line.p2.x - line.p1.x
    dy = line.p2.y - line.p1.y
    # Rotate 90 deg: (-dy, dx)
    p2 = Point(id=f"{op_id}_p2", x=point.x - dy, y=point.y + dx)
    return Line(id=op_id, p1=point, p2=p2)


def op_tangent_line(op_id: str, circle: Circle, point: Point) -> Line:
    # Radius vector from center to point
    dx = point.x - circle.center.x
    dy = point.y - circle.center.y
    # Perpendicular vector: (-dy, dx)
    p2 = Point(id=f"{op_id}_p2", x=point.x - dy, y=point.y + dx)
    return Line(id=op_id, p1=point, p2=p2)


def op_angle_bisector(op_id: str, vertex: Point, from_pt: Point, to_pt: Point) -> Ray:
    d1 = vertex.distance_to(from_pt)
    d2 = vertex.distance_to(to_pt)
    if d1 < 1e-9 or d2 < 1e-9:
        raise ValueError("Degenerate angle for bisector")
    # Unit vectors
    u1x, u1y = (from_pt.x - vertex.x) / d1, (from_pt.y - vertex.y) / d1
    u2x, u2y = (to_pt.x - vertex.x) / d2, (to_pt.y - vertex.y) / d2
    bx, by = u1x + u2x, u1y + u2y
    b_len = math.hypot(bx, by)
    if b_len < 1e-9:
        # Opposite directions, 180 deg
        bx, by = -u1y, u1x
        b_len = 1.0
    through = Point(id=f"{op_id}_thru", x=vertex.x + (bx / b_len) * 50.0, y=vertex.y + (by / b_len) * 50.0)
    return Ray(id=op_id, origin=vertex, through=through)


def op_angle_mark(op_id: str, vertex: Point, from_pt: Point, to_pt: Point) -> AngleMark:
    return AngleMark(id=op_id, vertex=vertex, p1=from_pt, p2=to_pt, is_right_angle=False)


RIGHT_ANGLE_TOLERANCE_DEG = 3.0


def op_right_angle_mark(op_id: str, vertex: Point, from_pt: Point, to_pt: Point) -> AngleMark:
    """The mark sits on a right angle at vertex between from_pt and to_pt.
    If the arms are not strictly perpendicular, auto-project to_pt onto the true perpendicular
    direction so the visual mark renders squarely, logging a warning rather than aborting the scene."""
    ax, ay = from_pt.x - vertex.x, from_pt.y - vertex.y
    bx, by = to_pt.x - vertex.x, to_pt.y - vertex.y
    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
    corrected_to = to_pt

    if la > 1e-9 and lb > 1e-9:
        dot = (ax * bx + ay * by) / (la * lb)
        deg = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        if abs(deg - 90.0) > RIGHT_ANGLE_TOLERANCE_DEG:
            import logging
            logging.getLogger("ai_math_tutor").warning(
                f"right_angle_mark at {vertex.label or vertex.id}: angle is {deg:.1f} deg (not 90). "
                f"Auto-projecting arm to ensure square mark renders without aborting scene."
            )
            # Cross product to determine whether to_pt is to the left or right of ray vertex->from_pt
            cross = ax * by - ay * bx
            sign = 1.0 if cross >= 0 else -1.0
            # Unit normal perpendicular to (ax, ay)
            nx, ny = -sign * (ay / la), sign * (ax / la)
            corrected_to = Point(
                id=f"{to_pt.id}_auto_perp",
                x=vertex.x + nx * lb,
                y=vertex.y + ny * lb,
                label=to_pt.label,
            )
    return AngleMark(id=op_id, vertex=vertex, p1=from_pt, p2=corrected_to, is_right_angle=True)


def op_tick_mark(op_id: str, segment: Segment, count: int = 1) -> TickMark:
    return TickMark(id=op_id, segment=segment, count=count)


def op_dimension(op_id: str, from_pt: Point, to_pt: Point, text: str | None = None, offset: float = 20.0) -> Dimension:
    return Dimension(id=op_id, p1=from_pt, p2=to_pt, text=text, offset=offset)


def op_axes(op_id: str, x_range: list[float], y_range: list[float]) -> Axes:
    return Axes(id=op_id, x_min=x_range[0], x_max=x_range[1], y_min=y_range[0], y_max=y_range[1])


def op_function_curve(op_id: str, domain: list[float], fn_eval: Any = None, steps: int = 120) -> Curve:
    """Sample fn_eval over domain. fn_eval is REQUIRED: the previous fallback silently drew
    y = x for every graph ("graph 2x + y = 6" rendered the wrong line with full confidence)."""
    if fn_eval is None:
        raise ValueError("function_curve needs an 'expression' in x, e.g. \"6 - 2*x\"")
    start_x, end_x = float(domain[0]), float(domain[1])
    if not end_x > start_x:
        raise ValueError(f"function_curve domain must be increasing: {domain}")
    dx = (end_x - start_x) / steps
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        x = start_x + i * dx
        try:
            y = fn_eval(x)
        except Exception:
            continue                      # skip poles / domain errors, keep the rest
        if y is not None and math.isfinite(y):
            points.append((x, y))
    if len(points) < 2:
        raise ValueError("function_curve produced no finite points over its domain")
    return Curve(id=op_id, points=points)


def op_number_line(op_id: str, val_range: list[float], tick_step: float, marks: list[float]) -> NumberLine:
    return NumberLine(id=op_id, min_val=val_range[0], max_val=val_range[1], tick_step=tick_step, marks=marks)


def op_label(op_id: str, target_id: str, text: str) -> Label:
    return Label(id=op_id, target_id=target_id, text=text)

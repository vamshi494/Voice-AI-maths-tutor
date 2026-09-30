# app/scene_engine/predicates.py
import math
from typing import Any
from app.scene_engine.operators import AngleMark, Circle, Point, Polygon, Segment

TOL_LENGTH_REL = 1e-6
TOL_ANGLE_DEG = 0.01


def get_direction_vector(entity: Any) -> tuple[float, float]:
    p1 = getattr(entity, "p1", None) or getattr(entity, "origin", None)
    p2 = getattr(entity, "p2", None) or getattr(entity, "through", None)
    if not p1 or not p2:
        raise ValueError(f"Entity {entity} has no endpoints for direction")
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return 0.0, 0.0
    return dx / length, dy / length


def angle_between_points_deg(vertex: Point, p1: Point, p2: Point) -> float:
    v1x, v1y = p1.x - vertex.x, p1.y - vertex.y
    v2x, v2y = p2.x - vertex.x, p2.y - vertex.y
    d1 = math.hypot(v1x, v1y)
    d2 = math.hypot(v2x, v2y)
    if d1 < 1e-9 or d2 < 1e-9:
        return 0.0
    dot = (v1x * v2x + v1y * v2y) / (d1 * d2)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def eval_on(point: Point, target: Any) -> bool:
    if isinstance(target, Circle):
        d = point.distance_to(target.center)
        return abs(d - target.radius) <= max(1e-6, 1e-6 * target.radius)
    p1 = getattr(target, "p1", None)
    p2 = getattr(target, "p2", None)
    if p1 and p2:
        seg_len = p1.distance_to(p2)
        if seg_len < 1e-9:
            return point.distance_to(p1) <= 1e-6
        # Cross product distance
        dist = abs((p2.y - p1.y) * point.x - (p2.x - p1.x) * point.y + p2.x * p1.y - p2.y * p1.x) / seg_len
        if dist > 1e-6:
            return False
        if isinstance(target, Segment):
            # Check bounding box / projection parameter
            dot = (point.x - p1.x) * (p2.x - p1.x) + (point.y - p1.y) * (p2.y - p1.y)
            return -1e-6 <= dot <= (seg_len * seg_len + 1e-6)
        return True
    return False


def eval_parallel(line1: Any, line2: Any) -> bool:
    u1x, u1y = get_direction_vector(line1)
    u2x, u2y = get_direction_vector(line2)
    cross = abs(u1x * u2y - u1y * u2x)
    return cross <= TOL_LENGTH_REL


def eval_perpendicular(line1: Any, line2: Any) -> bool:
    u1x, u1y = get_direction_vector(line1)
    u2x, u2y = get_direction_vector(line2)
    dot = abs(u1x * u2x + u1y * u2y)
    return dot <= TOL_LENGTH_REL


def eval_collinear(points: list[Point]) -> bool:
    if len(points) <= 2:
        return True
    p1, p2 = points[0], points[1]
    for pt in points[2:]:
        seg_len = p1.distance_to(p2)
        if seg_len < 1e-9:
            continue
        dist = abs((p2.y - p1.y) * pt.x - (p2.x - p1.x) * pt.y + p2.x * p1.y - p2.y * p1.x) / seg_len
        if dist > 1e-6:
            return False
    return True


def eval_equal_length(seg1: Segment, seg2: Segment) -> bool:
    l1, l2 = seg1.length, seg2.length
    tol = max(1e-6, TOL_LENGTH_REL * max(l1, l2))
    return abs(l1 - l2) <= tol


def _angle_triple(entity: Any) -> tuple[Point, Point, Point]:
    """Resolve an angle operand to (vertex, arm1, arm2).

    Assertions reference the scene's AngleMark entities (e.g. ang_A, ang_D), which carry
    vertex/p1/p2. The previous code subscripted them as a 3-tuple and raised
    "'AngleMark' object is not subscriptable", which failed the whole scene -> no diagram.
    A (vertex, p1, p2) sequence is still accepted for direct callers.
    """
    if isinstance(entity, AngleMark):
        return entity.vertex, entity.p1, entity.p2
    if isinstance(entity, (tuple, list)) and len(entity) >= 3:
        return entity[0], entity[1], entity[2]
    raise ValueError(
        f"equal_angle expects an AngleMark or a 3-point sequence, got {type(entity).__name__}"
    )


def eval_equal_angle(ang1: Any, ang2: Any) -> bool:
    v1, a1, b1 = _angle_triple(ang1)
    v2, a2, b2 = _angle_triple(ang2)
    deg1 = angle_between_points_deg(v1, a1, b1)
    deg2 = angle_between_points_deg(v2, a2, b2)
    return abs(deg1 - deg2) <= TOL_ANGLE_DEG


def eval_angle_between(vertex: Point, p1: Point, p2: Point, expected_deg: float) -> bool:
    deg = angle_between_points_deg(vertex, p1, p2)
    return abs(deg - float(expected_deg)) <= TOL_ANGLE_DEG


def eval_distance(p1: Point, p2: Point, expected_dist: float) -> bool:
    dist = p1.distance_to(p2)
    tol = max(1e-6, TOL_LENGTH_REL * max(dist, float(expected_dist)))
    return abs(dist - float(expected_dist)) <= tol


def eval_midpoint(mid: Point, seg: Segment) -> bool:
    true_mid_x = (seg.p1.x + seg.p2.x) / 2.0
    true_mid_y = (seg.p1.y + seg.p2.y) / 2.0
    return math.hypot(mid.x - true_mid_x, mid.y - true_mid_y) <= 1e-6


def eval_inside(point: Point, shape: Any) -> bool:
    if isinstance(shape, Circle):
        return point.distance_to(shape.center) <= shape.radius
    if isinstance(shape, Polygon):
        # Ray-casting algorithm
        inside = False
        n = len(shape.vertices)
        for i in range(n):
            j = (i + 1) % n
            vi, vj = shape.vertices[i], shape.vertices[j]
            if ((vi.y > point.y) != (vj.y > point.y)) and (
                point.x < (vj.x - vi.x) * (point.y - vi.y) / (vj.y - vi.y) + vi.x
            ):
                inside = not inside
        return inside
    return False


def eval_tangent(line: Any, circle: Circle) -> bool:
    p1 = getattr(line, "p1")
    p2 = getattr(line, "p2")
    seg_len = p1.distance_to(p2)
    if seg_len < 1e-9:
        return False
    dist = abs((p2.y - p1.y) * circle.center.x - (p2.x - p1.x) * circle.center.y + p2.x * p1.y - p2.y * p1.x) / seg_len
    tol = max(1e-6, TOL_LENGTH_REL * circle.radius)
    return abs(dist - circle.radius) <= tol


def eval_predicate(pred_name: str, entities: list[Any], expected: Any | None = None) -> bool:
    name = pred_name.lower().strip()
    if name in ("on", "incident"):
        return eval_on(entities[0], entities[1])
    if name == "parallel":
        return eval_parallel(entities[0], entities[1])
    if name == "perpendicular":
        return eval_perpendicular(entities[0], entities[1])
    if name == "collinear":
        return eval_collinear(entities)
    if name == "equal_length":
        return eval_equal_length(entities[0], entities[1])
    if name == "equal_angle":
        # expects two triples of points
        return eval_equal_angle(entities[0], entities[1])
    if name == "angle_between":
        return eval_angle_between(entities[0], entities[1], entities[2], float(expected))
    if name == "distance":
        return eval_distance(entities[0], entities[1], float(expected))
    if name == "midpoint":
        return eval_midpoint(entities[0], entities[1])
    if name == "inside":
        return eval_inside(entities[0], entities[1])
    if name == "tangent":
        return eval_tangent(entities[0], entities[1])
    if name == "label_attached":
        return entities[0] is not None and entities[1] is not None
    if name == "entity_count":
        return len(entities) == int(expected or 0)
    raise ValueError(f"Unknown predicate: '{pred_name}'")

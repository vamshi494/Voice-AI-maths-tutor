# app/scene_engine/capability.py
"""v1 Operator and Predicate capability manifest.

{operator_catalogue} and {predicate_catalogue} are generated from this file
so prompt text and engine capability can never drift apart.
"""

OPERATORS: dict[str, dict[str, str]] = {
    "point": {
        "inputs": "{x: float, y: float, label?: str}",
        "outputs": "point",
        "description": "Point at world coordinates (x, y); a single-letter id such as \"A\" is lettered on the board",
    },
    "point_polar": {
        "inputs": "{from: point_id, length: float, angleDeg: float}",
        "outputs": "point",
        "description": "Point at distance length and angle angleDeg from a base point",
    },
    "point_on_segment": {
        "inputs": "{segment: segment_id, ratio: float}",
        "outputs": "point",
        "description": "Point dividing segment in ratio (0..1)",
    },
    "segment": {
        "inputs": "{from: point_id, to: point_id}",
        "outputs": "segment",
        "description": "Line segment between two points",
    },
    "ray": {
        "inputs": "{from: point_id, through: point_id}",
        "outputs": "ray",
        "description": "Ray starting at 'from' passing through 'through'",
    },
    "line": {
        "inputs": "{through: [point_id1, point_id2]}",
        "outputs": "line",
        "description": "Infinite line through two points",
    },
    "polygon": {
        "inputs": "{vertices: [point_ids]}",
        "outputs": "polygon",
        "description": "Closed polygon through vertices in order",
    },
    "triangle_sss": {
        "inputs": "{a: float, b: float, c: float, labels: [V0, V1, V2]}",
        "outputs": "3 points named by labels + polygon named by the construction id",
        "description": "Triangle from three sides: a = V1V2 (opposite V0), b = V0V2, c = V0V1. "
                       "The vertex points get the label names, so reference them as \"A\", \"B\", \"C\"",
    },
    "triangle_sas": {
        "inputs": "{b: float, angleDeg: float, c: float, labels: [V0, V1, V2]}",
        "outputs": "3 points named by labels + polygon named by the construction id",
        "description": "Two sides and the INCLUDED angle, which sits at V0: V0V1 = c, V0V2 = b. "
                       "For AB = 5, BC = 12, angle B = 90 use labels [\"B\",\"A\",\"C\"], c = 5, b = 12",
    },
    "triangle_asa": {
        "inputs": "{angleBDeg: float, a: float, angleCDeg: float, labels: [A, B, C]}",
        "outputs": "3 points named by labels + polygon named by the construction id",
        "description": "Angle at B, side a = BC, angle at C",
    },
    "rectangle": {
        "inputs": "{corner: point_id, width: float, height: float}",
        "outputs": "4 points + polygon",
        "description": "Axis-aligned rectangle from corner with width and height",
    },
    "circle": {
        "inputs": "{center: point_id, radius: float} or {center: point_id, through: point_id}",
        "outputs": "circle",
        "description": "Circle with given center and radius or boundary point",
    },
    "arc": {
        "inputs": "{center: point_id, from: point_id, to: point_id}",
        "outputs": "arc",
        "description": "Counter-clockwise circular arc",
    },
    "midpoint": {
        "inputs": "{of: segment_id}",
        "outputs": "point",
        "description": "Midpoint of a segment",
    },
    "intersection": {
        "inputs": "{of: [entity_id1, entity_id2], pick: 0|1}",
        "outputs": "point",
        "description": "Intersection point of two lines, segments, or circles",
    },
    "project": {
        "inputs": "{point: point_id, onto: line_id | segment_id}",
        "outputs": "point",
        "description": "Foot of perpendicular from point onto line/segment",
    },
    "parallel_through": {
        "inputs": "{line: line_id, point: point_id}",
        "outputs": "line",
        "description": "Line parallel to line passing through point",
    },
    "perpendicular_through": {
        "inputs": "{line: line_id, point: point_id}",
        "outputs": "line",
        "description": "Line perpendicular to line passing through point",
    },
    "tangent_line": {
        "inputs": "{circle: circle_id, at: point_id}",
        "outputs": "line",
        "description": "Tangent line to circle at given boundary point",
    },
    "angle_bisector": {
        "inputs": "{vertex: point_id, from: point_id, to: point_id}",
        "outputs": "ray",
        "description": "Ray bisecting angle from-vertex-to",
    },
    "angle_mark": {
        "inputs": "{vertex: point_id, from: point_id, to: point_id}",
        "outputs": "mark",
        "description": "Angle arc mark",
    },
    "right_angle_mark": {
        "inputs": "{vertex: point_id, from: point_id, to: point_id}",
        "outputs": "mark",
        "description": "Right angle square symbol",
    },
    "tick_mark": {
        "inputs": "{segment: segment_id, count: 1|2|3}",
        "outputs": "mark",
        "description": "Congruence tick mark(s) on segment",
    },
    "dimension": {
        "inputs": "{from: point_id, to: point_id, text: str, offset: float}",
        "outputs": "dimension",
        "description": "Dimension line with distance measurement",
    },
    "axes": {
        "inputs": "{xRange: [min, max], yRange: [min, max]}",
        "outputs": "axes",
        "description": "Coordinate axes with ticks and origin",
    },
    "function_curve": {
        "inputs": "{expression: str in x, domain: [min, max]}",
        "outputs": "curve",
        "description": "Graph of y = expression over domain, e.g. {\"expression\": \"6 - 2*x\", \"domain\": [0, 3]}. "
                       "The expression is REQUIRED",
    },
    "number_line": {
        "inputs": "{range: [min, max], tickStep: float, marks: [float]}",
        "outputs": "number_line",
        "description": "Horizontal number line with ticks and marked values",
    },
    "label": {
        "inputs": "{target: entity_id, text: str}",
        "outputs": "label",
        "description": "Text label attached to target entity",
    },
}

PREDICATES: dict[str, dict[str, str]] = {
    "on": {"meaning": "A point lies on a line, segment or circle", "args": "entities: [point, target]"},
    "incident": {"meaning": "A point lies on a line, segment or circle", "args": "entities: [point, target]"},
    "parallel": {"meaning": "Two lines or segments are parallel", "args": "entities: [line1, line2]"},
    "perpendicular": {"meaning": "Two lines or segments are perpendicular", "args": "entities: [line1, line2]"},
    "collinear": {"meaning": "Three or more points lie on one line", "args": "entities: [p1, p2, p3]"},
    "equal_length": {"meaning": "Two segments have the same length", "args": "entities: [seg1, seg2]"},
    "equal_angle": {"meaning": "Two angles have the same degree measurement", "args": "entities: [ang1, ang2]"},
    "angle_between": {"meaning": "Angle has expected degrees", "args": "entities: [v, p1, p2], expected: float"},
    "distance": {"meaning": "Distance between points equals expected", "args": "entities: [p1, p2], expected: float"},
    "midpoint": {"meaning": "Point is midpoint of segment", "args": "entities: [mid, seg]"},
    "inside": {"meaning": "Point is inside a polygon or circle", "args": "entities: [point, shape]"},
    "tangent": {"meaning": "Line is tangent to circle", "args": "entities: [line, circle]"},
    "label_attached": {"meaning": "Label is attached to target entity", "args": "entities: [label, target]"},
    "entity_count": {"meaning": "Count of entities of a kind", "args": "entities: [], expected: int"},
}


def get_operator_catalogue() -> str:
    lines = [
        "Reference any produced entity by its id; a point may also be referenced by its label.",
        "Numbers may be plain numbers or plan quantity ids (e.g. \"q_ab\").",
    ]
    for op, info in sorted(OPERATORS.items()):
        lines.append(f"- {op}: inputs={info['inputs']} -> {info['outputs']}; {info['description']}")
    return "\n".join(lines)


def get_predicate_catalogue() -> str:
    lines = []
    for pred, info in sorted(PREDICATES.items()):
        lines.append(f"- {pred}: {info['args']}; {info['meaning']}")
    return "\n".join(lines)

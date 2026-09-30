# app/scene_engine/compile.py
from dataclasses import dataclass, field
from typing import Any
from app.contracts.scene import RepairError, SceneAnnotation, SceneDocument, SceneRevealGroup, ValidationReport
from app.contracts.turn_plan import TurnPlan
from app.scene_engine.capability import OPERATORS, PREDICATES
from app.scene_engine.operators import (
    AngleMark,
    Arc,
    Axes,
    Circle,
    Curve,
    Dimension,
    Label,
    Line,
    NumberLine,
    Point,
    Polygon,
    Ray,
    Segment,
    TickMark,
    op_angle_bisector,
    op_angle_mark,
    op_arc,
    op_axes,
    op_circle,
    op_dimension,
    op_function_curve,
    op_intersection,
    op_line,
    op_midpoint,
    op_number_line,
    op_parallel_through,
    op_perpendicular_through,
    op_point,
    op_point_on_segment,
    op_point_polar,
    op_polygon,
    op_project,
    op_ray,
    op_rectangle,
    op_right_angle_mark,
    op_segment,
    op_tangent_line,
    op_tick_mark,
    op_triangle_asa,
    op_triangle_sas,
    op_triangle_sss,
)
from app.scene_engine.predicates import eval_predicate
from app.contracts.base import coerce_number
import re

class EntityTable(dict):
    """Entity id -> geometry, with reference resolution by alias.

    LLMs refer to things the way they appear on the board: a point declared as
    {"id": "pt_B", "label": "B"} is referenced as "B"; a triangle vertex produced with
    labels ["A","B","C"] is referenced as "A". Lookups try the exact id, then declared
    labels, then common id prefixes. A miss still raises KeyError(ref) so the compiler
    reports unresolved_reference with the real name.
    """

    def __init__(self) -> None:
        super().__init__()
        self.aliases: dict[str, str] = {}

    def resolve(self, ref: Any) -> str | None:
        if not isinstance(ref, str):
            return None
        if dict.__contains__(self, ref):
            return ref
        target = self.aliases.get(ref)
        if target is not None and dict.__contains__(self, target):
            return target
        for cand in (f"pt_{ref}", f"p_{ref}", f"point_{ref}", f"c_{ref}", ref.removeprefix("c_"), ref.upper()):
            if dict.__contains__(self, cand):
                return cand
        return None

    def __getitem__(self, ref: Any) -> Any:
        rid = self.resolve(ref)
        if rid is None:
            raise KeyError(ref)
        return dict.__getitem__(self, rid)

    def get(self, ref: Any, default: Any = None) -> Any:  # type: ignore[override]
        rid = self.resolve(ref)
        return dict.__getitem__(self, rid) if rid is not None else default

    def __contains__(self, ref: object) -> bool:
        return self.resolve(ref) is not None


_KEY_ALIASES = {
    "angle_deg": "angleDeg", "angle": "angleDeg", "angledeg": "angleDeg", "degrees": "angleDeg",
    "angle_b_deg": "angleBDeg", "angle_c_deg": "angleCDeg", "angleb": "angleBDeg", "anglec": "angleCDeg",
    "x_range": "xRange", "y_range": "yRange", "tick_step": "tickStep", "expression_id": "expressionId",
    "centre": "center", "r": "radius", "start": "from", "end": "to", "p1": "from", "p2": "to",
    "origin": "from", "source": "from", "target_id": "target", "value": "text",
}
_OP_SYNONYMS = {
    "segment": "segment", "line_segment": "segment", "seg": "segment",
    "triangle": "triangle_sss", "polyline": "polygon", "quadrilateral": "polygon",
    "perpendicular": "perpendicular_through", "parallel": "parallel_through",
    "foot": "project", "perpendicular_foot": "project", "angle": "angle_mark",
    "right_angle": "right_angle_mark", "graph": "function_curve", "curve": "function_curve",
    "text": "label", "numberline": "number_line", "coordinate_axes": "axes",
}


def normalize_operator(op: str) -> str:
    o = (op or "").lower().strip().replace("-", "_").replace(" ", "_")
    return _OP_SYNONYMS.get(o, o)


def normalize_inputs(op: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize input keys to the capability manifest's names."""
    out: dict[str, Any] = {}
    for k, v in (inputs or {}).items():
        key = _KEY_ALIASES.get(k, _KEY_ALIASES.get(k.lower(), k))
        out[key] = v
    pts = out.get("points") or out.get("vertices")
    if op in ("segment", "ray", "dimension") and isinstance(pts, list) and len(pts) >= 2:
        out.setdefault("from", pts[0])
        out.setdefault("to" if op != "ray" else "through", pts[1])
    if op == "ray" and "to" in out and "through" not in out:
        out["through"] = out["to"]
    if op == "polygon" and "vertices" not in out and isinstance(out.get("points"), list):
        out["vertices"] = out["points"]
    if op == "line" and "through" not in out:
        if isinstance(pts, list):
            out["through"] = pts[:2]
        elif "from" in out and "to" in out:
            out["through"] = [out["from"], out["to"]]
    if op in ("angle_mark", "right_angle_mark", "angle_bisector") and isinstance(pts, list) and len(pts) == 3:
        out.setdefault("from", pts[0]); out.setdefault("vertex", pts[1]); out.setdefault("to", pts[2])
    if op == "midpoint" and "of" not in out:
        out["of"] = out.get("segment")
    if op == "tick_mark" and "segment" not in out:
        out["segment"] = out.get("of")
    if op == "intersection" and "of" not in out:
        out["of"] = out.get("entities") or out.get("lines") or [out.get("a"), out.get("b")]
    if op == "point" and "x" not in out and isinstance(out.get("coords") or out.get("at"), (list, tuple)):
        xy = out.get("coords") or out.get("at")
        out["x"], out["y"] = xy[0], xy[1]
    if op == "function_curve" and "expression" not in out:
        for k in ("expr", "equation", "function", "formula", "fx"):
            if k in out:
                out["expression"] = out[k]
                break
    return out


def num(v: Any, name: str = "value") -> float:
    """Numeric input: accepts 5, '5', '5 cm'. Missing -> clear error for the repair prompt."""
    try:
        val = coerce_number(v)
    except ValueError:
        val = None
    if val is None:
        raise ValueError(f"input '{name}' must be a number, got {v!r}")
    return val


def make_function(expression: str, variable: str = "x"):
    """Safe single-variable evaluator: '6 - 2x', 'y = x^2 - 4', '0.5*x+1'. No eval()."""
    from app.tutor.arithmetic import WhitelistEvaluator, normalize_source_text
    import ast as _ast
    expr = str(expression)
    if "=" in expr:
        lhs, rhs = expr.split("=", 1)
        expr = rhs if lhs.strip().lower() in ("y", "f(x)", "f(" + variable + ")") else rhs
    expr = normalize_source_text(expr).strip()
    expr = re.sub(r"(\d)\s*([a-zA-Z(])", r"\1*\2", expr)     # 2x -> 2*x, 3(x+1) -> 3*(x+1)
    expr = re.sub(r"\)\s*\(", ")*(", expr)
    tree = _ast.parse(expr, mode="eval")

    def fn(xv: float) -> float:
        return WhitelistEvaluator({variable: xv, "x": xv}).visit(tree)
    fn(0.5)  # fail fast on bad syntax / unknown names -> repair message
    return fn



@dataclass
class RenderScene:
    entities: dict[str, Any] = field(default_factory=dict)
    annotations: list[SceneAnnotation] = field(default_factory=list)
    reveal_groups: list[SceneRevealGroup] = field(default_factory=list)
    doc: SceneDocument | None = None


def resolve_val(val: Any, quantity_map: dict[str, float]) -> Any:
    if isinstance(val, str) and val in quantity_map:
        return quantity_map[val]
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list):
        return [resolve_val(v, quantity_map) for v in val]
    if isinstance(val, dict):
        return {k: resolve_val(v, quantity_map) for k, v in val.items()}
    return val


STRUCTURAL_PREDICATES = frozenset({"on", "incident", "parallel", "perpendicular", "collinear",
                                   "midpoint", "tangent", "angle_between"})


def compile_scene_document(
    doc: SceneDocument, plan: TurnPlan | None = None
) -> tuple[RenderScene | None, ValidationReport]:
    """Deterministic compilation of SceneDocument into RenderScene.

    Checks:
    - unknown operators and predicates
    - unresolved references
    - duplicate entity production
    - missing required entities
    - duplicate geometry
    - degenerate constructions
    - assertion evaluations
    """
    errors: list[RepairError] = []
    warnings: list[RepairError] = []
    eval_count = 0
    pass_count = 0

    if doc.visual_decision == "text_only":
        report = ValidationReport(
            valid=True,
            errors=[],
            warnings=[],
            evaluated_assertions=0,
            passed_assertions=0,
        )
        return RenderScene(doc=doc), report

    # Quantity values map
    quantity_map: dict[str, float] = {}
    if plan:
        for q in plan.givens + plan.derived:
            if q.value is not None:
                quantity_map[q.id] = q.value
    for q in doc.quantities:
        if q.value is not None:
            quantity_map[q.id] = q.value

    entities = EntityTable()
    for se in doc.entities:
        if se.label and se.label != se.id:
            entities.aliases.setdefault(se.label, se.id)

    # Compile constructions
    for c in doc.constructions:
        if not c.outputs:
            c.outputs = [c.id]
        op_name = normalize_operator(c.operator)
        if op_name not in OPERATORS:
            errors.append(
                RepairError(
                    code="unknown_operator",
                    message=f"Unknown operator: '{c.operator}'",
                    entity_ids=c.outputs,
                    severity="error",
                )
            )
            continue

        for out_id in c.outputs:
            if out_id in entities:
                errors.append(
                    RepairError(
                        code="duplicate_entity_production",
                        message=f"Entity '{out_id}' produced more than once",
                        entity_ids=[out_id],
                        severity="error",
                    )
                )

        # Resolve inputs
        raw_inputs = normalize_inputs(op_name, c.inputs)
        resolved_inputs = resolve_val(raw_inputs, quantity_map)

        try:
            if op_name == "point":
                pid = c.outputs[0]
                pt = op_point(pid, num(resolved_inputs.get("x"), "x"), num(resolved_inputs.get("y"), "y"))
                lbl = resolved_inputs.get("label")
                pt.label = str(lbl) if lbl else (pid if re.fullmatch(r"[A-Z][0-9']*", pid) else None)
                entities[pid] = pt

            elif op_name == "point_polar":
                base = entities[resolved_inputs["from"]]
                length = num(resolved_inputs.get("length"), "length")
                ang = num(resolved_inputs.get("angleDeg"), "angleDeg")
                entities[c.outputs[0]] = op_point_polar(c.outputs[0], base, length, ang)

            elif op_name == "point_on_segment":
                seg = entities[resolved_inputs["segment"]]
                ratio = num(resolved_inputs.get("ratio"), "ratio")
                entities[c.outputs[0]] = op_point_on_segment(c.outputs[0], seg, ratio)

            elif op_name == "segment":
                p1 = entities[resolved_inputs["from"]]
                p2 = entities[resolved_inputs["to"]]
                entities[c.outputs[0]] = op_segment(c.outputs[0], p1, p2)

            elif op_name == "ray":
                origin = entities[resolved_inputs["from"]]
                through = entities[resolved_inputs["through"]]
                entities[c.outputs[0]] = op_ray(c.outputs[0], origin, through)

            elif op_name == "line":
                pts = [entities[pid] for pid in resolved_inputs["through"]]
                entities[c.outputs[0]] = op_line(c.outputs[0], pts[0], pts[1])

            elif op_name == "polygon":
                pts = [entities[pid] for pid in resolved_inputs["vertices"]]
                entities[c.outputs[0]] = op_polygon(c.outputs[0], pts)

            elif op_name == "triangle_sss":
                a = num(resolved_inputs.get("a"), "a")
                b = num(resolved_inputs.get("b"), "b")
                c_len = num(resolved_inputs.get("c"), "c")
                labels = resolved_inputs.get("labels", ["A", "B", "C"])
                pa, pb, pc, poly = op_triangle_sss(c.outputs if len(c.outputs) != 1 or c.outputs[0] != c.id else [], a, b, c_len, labels, construction_id=c.id)
                entities[pa.id] = pa
                entities[pb.id] = pb
                entities[pc.id] = pc
                entities[poly.id] = poly

            elif op_name == "triangle_sas":
                b = num(resolved_inputs.get("b"), "b")
                ang = num(resolved_inputs.get("angleDeg"), "angleDeg")
                c_len = num(resolved_inputs.get("c"), "c")
                labels = resolved_inputs.get("labels", ["A", "B", "C"])
                pa, pb, pc, poly = op_triangle_sas(c.outputs if len(c.outputs) != 1 or c.outputs[0] != c.id else [], b, ang, c_len, labels, construction_id=c.id)
                entities[pa.id] = pa
                entities[pb.id] = pb
                entities[pc.id] = pc
                entities[poly.id] = poly

            elif op_name == "triangle_asa":
                b_ang = num(resolved_inputs.get("angleBDeg"), "angleBDeg")
                a = num(resolved_inputs.get("a"), "a")
                c_ang = num(resolved_inputs.get("angleCDeg"), "angleCDeg")
                labels = resolved_inputs.get("labels", ["A", "B", "C"])
                pa, pb, pc, poly = op_triangle_asa(c.outputs if len(c.outputs) != 1 or c.outputs[0] != c.id else [], b_ang, a, c_ang, labels, construction_id=c.id)
                entities[pa.id] = pa
                entities[pb.id] = pb
                entities[pc.id] = pc
                entities[poly.id] = poly

            elif op_name == "rectangle":
                corner = entities[resolved_inputs["corner"]]
                w = num(resolved_inputs.get("width"), "width")
                h = num(resolved_inputs.get("height"), "height")
                p0, p1, p2, p3, poly = op_rectangle(c.outputs, corner, w, h)
                entities[p0.id] = p0
                entities[p1.id] = p1
                entities[p2.id] = p2
                entities[p3.id] = p3
                entities[poly.id] = poly

            elif op_name == "circle":
                center = entities[resolved_inputs["center"]]
                radius = resolved_inputs.get("radius")
                radius = num(radius, "radius") if radius is not None else None
                through = entities.get(resolved_inputs.get("through"))
                entities[c.outputs[0]] = op_circle(c.outputs[0], center, radius, through)

            elif op_name == "arc":
                center = entities[resolved_inputs["center"]]
                p_from = entities[resolved_inputs["from"]]
                p_to = entities[resolved_inputs["to"]]
                entities[c.outputs[0]] = op_arc(c.outputs[0], center, p_from, p_to)

            elif op_name == "midpoint":
                seg = entities[resolved_inputs["of"]]
                entities[c.outputs[0]] = op_midpoint(c.outputs[0], seg)

            elif op_name == "intersection":
                e1 = entities[resolved_inputs["of"][0]]
                e2 = entities[resolved_inputs["of"][1]]
                pick = int(resolved_inputs.get("pick", 0))
                entities[c.outputs[0]] = op_intersection(c.outputs[0], e1, e2, pick)

            elif op_name == "project":
                pt = entities[resolved_inputs["point"]]
                onto = entities[resolved_inputs["onto"]]
                entities[c.outputs[0]] = op_project(c.outputs[0], pt, onto)

            elif op_name == "parallel_through":
                line_ent = entities[resolved_inputs["line"]]
                pt = entities[resolved_inputs["point"]]
                entities[c.outputs[0]] = op_parallel_through(c.outputs[0], line_ent, pt)

            elif op_name == "perpendicular_through":
                line_ent = entities[resolved_inputs["line"]]
                pt = entities[resolved_inputs["point"]]
                entities[c.outputs[0]] = op_perpendicular_through(c.outputs[0], line_ent, pt)

            elif op_name == "tangent_line":
                circ = entities[resolved_inputs["circle"]]
                pt = entities[resolved_inputs["at"]]
                entities[c.outputs[0]] = op_tangent_line(c.outputs[0], circ, pt)

            elif op_name == "angle_bisector":
                v = entities[resolved_inputs["vertex"]]
                pf = entities[resolved_inputs["from"]]
                pt = entities[resolved_inputs["to"]]
                entities[c.outputs[0]] = op_angle_bisector(c.outputs[0], v, pf, pt)

            elif op_name == "angle_mark":
                v = entities[resolved_inputs["vertex"]]
                pf = entities[resolved_inputs["from"]]
                pt = entities[resolved_inputs["to"]]
                entities[c.outputs[0]] = op_angle_mark(c.outputs[0], v, pf, pt)

            elif op_name == "right_angle_mark":
                v = entities[resolved_inputs["vertex"]]
                pf = entities[resolved_inputs["from"]]
                pt = entities[resolved_inputs["to"]]
                mark = op_right_angle_mark(c.outputs[0], v, pf, pt)
                entities[c.outputs[0]] = mark
                if mark.p2.id.endswith("_auto_perp"):
                    warnings.append(
                        RepairError(
                            code="right_angle_auto_projected",
                            message=f"right_angle_mark '{c.id}' at {v.label or v.id}: arms were not perpendicular; auto-projected arm to render square mark cleanly",
                            entity_ids=c.outputs,
                            severity="warning",
                        )
                    )

            elif op_name == "tick_mark":
                seg = entities[resolved_inputs["segment"]]
                cnt = int(resolved_inputs.get("count", 1))
                entities[c.outputs[0]] = op_tick_mark(c.outputs[0], seg, cnt)

            elif op_name == "dimension":
                p1 = entities[resolved_inputs["from"]]
                p2 = entities[resolved_inputs["to"]]
                txt = resolved_inputs.get("text")
                off = num(resolved_inputs.get("offset", 20.0), "offset")
                entities[c.outputs[0]] = op_dimension(c.outputs[0], p1, p2, txt, off)

            elif op_name == "axes":
                xr = [num(v, "xRange") for v in resolved_inputs["xRange"]]
                yr = [num(v, "yRange") for v in resolved_inputs["yRange"]]
                entities[c.outputs[0]] = op_axes(c.outputs[0], xr, yr)

            elif op_name == "function_curve":
                dom = [num(v, "domain") for v in resolved_inputs["domain"]]
                expr = resolved_inputs.get("expression")
                fn = make_function(expr, str(resolved_inputs.get("variable") or "x")) if expr else None
                entities[c.outputs[0]] = op_function_curve(c.outputs[0], dom, fn)

            elif op_name == "number_line":
                rng = [num(v, "range") for v in resolved_inputs["range"]]
                tstep = num(resolved_inputs.get("tickStep", 1.0), "tickStep")
                marks = [num(m, "marks") for m in (resolved_inputs.get("marks") or [])]
                entities[c.outputs[0]] = op_number_line(c.outputs[0], rng, tstep, marks)

            elif op_name == "label":
                target_id = str(resolved_inputs.get("target") or resolved_inputs.get("target_id") or "")
                text = str(resolved_inputs.get("text", ""))
                target_ent = entities.get(target_id)
                if isinstance(target_ent, Point):
                    x, y = target_ent.x, target_ent.y
                elif target_ent is not None and hasattr(target_ent, "x") and hasattr(target_ent, "y"):
                    x, y = float(target_ent.x), float(target_ent.y)
                else:
                    x = num(resolved_inputs.get("x", 0.0), "x")
                    y = num(resolved_inputs.get("y", 0.0), "y")
                entities[c.outputs[0]] = Label(id=c.outputs[0], target_id=target_id, text=text, x=x, y=y)

            else:
                errors.append(
                    RepairError(
                        code="unimplemented_operator",
                        message=f"Operator '{op_name}' not implemented",
                        entity_ids=c.outputs,
                        severity="error",
                    )
                )
        except KeyError as e:
            errors.append(
                RepairError(
                    code="unresolved_reference",
                    message=f"{c.operator} '{c.id}' references {e}, which no earlier construction produced",
                    entity_ids=c.outputs,
                    severity="error",
                )
            )
        except ValueError as e:
            errors.append(
                RepairError(
                    code="degenerate_construction",
                    message=f"{c.operator} '{c.id}': {e}",
                    entity_ids=c.outputs,
                    severity="error",
                )
            )
        except Exception as e:
            # Catch TypeError / AttributeError / IndexError / ZeroDivisionError here so a bad
            # construction degrades to text-only instead of crashing the turn.
            errors.append(
                RepairError(
                    code="construction_error",
                    message=f"{c.operator} '{c.id}' has wrong input types or shapes "
                            f"({type(e).__name__}: {e}). Check it against the operator catalogue.",
                    entity_ids=c.outputs,
                    severity="error",
                )
            )

    # Check required entities
    for req_id in doc.required_entity_ids:
        if req_id not in entities:
            errors.append(
                RepairError(
                    code="required_entity_missing",
                    message=f"Required entity '{req_id}' was not produced",
                    entity_ids=[req_id],
                    severity="error",
                )
            )

    # Check duplicate geometry
    points_list = [e for e in entities.values() if isinstance(e, Point)]
    for i in range(len(points_list)):
        for j in range(i + 1, len(points_list)):
            if points_list[i].distance_to(points_list[j]) < 1e-9:
                errors.append(
                    RepairError(
                        code="duplicate_geometry",
                        message=f"Points '{points_list[i].id}' and '{points_list[j].id}' are identical (distance < 1e-9)",
                        entity_ids=[points_list[i].id, points_list[j].id],
                        severity="error",
                    )
                )

    segments_list = [e for e in entities.values() if isinstance(e, Segment)]
    for i in range(len(segments_list)):
        s1 = segments_list[i]
        for j in range(i + 1, len(segments_list)):
            s2 = segments_list[j]
            if (s1.p1.id == s2.p1.id and s1.p2.id == s2.p2.id) or (s1.p1.id == s2.p2.id and s1.p2.id == s2.p1.id):
                errors.append(
                    RepairError(
                        code="duplicate_geometry",
                        message=f"Segments '{s1.id}' and '{s2.id}' have identical endpoints",
                        entity_ids=[s1.id, s2.id],
                        severity="error",
                    )
                )

    # Check for isolated points without connecting geometry.
    # If the scene has 3+ points but zero segments, polygons, lines, rays, circles,
    # arcs, or curves connecting them, the diagram would render as invisible bare dots:
    # the "bare points instead of a triangle" failure this check blocks.
    if len(points_list) >= 3:
        has_connectors = any(
            isinstance(e, (Segment, Line, Polygon, Circle, Arc, Curve, Ray))
            for e in entities.values()
        )
        if not has_connectors:
            point_ids = [p.id for p in points_list[:6]]
            errors.append(
                RepairError(
                    code="no_connected_geometry",
                    message=(
                        f"Scene has {len(points_list)} points ({', '.join(point_ids)}) but no "
                        f"segments, polygon, or other connecting constructions. Bare points are "
                        f"invisible to the student. You MUST add a polygon construction connecting "
                        f"the vertices, or segment constructions for each side (e.g. segment AB, "
                        f"segment BC, segment CA) to form visible closed figures."
                    ),
                    entity_ids=point_ids,
                    severity="error",
                )
            )

    # Evaluate assertions
    for a in doc.assertions:
        eval_count += 1
        p_name = a.predicate.lower().strip().replace("-", "_").replace(" ", "_")
        if p_name not in PREDICATES:
            errors.append(
                RepairError(
                    code="unknown_predicate",
                    message=f"Unknown predicate: '{a.predicate}'",
                    entity_ids=a.entities,
                    severity="error",
                )
            )
            continue

        resolved_ents = []
        missing = False
        for eid in a.entities:
            if eid in entities:
                resolved_ents.append(entities.get(eid))
            else:
                missing = True
                errors.append(
                    RepairError(
                        code="assertion_unresolved_entity",
                        message=f"Entity '{eid}' for assertion '{a.id}' not found",
                        entity_ids=[eid],
                        severity="error",
                    )
                )
        if missing:
            continue

        # Structural predicates (parallel, perpendicular, collinear, etc.) and assertions marked
        # as warning degrade to warnings so that diagrams render rather than falling back to text-only.
        severity = "warning" if (p_name in STRUCTURAL_PREDICATES or a.severity == "warning") else a.severity
        try:
            passed = eval_predicate(p_name, resolved_ents, expected=a.expected)
            if passed:
                pass_count += 1
            else:
                rep_err = RepairError(
                    code="assertion_failed",
                    message=f"Assertion '{a.id}' failed: predicate {a.predicate} on {a.entities}",
                    entity_ids=a.entities,
                    severity=severity,
                )
                if severity == "error":
                    errors.append(rep_err)
                else:
                    warnings.append(rep_err)
        except Exception as e:
            rep_err = RepairError(
                code="assertion_eval_error",
                message=f"Assertion '{a.id}' evaluation error: {e}",
                entity_ids=a.entities,
                severity=severity,
            )
            if severity == "error":
                errors.append(rep_err)
            else:
                warnings.append(rep_err)

    is_valid = len(errors) == 0
    report = ValidationReport(
        valid=is_valid,
        errors=errors,
        warnings=warnings,
        evaluated_assertions=eval_count,
        passed_assertions=pass_count,
    )

    if not is_valid:
        return None, report

    scene = RenderScene(
        entities=entities,
        annotations=doc.annotations,
        reveal_groups=doc.reveal_groups,
        doc=doc,
    )
    return scene, report

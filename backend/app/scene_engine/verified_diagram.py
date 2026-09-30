# app/scene_engine/verified_diagram.py
import re
from app.observability import log_event
from typing import Any

from app.contracts.diagram import (
    SemanticRef,
    DeferredAnnotation,
    DiagramAnchor,
    DiagramCommand,
    DiagramGroup,
    DiagramReveal,
    LabelFact,
    VerifiedDiagram,
)
from app.contracts.turn_plan import TurnPlan
from app.scene_engine.compile import RenderScene
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
)
from app.scene_engine.layout import Rect
from app.scene_engine.project import FIGURE_RECT_DEFAULT, project_scene_to_commands


def _label_of(obj: Any) -> str | None:
    label = getattr(obj, "label", None)
    return label.strip() if isinstance(label, str) and label.strip() else None


def _canonical_id_map(entities: dict[str, Any]) -> dict[str, str]:
    """Canonical id per compiled entity type; collisions get `_2`, `_3` suffixes.

    Point→`pt_{L}`, Segment→`seg_{A}{B}` (declared order), Polygon 3→`tri_{ABC}` / 4→
    `quad_{ABCD}` / else `poly_{…}`, AngleMark→`ang_{A}{V}{B}`, Circle→`cir_{O}`,
    Line→`line_{A}{B}`, Ray→`ray_{A}{B}`, any other → `{kind}_{llm_id}`. Entities whose
    letter labels are missing fall back to the generic form.
    """
    counts: dict[str, int] = {}
    out: dict[str, str] = {}
    for ent_id, ent in entities.items():
        base: str | None = None
        if isinstance(ent, Point):
            l = _label_of(ent)
            base = f"pt_{l}" if l else None
        elif isinstance(ent, Segment):
            a, b = _label_of(ent.p1), _label_of(ent.p2)
            base = f"seg_{a}{b}" if a and b else None
        elif isinstance(ent, Polygon):
            names = [_label_of(v) for v in ent.vertices]
            if len(ent.vertices) == 3 and all(names):
                base = f"tri_{''.join(names)}"
            elif len(ent.vertices) == 4 and all(names):
                base = f"quad_{''.join(names)}"
            elif all(names):
                base = f"poly_{''.join(names)}"
        elif isinstance(ent, AngleMark):
            a, v, b = _label_of(ent.p1), _label_of(ent.vertex), _label_of(ent.p2)
            base = f"ang_{a}{v}{b}" if a and v and b else None
        elif isinstance(ent, Circle):
            o = _label_of(ent.center)
            base = f"cir_{o}" if o else None
        elif isinstance(ent, Line):
            a, b = _label_of(ent.p1), _label_of(ent.p2)
            base = f"line_{a}{b}" if a and b else None
        elif isinstance(ent, Ray):
            a, b = _label_of(ent.origin), _label_of(ent.through)
            base = f"ray_{a}{b}" if a and b else None
        if base is None:
            base = f"{type(ent).__name__.lower()}_{ent_id}"
        n = counts.get(base, 0) + 1
        counts[base] = n
        out[ent_id] = base if n == 1 else f"{base}_{n}"
    return out


def _qualify(llm_id: str, canonical: dict[str, str], namespace: str) -> str:
    """Namespace prefix is applied last: `d1_seg_AB`."""
    return f"{namespace}{canonical.get(llm_id, llm_id)}"



FIGURE_LABEL_MAX_WORDS = 4
_RELATION_RE = re.compile(r"∥|\|\||⊥|≅|∼|~|\b(collinear|parallel|perpendicular|similar|congruent|gives|so|hence|therefore)\b",
                          re.IGNORECASE)


def is_figure_label_text(text: str) -> bool:
    """A figure annotation is a short measurement ("5 cm", "AB = 5 cm", "x = 2.4"), not a sentence
    or a relation statement."""
    t = (text or "").strip()
    return bool(t) and len(t.split()) <= FIGURE_LABEL_MAX_WORDS and not _RELATION_RE.search(t)


def _canonical_aliases(ent: Any, label: str) -> list[str]:
    """Prefixed names the teaching prompt uses for an entity with vertex-letter label `label`."""
    if isinstance(ent, Point):
        return [f"pt_{label}", f"point_{label}"]
    if isinstance(ent, Segment) and len(label) == 2:
        return [f"{p}_{x}" for p in ("seg", "segment", "side") for x in (label, label[::-1])]
    if isinstance(ent, AngleMark) and len(label) == 3:
        return [f"ang_{label}", f"ang_{label[::-1]}", f"angle_{label}"]
    if isinstance(ent, Polygon):
        n = len(label)
        cyc = {label[k:] + label[:k] for k in range(n)} | {(label[k:] + label[:k])[::-1] for k in range(n)}
        prefixes = ("tri", "triangle") if n == 3 else ("poly", "quad")
        return [f"{p}_{c}" for c in sorted(cyc) for p in prefixes]
    return []

def build_verified_diagram(
    scene: RenderScene,
    plan: TurnPlan | None = None,
    namespace: str = "",
    target: Rect = FIGURE_RECT_DEFAULT,
) -> VerifiedDiagram:
    """Build VerifiedDiagram from compiled RenderScene (canonical ids).

    `target` is the block rect the figure is projected into (the layout engine's
    placed rect, or FIGURE_RECT_DEFAULT for the legacy full-zone diagram).
    """
    canonical = _canonical_id_map(scene.entities)

    def qual(llm_id: str) -> str:
        return _qualify(llm_id, canonical, namespace)

    all_commands, anchors, _preferred_aspect = project_scene_to_commands(scene, namespace="", target=target)

    def _remap_command(cmd: DiagramCommand) -> DiagramCommand:
        update: dict[str, Any] = {}
        if cmd.anchor_id and cmd.anchor_id in canonical:
            update["anchor_id"] = qual(cmd.anchor_id)
        if cmd.semantic_ref and cmd.semantic_ref.entity_id and cmd.semantic_ref.entity_id in canonical:
            update["semantic_ref"] = cmd.semantic_ref.model_copy(
                update={"entity_id": qual(cmd.semantic_ref.entity_id)})
        return cmd.model_copy(update=update) if update else cmd

    all_commands = [_remap_command(c) for c in all_commands]
    anchors = [a.model_copy(update={"id": qual(a.id)}) if a.id in canonical else a for a in anchors]

    # Derived quantity IDs from plan
    derived_q_ids = set()
    quantity_lookup: dict[str, Any] = {}
    if plan:
        for q in plan.derived:
            derived_q_ids.add(q.id)
            quantity_lookup[q.id] = q
        for q in plan.givens:
            quantity_lookup[q.id] = q

    # Annotations: GIVEN measurements are drawn immediately; annotations bound to a DERIVED
    # quantity are withheld until [ANNOTATE:id].
    deferred_annotations: list[DeferredAnnotation] = []
    annotation_commands: list[DiagramCommand] = []
    anchor_by_id = {a.id: a for a in anchors}
    placed: list[tuple[float, float]] = [(c.params[0], c.params[1]) for c in all_commands if c.type == "LABEL"]

    def _ann_text(ann: Any) -> str | None:
        if ann.text:
            return ann.text
        q = quantity_lookup.get(ann.quantity_id) if ann.quantity_id else None
        if q is not None and q.value is not None:
            unit = f" {q.unit}" if q.unit else ""
            sym = f"{q.symbol} = " if q.symbol else ""
            return f"{sym}{q.value:g}{unit}"
        return None

    def _ann_command(ann: Any, target: str) -> DiagramCommand | None:
        text = _ann_text(ann)
        if not text:
            return None
        if not is_figure_label_text(text):
            # Derivation sentences and relation statements ("In triangle ABE, DF || AE
            # gives BF/FE = BD/DA", "B, F, E, C are collinear") belong on the work column;
            # the figure keeps short measurements only.
            log_event("figure_annotation_skipped", target=target, text=text[:80])
            return None
        # Prevent duplicate labels: if all_commands (from project.py) already has a LABEL
        # command for this target with the exact same text, skip creating a duplicate.
        if any(c.type == "LABEL" and c.anchor_id == target and c.text == text for c in all_commands):
            return None
        anchor = anchor_by_id.get(target)
        if anchor is not None:
            lx = anchor.x + anchor.width / 2.0 + 6.0
            ly = anchor.y + anchor.height / 2.0 + 6.0
        else:
            lx, ly = 440.0, 60.0 + 22.0 * len(annotation_commands)
        for _ in range(6):                               # avoid sitting on an existing label
            if all(abs(lx - px) > 30 or abs(ly - py) > 16 for px, py in placed):
                break
            ly += 20.0
        placed.append((lx, ly))
        return DiagramCommand(type="LABEL", params=[round(lx, 2), round(min(ly, 630.0), 2), 16.0],
                              text=text, anchor_id=target,
                              semantic_ref=SemanticRef(entity_id=target))

    if scene.doc:
        for ann in scene.doc.annotations:
            if ann.kind == "caption":
                continue
            target = qual(ann.target_ids[0]) if ann.target_ids else ""
            cmd = _ann_command(ann, target)
            if ann.quantity_id and ann.quantity_id in derived_q_ids:
                deferred_annotations.append(DeferredAnnotation(entity_id=target, commands=[cmd] if cmd else []))
            elif cmd is not None:
                annotation_commands.append(cmd)

    deferred_target_ids = {da.entity_id for da in deferred_annotations}
    immediate_commands: list[DiagramCommand] = []
    for cmd in all_commands:
        # withhold a DIMENSION that states a derived answer; never withhold name labels
        if cmd.anchor_id in deferred_target_ids and cmd.type == "DRAW_DIMENSION":
            continue
        immediate_commands.append(cmd)
    immediate_commands.extend(annotation_commands)

    # Build reveals (membership by canonical ids)
    reveals: list[DiagramReveal] = []
    if scene.doc:
        for rg in scene.doc.reveal_groups:
            prefixed_r_ids = {qual(eid) for eid in rg.entity_ids}
            cmd_indices = [
                i for i, c in enumerate(immediate_commands)
                if c.semantic_ref and c.semantic_ref.entity_id in prefixed_r_ids
            ]
            reveals.append(
                DiagramReveal(
                    narration=rg.label or f"Reveal group {rg.id}",
                    command_indices=cmd_indices,
                    kind="reveal",
                    target_id=f"{namespace}{rg.id}",
                )
            )

    # Figure name
    figure_name = "diagram_figure"
    if scene.doc and scene.doc.entities:
        kinds = [e.kind for e in scene.doc.entities if e.kind]
        if kinds:
            # Snake case of first significant kind
            raw_kind = kinds[0]
            clean_kind = re.sub(r"[^a-zA-Z0-9]+", "_", raw_kind).strip("_").lower()
            figure_name = f"{clean_kind}_figure"

    # Caption
    caption_text: str | None = None
    if scene.doc:
        for ann in scene.doc.annotations:
            if ann.kind == "caption" and ann.text:
                caption_text = ann.text
                break

    # Label glossary (keys use canonical ids)
    glossary: dict[str, LabelFact] = {}
    if scene.doc:
        for s_ent in scene.doc.entities:
            if s_ent.label:
                prefixed_id = qual(s_ent.id)
                generic = (s_ent.kind or "").lower() in ("", "other", "entity", "object")
                title = s_ent.label if generic else f"{s_ent.kind} {s_ent.label}"
                q_val_str = None
                prov = None
                # Check if matches any quantity
                for q in quantity_lookup.values():
                    if (q.symbol == s_ent.label or q.id == s_ent.id) and q.value is not None:
                        unit_str = f" {q.unit}" if q.unit else ""
                        q_val_str = f"{q.value:g}{unit_str}"
                        prov = q.provenance
                        break
                glossary[prefixed_id] = LabelFact(
                    symbol=s_ent.label,
                    title=title,
                    value=q_val_str,
                    provenance=prov,
                )

    # Human descriptions per anchor so the teaching LLM can pick the right [FOCUS] id.
    # ("tri: tri" / "mk: mk" tells it nothing; "seg_AB: side AB (AB = 5 cm)" does).

    def _pt_name(pt: Any) -> str:
        return getattr(pt, "label", None) or getattr(pt, "id", "?")

    def _describe(ent: Any) -> str | None:
        if isinstance(ent, Point):
            return f"point {_pt_name(ent)}"
        if isinstance(ent, Segment):
            return f"side {_pt_name(ent.p1)}{_pt_name(ent.p2)}"
        if isinstance(ent, Polygon):
            names = "".join(_pt_name(v) for v in ent.vertices)
            kind = {3: "triangle", 4: "quadrilateral"}.get(len(ent.vertices), "polygon")
            return f"{kind} {names}"
        if isinstance(ent, AngleMark):
            n = f"{_pt_name(ent.p1)}{_pt_name(ent.vertex)}{_pt_name(ent.p2)}"
            return f"right angle {n}" if ent.is_right_angle else f"angle {n}"
        if isinstance(ent, Circle):
            return f"circle with centre {_pt_name(ent.center)}"
        if isinstance(ent, (Line, Ray)):
            return "line"
        return None

    desc_by_anchor: dict[str, str] = {}
    for ent_id, ent in scene.entities.items():
        d = _describe(ent)
        if d:
            desc_by_anchor[qual(ent_id)] = d
    value_by_anchor: dict[str, str] = {}
    if scene.doc:
        for ann in scene.doc.annotations:
            if ann.target_ids and ann.quantity_id and ann.quantity_id not in derived_q_ids:
                t = _ann_text(ann)
                if t:
                    value_by_anchor[qual(ann.target_ids[0])] = t

    # Prompt addon (canonical ids)
    addon_lines = [f"figure: {figure_name}", "parts you may [FOCUS] (use each id exactly):"]
    for anc in anchors:
        fact = glossary.get(anc.id)
        title = desc_by_anchor.get(anc.id) or (fact.title if fact else None) or ", ".join(anc.labels) or anc.id
        value = value_by_anchor.get(anc.id) or (f"{fact.symbol} = {fact.value}" if fact and fact.value else None)
        addon_lines.append(f"- {anc.id}: {title}" + (f" ({value})" if value else ""))

    if reveals:
        rg_ids = ", ".join(r.target_id for r in reveals if r.target_id)
        if rg_ids:
            addon_lines.append(f"reveal groups you may [FOCUS]: {rg_ids}")

    if deferred_annotations:
        addon_lines.append("withheld labels, reveal with [ANNOTATE:id] when you reach them:")
        for da in deferred_annotations:
            fact = glossary.get(da.entity_id)
            desc = desc_by_anchor.get(da.entity_id) or (fact.title if fact else da.entity_id)
            desc = f"length of {desc.replace('side ', '')}" if desc.startswith("side ") else desc
            addon_lines.append(f"- {da.entity_id}: {desc}")

    prompt_addon = "\n".join(addon_lines)

    # Alias map: LLM id, entity label and reversed two-letter segment names resolve to
    # the canonical (namespaced) id; the first entity claiming a key wins. Entities without a
    # declared scene-doc label still get letter aliases derived from their labelled parts
    # (the same letters _canonical_id_map uses), so [FOCUS:AB] works for a constructed segment.
    declared_labels: dict[str, str] = {}
    if scene.doc:
        for s_ent in scene.doc.entities:
            if s_ent.label:
                declared_labels[s_ent.id] = s_ent.label

    def _derived_label(ent: Any) -> str | None:
        if isinstance(ent, Point):
            return _label_of(ent)
        if isinstance(ent, Segment):
            a, b = _label_of(ent.p1), _label_of(ent.p2)
            return f"{a}{b}" if a and b else None
        if isinstance(ent, Line):
            a, b = _label_of(ent.p1), _label_of(ent.p2)
            return f"{a}{b}" if a and b else None
        if isinstance(ent, Ray):
            a, b = _label_of(ent.origin), _label_of(ent.through)
            return f"{a}{b}" if a and b else None
        if isinstance(ent, Polygon):
            names = [_label_of(v) for v in ent.vertices]
            return "".join(names) if all(names) else None
        if isinstance(ent, AngleMark):
            a, v, b = _label_of(ent.p1), _label_of(ent.vertex), _label_of(ent.p2)
            return f"{a}{v}{b}" if a and v and b else None
        if isinstance(ent, Circle):
            return _label_of(ent.center)
        return None

    alias_map: dict[str, str] = {}
    for ent_id, ent in scene.entities.items():
        alias_map.setdefault(ent_id, qual(ent_id))
        label = declared_labels.get(ent_id)
        if not label:
            label = _derived_label(ent)
        if label:
            alias_map.setdefault(label, qual(ent_id))
            if isinstance(ent, Segment) and len(label) == 2 and label[0] != label[1]:
                alias_map.setdefault(label[::-1], qual(ent_id))
            # The teaching prompt names parts tri_ABC / seg_AB / ang_ABC / pt_A, the
            # scene may call them poly1 / sAB / segment_CA; these aliases bridge both.
            for alias in _canonical_aliases(ent, label):
                alias_map.setdefault(alias, qual(ent_id))

    return VerifiedDiagram(
        id="verified_scene",
        name=figure_name,
        commands=immediate_commands,
        anchors=anchors,
        reveals=reveals,
        prompt_addon=prompt_addon,
        caption=caption_text,
        deferred_annotations=deferred_annotations if deferred_annotations else None,
        label_glossary=glossary if glossary else None,
        alias_map=alias_map if alias_map else None,
        namespace=namespace,
    )


def prefix_block_ids(diagram: VerifiedDiagram, block_id: str) -> VerifiedDiagram:
    """A non-first figure block whose ids collide with an earlier block gets every
    id prefixed `{block_id}.`, so the ids stay unique by construction. Sticky blocks keep the
    ids they were introduced with, so they are never passed through here.
    """
    prefix = f"{block_id}."

    def pfx(v: str) -> str:
        return f"{prefix}{v}"

    def _cmd(c: DiagramCommand) -> DiagramCommand:
        update: dict[str, Any] = {}
        if c.anchor_id:
            update["anchor_id"] = pfx(c.anchor_id)
        if c.semantic_ref and c.semantic_ref.entity_id:
            update["semantic_ref"] = c.semantic_ref.model_copy(
                update={"entity_id": pfx(c.semantic_ref.entity_id)})
        return c.model_copy(update=update) if update else c

    anchors = [a.model_copy(update={"id": pfx(a.id)}) for a in diagram.anchors]
    reveals = [r.model_copy(update={"target_id": pfx(r.target_id)}) for r in diagram.reveals]
    deferred = (
        [da.model_copy(update={"entity_id": pfx(da.entity_id), "commands": [_cmd(c) for c in da.commands]})
         for da in diagram.deferred_annotations]
        if diagram.deferred_annotations else None
    )
    glossary = {pfx(k): v for k, v in (diagram.label_glossary or {}).items()} or None
    aliases = {k: pfx(v) for k, v in (diagram.alias_map or {}).items()} or None
    prompt = diagram.prompt_addon
    for old in sorted({a.id for a in diagram.anchors} | {r.target_id for r in diagram.reveals},
                      key=len, reverse=True):
        prompt = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", pfx(old), prompt)
    return diagram.model_copy(update={
        "commands": [_cmd(c) for c in diagram.commands],
        "anchors": anchors,
        "reveals": reveals,
        "deferred_annotations": deferred,
        "label_glossary": glossary,
        "alias_map": aliases,
        "prompt_addon": prompt,
    })

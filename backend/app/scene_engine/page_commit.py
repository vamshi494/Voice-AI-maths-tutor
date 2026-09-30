# app/scene_engine/page_commit.py
"""Page commit builder: layout + compiled scenes -> wire PageCommit."""
from app.contracts.messages import Block, PageCommit, Rect
from app.contracts.turn_plan import TurnPlan
from app.scene_engine.compile import RenderScene
from app.scene_engine.layout import PageLayout
from app.scene_engine.verified_diagram import build_verified_diagram, prefix_block_ids


def build_page_commit(
    turn_id: str,
    page_id: str,
    commit_id: str,
    layout: PageLayout,
    figures: dict[str, tuple[RenderScene, TurnPlan | None, str]],
    texts: dict[str, list[str]],
    revealed: dict[str, list[str]],
    carried_ids: list[str],
) -> PageCommit:
    """Build the wire PageCommit for a computed layout.

    `figures` maps a figure block's id to `(RenderScene, TurnPlan, namespace)`; `texts` maps
    a table/text block's id to its `textLines`; `revealed` maps block id -> already-shown
    reveal-group / deferred-annotation ids; `carried_ids` are sticky block ids carried from
    an earlier page. `generation` is left 0 — the sender re-stamps it (transport stamps
    seq/epoch), so callers `model_copy(update={"generation": g})` before publishing.

    Figure blocks keep bare canonical ids except when they collide with an earlier block's
    ids, in which case every id gains the `{block_id}.` prefix.
    """
    blocks: list[Block] = []
    taken_ids: set[str] = set()
    for placed in layout.blocks:
        rect = Rect(x=placed.rect.x, y=placed.rect.y, width=placed.rect.width, height=placed.rect.height)
        revealed_ids = revealed.get(placed.id, [])
        if placed.role == "figure":
            scene, plan, namespace = figures[placed.id]
            diagram = build_verified_diagram(scene, plan, namespace, target=placed.rect)
            if taken_ids & {a.id for a in diagram.anchors}:
                diagram = prefix_block_ids(diagram, placed.id)
            taken_ids |= {a.id for a in diagram.anchors}
            blocks.append(Block(
                id=placed.id,
                role="figure",
                rect=rect,
                sticky=placed.sticky,
                commands=diagram.commands,
                anchors=diagram.anchors,
                reveals=diagram.reveals,
                deferred_annotations=diagram.deferred_annotations,
                label_glossary=diagram.label_glossary,
                alias_map=diagram.alias_map,
                namespace=diagram.namespace,
                revealed_ids=revealed_ids,
            ))
        else:
            blocks.append(Block(
                id=placed.id,
                role=placed.role,
                rect=rect,
                sticky=placed.sticky,
                text_lines=texts.get(placed.id),
                revealed_ids=revealed_ids,
            ))

    return PageCommit(
        generation=0,   # the sender re-stamps the real generation before publishing
        turn_id=turn_id,
        page_id=page_id,
        commit_id=commit_id,
        work_rect=(Rect(x=layout.work_rect.x, y=layout.work_rect.y,
                        width=layout.work_rect.width, height=layout.work_rect.height)
                   if layout.work_rect else None),
        blocks=blocks,
        # Only ids that actually rendered count as carried; an outline drift (a sticky
        # that failed to compile) must not claim a block the client will not see.
        carried_ids=[cid for cid in carried_ids if cid in {b.id for b in blocks}],
    )

# backend/tests/test_layout_engine.py
"""Layout engine and cross-block id prefixing tests."""
from app.contracts.scene import SceneDocument
from app.scene_engine.compile import compile_scene_document
from app.scene_engine.layout import (
    BlockSpec,
    PageIntent,
    Rect,
    compute_layout,
    estimate_rows,
    fit,
)
from app.scene_engine.verified_diagram import build_verified_diagram, prefix_block_ids
from tests.make_frontend_fixture import PLAN, SCENES


def _diagram(name: str = "triangle_and_circle"):
    scene, report = compile_scene_document(SceneDocument.model_validate(SCENES[name]), plan=PLAN)
    assert report.valid, [e.message for e in report.errors]
    return build_verified_diagram(scene, plan=PLAN)


def test_canonical_id_collision_prefixed():
    """A non-first figure block whose ids collide with an earlier block gets every id
    prefixed `{block_id}.`; the first block keeps bare ids."""
    first = _diagram()
    second = _diagram()

    taken = {a.id for a in first.anchors}
    assert taken & {a.id for a in second.anchors}, "same scene must produce colliding ids"

    prefixed = prefix_block_ids(second, "fig2")
    ids = {a.id for a in prefixed.anchors}
    assert all(i.startswith("fig2.") for i in ids)
    assert taken.isdisjoint(ids)

    for cmd in prefixed.commands:
        if cmd.anchor_id:
            assert cmd.anchor_id.startswith("fig2.")
        if cmd.semantic_ref and cmd.semantic_ref.entity_id:
            assert cmd.semantic_ref.entity_id.startswith("fig2.")
    for r in prefixed.reveals:
        assert r.target_id.startswith("fig2.")
    if prefixed.deferred_annotations:
        for da in prefixed.deferred_annotations:
            assert da.entity_id.startswith("fig2.")
    assert all(k.startswith("fig2.") for k in (prefixed.label_glossary or {}))
    assert all(v.startswith("fig2.") for v in (prefixed.alias_map or {}).values())
    assert "fig2.seg_AB" in prefixed.prompt_addon

    # the first block is untouched
    assert all(not a.id.startswith("fig2.") for a in first.anchors)


def _figures(n: int, aspect: float = 1.0) -> list[BlockSpec]:
    return [BlockSpec(id=f"fig{i}", role="figure", preferred_aspect=aspect) for i in range(n)]


def test_one_figure_full_region():
    layout = compute_layout(PageIntent(has_work=True, blocks=_figures(1)))
    assert layout.work_rect == Rect(40, 72, 340, 608)
    assert len(layout.blocks) == 1 and not layout.overflow
    assert layout.blocks[0].rect == Rect(444, 64, 672, 552)


def test_two_side_by_side():
    layout = compute_layout(PageIntent(has_work=True, blocks=_figures(2), hint="side_by_side"))
    rects = [b.rect for b in layout.blocks]
    assert len(rects) == 2 and not layout.overflow
    assert [r.width for r in rects] == [324.0, 324.0]
    assert [r.height for r in rects] == [552.0, 552.0]
    assert (rects[0].x, rects[0].y) == (444.0, 64.0)
    assert (rects[1].x, rects[1].y) == (792.0, 64.0)


def test_two_stacked_aspect_clamped():
    layout = compute_layout(PageIntent(has_work=True, blocks=_figures(2, aspect=2.0), hint="stacked"))
    rects = [b.rect for b in layout.blocks]
    assert rects[0] == Rect(516, 64, 528, 264)
    assert rects[1] == Rect(516, 352, 528, 264)


def test_three_is_2x2_centered_last():
    layout = compute_layout(PageIntent(has_work=True, blocks=_figures(3)))
    rects = [b.rect for b in layout.blocks]
    assert rects[0] == Rect(444, 64, 324, 264)
    assert rects[1] == Rect(792, 64, 324, 264)
    assert rects[2] == Rect(618, 352, 324, 264)


def test_four_grid():
    layout = compute_layout(PageIntent(has_work=True, blocks=_figures(4)))
    rects = [b.rect for b in layout.blocks]
    assert rects[0] == Rect(444, 64, 324, 264)
    assert rects[1] == Rect(792, 64, 324, 264)
    assert rects[2] == Rect(444, 352, 324, 264)
    assert rects[3] == Rect(792, 352, 324, 264)
    assert layout.overflow == []


def test_five_overflows_one():
    blocks = _figures(5)
    layout = compute_layout(PageIntent(has_work=True, blocks=blocks))
    assert len(layout.blocks) == 4
    assert [b.id for b in layout.overflow] == ["fig4"]


def test_figure_only_full_width():
    layout = compute_layout(PageIntent(has_work=False, blocks=_figures(1, aspect=1.9)))
    assert layout.work_rect is None
    assert layout.blocks[0].rect == Rect(64, 64, 1052, 552)


def test_text_only_work_rect():
    layout = compute_layout(PageIntent(has_work=True, blocks=[]))
    assert layout.work_rect == Rect(40, 72, 1100, 608)
    assert layout.blocks == [] and layout.overflow == []


def test_estimate_rows():
    assert estimate_rows(Rect(40, 72, 340, 608)) == 13
    assert estimate_rows(Rect(40, 72, 1100, 608)) == 39
    assert estimate_rows(None) == 0


def test_deterministic():
    intent = PageIntent(has_work=True, blocks=_figures(3) + [BlockSpec(id="tbl", role="table", preferred_aspect=2.0)])
    assert compute_layout(intent) == compute_layout(intent)


def test_preferred_aspects():
    """Table aspect = (cols*110)/(rows*36); text = (max_line_chars*11+32)/(lines*38+24)."""
    from app.scene_engine.layout import table_aspect, text_aspect

    assert table_aspect(["Side | Length", "AD | 1.5 cm", "DB | 3 cm"]) == (2 * 110) / (3 * 36)
    assert table_aspect([]) == (1 * 110) / (1 * 36)
    assert text_aspect(["a² + b² = c²", "Pythagoras"]) == (12 * 11 + 32) / (2 * 38 + 24)
    assert text_aspect([]) == (0 * 11 + 32) / (1 * 38 + 24)


# ---------------------------------------------------------------------------------------------
# Sticky carry (reflow, stable ids, carried_ids)
# ---------------------------------------------------------------------------------------------
from app.scene_engine.page_commit import build_page_commit       # noqa: E402
from app.scene_engine.project import project_scene_to_commands  # noqa: E402


def _fixture_render_scene(name: str = "triangle_and_circle"):
    scene, report = compile_scene_document(SceneDocument.model_validate(SCENES[name]), plan=PLAN)
    assert report.valid, [e.message for e in report.errors]
    return scene


def _sticky_layouts():
    scene = _fixture_render_scene()
    aspect = project_scene_to_commands(scene, namespace="")[2]
    sticky = BlockSpec(id="fig_tri", role="figure", preferred_aspect=aspect, sticky=True)
    own = BlockSpec(id="fig_new", role="figure", preferred_aspect=aspect)
    p0 = compute_layout(PageIntent(has_work=True, blocks=[sticky]))
    p1 = compute_layout(PageIntent(has_work=True, hint="side_by_side",
                                   blocks=[sticky, own]))
    return scene, aspect, p0, p1


def test_sticky_block_reflowed():
    """The same sticky scene is re-laid-out on the next page (new rect, same content)."""
    scene, aspect, p0, p1 = _sticky_layouts()
    c0 = build_page_commit("t", "L_x_p0", "c0", p0, {"fig_tri": (scene, PLAN, "pg0_")},
                           {}, {}, [])
    c1 = build_page_commit("t", "L_x_p1", "c1", p1,
                           {"fig_tri": (scene, PLAN, "pg0_"), "fig_new": (scene, PLAN, "pg1_")},
                           {}, {}, ["fig_tri"])
    b0 = c0.blocks[0]
    b1 = next(b for b in c1.blocks if b.id == "fig_tri")
    assert (b0.rect.x, b0.rect.y, b0.rect.width, b0.rect.height) == (444.0, 64.0, 672.0, 552.0)
    assert b1.rect != b0.rect and b1.rect.width < b0.rect.width
    assert b1.sticky is True
    assert len(b1.commands) == len(b0.commands) > 0
    assert c1.carried_ids == ["fig_tri"]
    assert b1.revealed_ids == []

    # an unknown carried id is dropped (a sticky that never compiled must not be claimed)
    own_only = compute_layout(PageIntent(has_work=True,
                                         blocks=[BlockSpec(id="fig_new", role="figure",
                                                           preferred_aspect=aspect)]))
    c2 = build_page_commit("t", "L_x_p1", "c2", own_only,
                           {"fig_new": (scene, PLAN, "pg1_")}, {}, {}, ["fig_tri"])
    assert c2.carried_ids == []


def test_sticky_anchor_ids_stable():
    """Same block id, same canonical anchor ids: each page's figures carry their own namespace,
    so the carried sticky keeps page 0's ids and never collides with the page's own block."""
    scene, _aspect, p0, p1 = _sticky_layouts()
    c0 = build_page_commit("t", "L_x_p0", "c0", p0, {"fig_tri": (scene, PLAN, "pg0_")},
                           {}, {}, [])
    c1 = build_page_commit("t", "L_x_p1", "c1", p1,
                           {"fig_tri": (scene, PLAN, "pg0_"), "fig_new": (scene, PLAN, "pg1_")},
                           {}, {}, ["fig_tri"])
    ids0 = {a.id for a in c0.blocks[0].anchors}
    ids1 = {a.id for a in next(b for b in c1.blocks if b.id == "fig_tri").anchors}
    assert ids0 and ids0 == ids1, "the carried sticky's anchor ids never change"
    assert all(i.startswith("pg0_") for i in ids1)
    new_ids = {a.id for a in next(b for b in c1.blocks if b.id == "fig_new").anchors}
    assert new_ids and all(i.startswith("pg1_") for i in new_ids)
    assert not (new_ids & ids0)

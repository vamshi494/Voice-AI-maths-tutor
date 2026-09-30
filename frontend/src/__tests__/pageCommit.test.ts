// frontend/src/__tests__/pageCommit.test.ts
// The client renders REAL backend PageCommit events (fixtures/page_commits.json,
// regenerate with `backend/tests/make_frontend_fixture.py --pages`). commitPage places each
// block in its server-computed rect, sets the work columns, replaces recommitted blocks,
// isolates render failures per block, reflows marking targets and removes blocks that left
// the page.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import Konva from 'konva';
import gsap from 'gsap';
import commits from './fixtures/page_commits.json';
import { CommandExecutor, BLOCK_FADE_MS } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { exportLessonNotesPdf } from '../whiteboard/export';
import { PageCommitEvent, SnapshotPage } from '../types/events';

// jsPDF draws onto instance methods, so the export tests observe a fake pdf instead of
// spying on its prototype.
vi.mock('jspdf', () => {
  const instances: any[] = [];
  class FakeJsPDF {
    pages = 1;
    images = 0;
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    constructor(..._args: any[]) { instances.push(this); }
    addPage() { this.pages += 1; return this; }
    addImage() { this.images += 1; return this; }
    output() { return new Blob(['pdf'], { type: 'application/pdf' }); }
  }
  return { default: FakeJsPDF, __instances: instances };
});
// eslint-disable-next-line @typescript-eslint/no-var-requires
import * as jspdfModule from 'jspdf';
const pdfInstances: any[] = (jspdfModule as any).__instances ?? [];

const events = commits as unknown as PageCommitEvent[];

function byId(commitId: string): PageCommitEvent {
  const evt = events.find((e) => e.commitId === commitId);
  if (!evt) throw new Error(`fixture case ${commitId} missing`);
  return evt;
}

function setup() {
  const layout = new BoardLayout('p1');
  const txManager = new TransactionManager();
  const stage = new Konva.Stage({ container: document.createElement('div'), width: 1200, height: 700 });
  const drawLayer = new Konva.Layer({ listening: false });
  const animLayer = new Konva.Layer({ listening: false });
  const spotlightLayer = new Konva.Layer({ listening: false });
  stage.add(drawLayer, animLayer, spotlightLayer);
  txManager.setLayers(animLayer, drawLayer);
  const reports: any[] = [];
  const executor = new CommandExecutor({
    layout, txManager, drawLayer, animLayer, spotlightLayer,
    highlightLayer: new Konva.Layer({ listening: false }),
    cursorLayer: new Konva.Layer({ listening: false }),
    onSendReport: (r) => reports.push(r),
  });
  return { executor, layout, drawLayer, animLayer, spotlightLayer, reports };
}

const countShapes = (node: Konva.Container) => node.find('Shape').length;

describe('commitPage', () => {
  beforeAll(() => {
    const ctx = new Proxy({}, {
      get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
      set: () => true,
    });
    HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
  });

  it('renders_blocks_in_rects', () => {
    const { executor, drawLayer } = setup();
    const evt = byId('c_p1_2');   // two side-by-side figure blocks, 324x552 each
    executor.commitPage(evt, true);
    for (const block of evt.blocks) {
      const group = drawLayer.findOne(`.block_${block.id}`) as Konva.Group;
      expect(group, block.id).toBeTruthy();
      expect(countShapes(group)).toBeGreaterThan(0);
      for (const shape of group.find('Shape')) {
        const pts: number[][] = shape instanceof Konva.Line || shape instanceof Konva.Arrow
          ? (shape.points() as number[]).reduce<number[][]>((a, _, i, arr) =>
              (i % 2 === 0 ? [...a, [arr[i], arr[i + 1]]] : a), [])
          : [[shape.x(), shape.y()]];
        for (const [x, y] of pts) {
          expect(x, `${block.id} x`).toBeGreaterThanOrEqual(block.rect.x - 0.5);
          expect(x, `${block.id} x`).toBeLessThanOrEqual(block.rect.x + block.rect.width + 0.5);
          expect(y, `${block.id} y`).toBeGreaterThanOrEqual(block.rect.y - 0.5);
          expect(y, `${block.id} y`).toBeLessThanOrEqual(block.rect.y + block.rect.height + 0.5);
        }
      }
    }
  });

  it('work_rect_sets_columns', () => {
    const { executor, layout } = setup();
    executor.commitPage(byId('c_p1_6'), true);   // 340-wide work rect
    expect(layout.getLayoutMode()).toBe('DIAGRAM_STANDARD_1COL');

    const wide = { ...byId('c_p1_6'), commitId: 'c_p1_6b', workRect: { x: 40, y: 72, width: 1100, height: 608 } };
    executor.commitPage(wide, true);             // text-only width -> 3 columns
    expect(layout.getLayoutMode()).toBe('TEXT_ONLY_3COL');
    expect(layout.getDividers()).toEqual([395, 785]);
  });

  it('recommit_same_block_replaces', () => {
    const { executor, drawLayer } = setup();
    const first = byId('c_p1_1');    // one figure block `fig_tri`
    executor.commitPage(first, true);
    const group = drawLayer.findOne('.block_fig_tri') as Konva.Group;
    const before = countShapes(group);

    // A later commit of the same page keeps the block but replaces its contents.
    const second: PageCommitEvent = {
      ...first,
      commitId: 'c_p1_1b',
      blocks: [{
        ...first.blocks[0],
        rect: { x: 444, y: 64, width: 324, height: 264 },
        commands: [first.blocks[0].commands![0], first.blocks[0].commands![1]],
      }],
    };
    executor.commitPage(second, true);
    expect(drawLayer.find('.block_fig_tri').length).toBe(1);
    const after = countShapes(drawLayer.findOne('.block_fig_tri') as Konva.Group);
    expect(after).toBe(2);
    expect(after).not.toBe(before);
  });

  it('duplicate_commit_is_idempotent', () => {
    // A re-sent page_commit (same pageId/commitId — resync, replay) must not re-render:
    // re-rendering destroys the staged hidden groups and dumps the whole figure at opacity 1.
    const { executor, animLayer, drawLayer } = setup();
    const evt = byId('c_p1_1');
    executor.commitPage(evt, false);
    const container = animLayer.findOne('.block_fig_tri') as Konva.Group;
    const before = container.getChildren().map((c) => c.name()).sort();
    expect(before.length).toBeGreaterThan(0);
    expect(container.getChildren().every((c) => c.opacity() === 0)).toBe(true);

    executor.commitPage(evt, false);   // duplicate

    expect(container.getChildren().map((c) => c.name()).sort()).toEqual(before);
    expect(container.getChildren().every((c) => c.opacity() === 0)).toBe(true);
    expect(drawLayer.findOne('.block_fig_tri')).toBeFalsy();   // never dumped to drawLayer
  });

  it('recommit_restages_instead_of_instant_dump', () => {
    // A resume re-activation commits the same page again with a fresh commitId: the block is
    // re-staged hidden for the new turn's speech, never drawn instantly.
    const { executor, animLayer } = setup();
    const first = byId('c_p1_1');
    executor.commitPage(first, false);
    executor.commitPage({ ...first, commitId: 'c_p1_1b' }, false);
    expect(animLayer.find('.block_fig_tri').length).toBe(1);
    const container = animLayer.findOne('.block_fig_tri') as Konva.Group;
    const children = container.getChildren();
    expect(children.length).toBeGreaterThan(0);
    expect(children.every((c) => c.opacity() === 0)).toBe(true);
  });

  it('staged_commit_renders_hidden_groups', () => {
    // The live non-instant path stages base + reveal groups hidden on the animLayer and
    // lets the reveal clock show them as the speech reaches them.
    const { executor, animLayer, drawLayer } = setup();
    const evt = byId('c_p1_1');   // figure with a reveal group
    executor.commitPage(evt, false);

    const container = animLayer.findOne('.block_fig_tri') as Konva.Group;
    expect(container).toBeTruthy();
    const names = container.getChildren().map((c) => c.name()).sort();
    expect(names).toContain('reveal_base');
    expect(names.some((n) => n.startsWith('reveal_') && n !== 'reveal_base')).toBe(true);
    expect(container.getChildren().every((c) => c.opacity() === 0)).toBe(true);
    expect(drawLayer.findOne('.block_fig_tri')).toBeFalsy();   // nothing on the draw layer yet

    // the first step start reveals the base (fade settled via gsap timeline progress)
    const step = { turnId: evt.turnId, generation: evt.generation, stepIndex: 0, spokenText: 'x', words: ['x'], ops: [] };
    executor.reportStarted(step);
    gsap.globalTimeline.progress(1);
    const base = container.findOne('.reveal_base') as Konva.Group;
    expect(base.opacity()).toBe(1);
  });

  it('all_commands_in_reveals_promotes_first_to_base', () => {
    const { executor, animLayer } = setup();
    // Simulate a block where all commands are covered by reveals (e.g. group_triangle + group_parallel)
    const evt: PageCommitEvent = {
      type: 'page_commit',
      turnId: 't1',
      generation: 1,
      pageId: 'p1',
      commitId: 'c1',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [
        {
          id: 'fig_all_reveals',
          role: 'figure',
          rect: { x: 400, y: 50, width: 350, height: 500 },
          commands: [
            { type: 'DRAW_LINE', params: [0, 0, 100, 100], anchorId: 'seg_AB' },
            { type: 'DRAW_LINE', params: [100, 100, 50, 0], anchorId: 'seg_BC' },
            { type: 'DRAW_POINT', params: [50, 50, 3], anchorId: 'pt_D' },
          ],
          anchors: [],
          reveals: [
            { targetId: 'rg_tri', commandIndices: [0, 1] },
            { targetId: 'rg_pt', commandIndices: [2] },
          ],
        },
      ],
    };

    executor.commitPage(evt, false);
    const container = animLayer.findOne('.block_fig_all_reveals') as Konva.Group;
    expect(container).toBeTruthy();
    // The first reveal group should have been promoted to base so it has reveal_base
    const base = container.findOne('.reveal_base') as Konva.Group;
    expect(base).toBeTruthy();
    expect(base.opacity()).toBe(0);

    // On step start, the promoted base figure reveals
    const step = { turnId: 't1', generation: 1, stepIndex: 0, spokenText: 'x', words: ['x'], ops: [] };
    executor.reportStarted(step);
    gsap.globalTimeline.progress(1);
    expect(base.opacity()).toBe(1);

    // The second reveal group (rg_pt) is still hidden until its entity or targetId is focused
    const ptGroup = container.findOne('.reveal_rg_pt') as Konva.Group;
    expect(ptGroup).toBeTruthy();
    expect(ptGroup.opacity()).toBe(0);

    // When the tutor focuses pt_D, rg_pt reveals
    const step2 = { turnId: 't1', generation: 1, stepIndex: 1, spokenText: 'look at D', words: ['look'], ops: [{ opId: 't1:1:0', kind: 'FOCUS', atWord: 0, entityId: 'pt_D' }] };
    executor.reportStarted(step2 as any);
    gsap.globalTimeline.progress(1);
    expect(ptGroup.opacity()).toBe(1);
  });

  it('render_error_isolated_per_block', () => {
    const { executor, drawLayer, reports } = setup();
    const evt = byId('c_p1_2');
    const broken: PageCommitEvent = {
      ...evt,
      blocks: [
        { ...evt.blocks[0], commands: [{ type: 'DRAW_LINE', params: 'not-an-array' } as any] },
        evt.blocks[1],
      ],
    };
    executor.commitPage(broken, true);

    const errors = reports.filter((r) => r.type === 'client_error');
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatchObject({ where: 'render', blockId: evt.blocks[0].id });
    // the healthy block still rendered
    expect(drawLayer.findOne(`.block_${evt.blocks[1].id}`)).toBeTruthy();
    expect(countShapes(drawLayer.findOne(`.block_${evt.blocks[1].id}`) as Konva.Group)).toBeGreaterThan(0);
  });

  it('marking_candidates_after_reflow', () => {
    const { executor, layout } = setup();
    const evt = byId('c_p1_2');
    executor.commitPage(evt, true);
    const expected = evt.blocks.reduce((n, b) => n + (b.anchors || []).length, 0);
    expect(executor.activeAnchors.length).toBe(expected);
    const diagramTargets = layout.getMarkingCandidates(executor.activeAnchors).filter((t) => t.kind === 'diagram');
    expect(diagramTargets.length).toBe(expected);
  });

  it('multi_figure_focus_highlights', () => {
    // [FOCUS] on any anchor of a multi-figure page must still highlight (the aggregate
    // activeDiagram covers every figure block's commands and anchors).
    const { executor, spotlightLayer } = setup();
    const evt = byId('c_p1_2');    // two figure blocks g0 + g1
    executor.commitPage(evt, true);
    const anchor = evt.blocks[1].anchors![0];
    executor.executeOp({ opId: 't:0:0', kind: 'FOCUS', atWord: 0, entityId: anchor.id }, 1, 't');
    const groups = spotlightLayer.getChildren().filter((n) => n.name() === 'focus_group') as Konva.Group[];
    expect(groups.length).toBeGreaterThan(0);
    expect(groups[0].getChildren().length).toBeGreaterThan(0);
    // commands of both blocks are visible to the focus path
    const allCommands = executor.activeDiagram?.commands ?? [];
    expect(allCommands.length).toBeGreaterThan(evt.blocks[0].commands!.length);
  });

  it('multi_figure_reveal_focus_uses_own_block_commands', () => {
    // A non-first block's reveal group must highlight ITS OWN commands (offset
    // commandIndices against the flattened aggregate), never the first block's.
    const { executor, spotlightLayer } = setup();
    const evt: PageCommitEvent = {
      type: 'page_commit', generation: 1, turnId: 't', pageId: 'p1', commitId: 'c_multi',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [
        { id: 'fa', role: 'figure', rect: { x: 444, y: 64, width: 324, height: 552 },
          commands: [
            { type: 'DRAW_POINT', params: [100, 100, 3], anchorId: 'a1' },
            { type: 'DRAW_POINT', params: [110, 110, 3], anchorId: 'a2' },
          ], anchors: [], reveals: [] },
        { id: 'fb', role: 'figure', rect: { x: 792, y: 64, width: 324, height: 552 },
          commands: [
            { type: 'DRAW_POINT', params: [200, 200, 3], anchorId: 'b1' },
            { type: 'DRAW_POINT', params: [210, 210, 3], anchorId: 'b2' },
          ], anchors: [],
          reveals: [{ targetId: 'rg_b', commandIndices: [0, 1] }] },
      ],
    };
    executor.commitPage(evt, true);
    executor.executeOp({ opId: 't:0:0', kind: 'FOCUS', atWord: 0, entityId: 'rg_b' }, 1, 't');
    const groups = spotlightLayer.getChildren().filter((n) => n.name() === 'focus_group') as Konva.Group[];
    expect(groups.length).toBeGreaterThan(0);
    const circles = groups[0].find('Circle') as Konva.Circle[];
    expect(circles.length).toBe(2);
    expect(circles.map((c) => c.x()).sort((a, b) => a - b)).toEqual([200, 210]);
  });

  it('clear_diagram_unstages_and_drops_anchors', () => {
    const { executor, animLayer } = setup();
    const evt = byId('c_p1_2');
    executor.commitPage(evt, false);   // staged, hidden
    const anchorCount = executor.activeAnchors.length;
    expect(anchorCount).toBeGreaterThan(0);
    const container = animLayer.findOne('.block_g1') as Konva.Group;
    expect(container).toBeTruthy();

    executor.clearDiagram('g1');
    expect(animLayer.findOne('.block_g1')).toBeFalsy();
    expect(executor.activeAnchors.length).toBe(anchorCount - (evt.blocks[1].anchors || []).length);
    expect(executor.activeAnchors.some((a) => evt.blocks[1].anchors!.some((b) => b.id === a.id))).toBe(false);
    // a later step start must not animate the destroyed block's groups
    executor.reportStarted({ turnId: 't', generation: 1, stepIndex: 0, spokenText: 'x', words: ['x'], ops: [] });
    expect(executor.activeDiagram?.anchors.length).toBe(evt.blocks[0].anchors!.length);
  });

  it('clear_diagram_allows_recommit', () => {
    // Clearing all diagrams forgets the (pageId, commitId) identity, so an identical
    // re-commit afterwards renders instead of being dropped as a duplicate.
    const { executor, drawLayer } = setup();
    const evt = byId('c_p1_1');
    executor.commitPage(evt, true);
    executor.clearDiagram();
    executor.commitPage(evt, true);   // same (pageId, commitId) again
    expect(drawLayer.findOne('.block_fig_tri')).toBeTruthy();
  });

  it('non_sticky_block_removed', async () => {
    const { executor, drawLayer, animLayer } = setup();
    executor.commitPage(byId('c_p1_2'), true);    // blocks g0 + g1
    executor.commitPage(byId('c_p1_1'), true);    // only fig_tri stays
    await new Promise((r) => setTimeout(r, BLOCK_FADE_MS + 200));
    for (const id of ['g0', 'g1']) {
      expect(drawLayer.findOne(`.block_${id}`), id).toBeFalsy();
      expect(animLayer.findOne(`.block_${id}`), id).toBeFalsy();
    }
    expect(drawLayer.findOne('.block_fig_tri')).toBeTruthy();
  });

  it('null_work_rect_falls_back', () => {
    const { executor, layout } = setup();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const evt = { ...byId('c_p1_1'), workRect: null };
    executor.commitPage(evt, true);
    expect(warn).toHaveBeenCalledWith('[layout] null workRect');
    expect(layout.getLayoutMode()).toBe('DIAGRAM_STANDARD_1COL');
    warn.mockRestore();
  });

  it('table_block_renders_cells', () => {
    const { executor, drawLayer } = setup();
    const evt = byId('c_p1_6');   // table block: ["Side | Length", "AD | 1.5 cm", "DB | 3 cm"]
    executor.commitPage(evt, true);
    const group = drawLayer.findOne('.block_tbl_ratio') as Konva.Group;
    expect(group).toBeTruthy();

    const texts = group.find('Text') as Konva.Text[];
    expect(texts.map((t) => t.text())).toEqual(['Side', 'Length', 'AD', '1.5 cm', 'DB', '3 cm']);
    expect(texts.every((t) => t.fontSize() === 18)).toBe(true);
    // each cell spans its column width and sits 8 px below its row top
    const block = evt.blocks[0];
    const colW = block.rect.width / 2;
    expect(texts.every((t) => t.width() === colW)).toBe(true);
    expect(texts[0].x()).toBe(block.rect.x);
    expect(texts[0].y()).toBe(block.rect.y + 8);

    const lines = group.find('Line') as Konva.Line[];
    expect(lines.some((l) => l.strokeWidth() === 2)).toBe(true);       // header underline
    const rects = group.find('Rect') as Konva.Rect[];
    expect(rects.some((r) => r.strokeWidth() === 1.5)).toBe(true);     // outer border
  });

  it('table_shrinks_font_when_tall', () => {
    const { executor, drawLayer } = setup();
    const rows = Array.from({ length: 22 }, (_, i) => `R${i} | ${i * 2}`);
    const evt: PageCommitEvent = {
      type: 'page_commit', generation: 1, turnId: 't', pageId: 'p1', commitId: 'c_tall',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [{ id: 'tbl_tall', role: 'table', rect: { x: 444, y: 64, width: 672, height: 552 }, textLines: rows }],
    };
    executor.commitPage(evt, true);
    const group = drawLayer.findOne('.block_tbl_tall') as Konva.Group;

    const texts = group.find('Text') as Konva.Text[];
    // 22 rows at 36 px overflows 552 px -> 30 px rows; 18 fit, the last visible row is "…".
    expect(texts.length).toBe(17 * 2 + 1);
    expect(texts.every((t) => t.fontSize() === 16)).toBe(true);
    expect(texts.at(-1)!.text()).toBe('…');
    const rects = group.find('Rect') as Konva.Rect[];
    expect(rects.some((r) => r.strokeWidth() === 1.5 && r.height() === 18 * 30)).toBe(true);
  });

  it('text_block_boxed', () => {
    const { executor, drawLayer } = setup();
    const evt: PageCommitEvent = {
      type: 'page_commit', generation: 1, turnId: 't', pageId: 'p1', commitId: 'c_txt',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [{ id: 'txt_note', role: 'text', rect: { x: 444, y: 64, width: 300, height: 100 },
                 textLines: ['a² + b² = c²', 'Pythagoras'] }],
    };
    executor.commitPage(evt, true);
    const group = drawLayer.findOne('.block_txt_note') as Konva.Group;

    const texts = group.find('Text') as Konva.Text[];
    expect(texts.map((t) => t.text())).toEqual(['a² + b² = c²', 'Pythagoras']);
    expect(texts.every((t) => t.fontSize() === 24)).toBe(true);
    expect(texts[0].x()).toBe(444 + 12);              // 12 px padding
    expect(texts[0].y()).toBe(64 + 12);
    expect(texts[1].y()).toBe(64 + 12 + 38);          // 38 px line height
    const rects = group.find('Rect') as Konva.Rect[];
    expect(rects.some((r) => r.strokeWidth() === 1.5)).toBe(true);
  });

  it('text_block_wraps_long_lines', () => {
    // Long lines are clamped to the block's inner width and word-wrapped.
    const { executor, drawLayer } = setup();
    const evt: PageCommitEvent = {
      type: 'page_commit', generation: 1, turnId: 't', pageId: 'p1', commitId: 'c_txt2',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [{ id: 'txt_long', role: 'text', rect: { x: 444, y: 64, width: 200, height: 100 },
                 textLines: ['a very long sentence that cannot possibly fit on one line'] }],
    };
    executor.commitPage(evt, true);
    const group = drawLayer.findOne('.block_txt_long') as Konva.Group;
    const text = group.find('Text')[0] as Konva.Text;
    expect(text.width()).toBe(200 - 24);
    expect(text.wrap()).toBe('word');
  });

  it('text_block_clamps_tiny_rect_width', () => {
    // A degenerate rect never yields a negative or zero text width (zero would make
    // Konva drop the width constraint and overflow the line again).
    const { executor, drawLayer } = setup();
    const evt: PageCommitEvent = {
      type: 'page_commit', generation: 1, turnId: 't', pageId: 'p1', commitId: 'c_tiny',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [{ id: 'txt_tiny', role: 'text', rect: { x: 444, y: 64, width: 10, height: 100 },
                 textLines: ['x'] }],
    };
    executor.commitPage(evt, true);
    const group = drawLayer.findOne('.block_txt_tiny') as Konva.Group;
    const text = group.find('Text')[0] as Konva.Text;
    expect(text.width()).toBeGreaterThan(0);
    expect(text.wrap()).toBe('word');
  });
});

describe('sticky carry', () => {
  const stickyCommit = (commitId: string, pageId: string, rect: any, revealed: string[] = []): PageCommitEvent => ({
    type: 'page_commit', generation: 1, turnId: 't1', pageId, commitId,
    workRect: { x: 40, y: 72, width: 340, height: 608 },
    blocks: [{
      id: 'fig_tri', role: 'figure', sticky: true, rect,
      commands: [
        { type: 'DRAW_POINT', params: [500, 120, 4] },
        { type: 'DRAW_POINT', params: [700, 200, 4] },
      ],
      anchors: [{ id: 'pt_a', labels: ['A'], x: 496, y: 116, width: 8, height: 8 }],
      reveals: [{ targetId: 'rg_x', commandIndices: [1] }],
      revealedIds: revealed,
    }],
  });

  it('sticky_block_tweens_then_swaps', () => {
    const { executor, animLayer } = setup();
    executor.commitPage(stickyCommit('c0', 'p0', { x: 444, y: 64, width: 324, height: 264 }), false);
    const group = animLayer.findOne('.block_fig_tri') as Konva.Group;
    expect(group).toBeTruthy();
    expect(group.getChildren().length).toBeGreaterThan(0);

    executor.commitPage(
      stickyCommit('c1', 'p1', { x: 444, y: 64, width: 672, height: 552 }, ['rg_x']), false,
    );
    expect(animLayer.find('.block_fig_tri').length).toBe(1);   // same group, never duplicated
    expect(group.position().x).toBe(444);                      // the tween starts from the old rect
    expect(group.position().y).toBe(64);

    gsap.globalTimeline.progress(1);                           // settle -> swap in the new ink
    expect(group.x()).toBe(0);
    expect(group.y()).toBe(0);
    expect(group.scaleX()).toBe(1);
    const circles = group.find('Circle') as Konva.Circle[];
    expect(circles.length).toBe(2);
    expect(circles.every((c) => c.opacity() === 1)).toBe(true);
    expect(circles.map((c) => [Math.round(c.x()), Math.round(c.y())]))
      .toEqual([[500, 120], [700, 200]]);
    expect(executor.activeAnchors.map((a) => a.id)).toContain('pt_a');
  });

  it('page_advance_keeps_sticky', () => {
    const { executor, animLayer } = setup();
    executor.commitPage(stickyCommit('c0', 'p0', { x: 444, y: 64, width: 324, height: 264 }, ['rg_x']), false);
    executor.commitPage(stickyCommit('c1', 'p1', { x: 444, y: 64, width: 672, height: 552 }, ['rg_x']), false);
    gsap.globalTimeline.progress(1);
    // A second page advance tweens it again from the p1 rect.
    executor.commitPage(stickyCommit('c2', 'p2', { x: 444, y: 64, width: 324, height: 552 }, ['rg_x']), false);
    gsap.globalTimeline.progress(1);
    const group = animLayer.findOne('.block_fig_tri') as Konva.Group;
    expect(animLayer.find('.block_fig_tri').length).toBe(1);
    expect((group.find('Circle') as Konva.Circle[]).length).toBe(2);
    expect(group.scaleX()).toBe(1);
    expect(executor.activeAnchors.map((a) => a.id)).toEqual(['pt_a']);
  });

  it('sticky_unrevealed_groups_stay_hidden_until_revealed', () => {
    const { executor, animLayer } = setup();
    executor.commitPage(stickyCommit('c0', 'p0', { x: 444, y: 64, width: 324, height: 264 }), false);
    executor.commitPage(stickyCommit('c1', 'p1', { x: 444, y: 64, width: 672, height: 552 }), false);
    gsap.globalTimeline.progress(1);
    const group = animLayer.findOne('.block_fig_tri') as Konva.Group;
    const hidden = group.findOne('.reveal_rg_x') as Konva.Group;
    expect(hidden).toBeTruthy();
    expect(hidden.opacity()).toBe(0);

    executor.revealAll();
    gsap.globalTimeline.progress(1);
    expect(hidden.opacity()).toBe(1);
  });

  it('clear_board_mid_sticky_tween_does_not_throw', () => {
    const { executor, animLayer } = setup();
    executor.commitPage(stickyCommit('c0', 'p0', { x: 444, y: 64, width: 324, height: 264 }), false);
    executor.commitPage(stickyCommit('c1', 'p1', { x: 444, y: 64, width: 672, height: 552 }), false);
    expect(() => {
      executor.clearBoard(true);
      gsap.globalTimeline.progress(1);
    }).not.toThrow();
    expect(animLayer.find('.block_fig_tri').length).toBe(0);
  });
});

describe('notes read-only preview', () => {
  it('notes_view_does_not_block_live_ops', () => {
    // The drawer renders into its OWN stage/executor (NotesDrawer); the live board must keep
    // its blocks, anchors and op execution untouched by a preview render.
    const live = setup();
    const evt = byId('c_p1_7');
    live.executor.commitPage(evt, true);
    const liveShapes = countShapes(live.drawLayer);
    const liveAnchors = live.executor.activeAnchors.length;

    const notes = setup();
    notes.executor.commitPage(evt, true);
    const last = evt.blocks[evt.blocks.length - 1];
    for (const op of [{ opId: 't:0:0', kind: 'WRITE' as const, atWord: 0, text: 'note row', rowId: 'w1' }]) {
      notes.executor.executeOp(op, 1, 't', true);
    }

    expect(countShapes(live.drawLayer)).toBe(liveShapes);
    expect(live.executor.activeAnchors.length).toBe(liveAnchors);
    expect(live.executor.activeAnchors.map((a) => a.id))
      .toEqual(notes.executor.activeAnchors.slice(0, liveAnchors).map((a) => a.id));
    expect(last).toBeTruthy();

    // the live executor still executes live ops and reports board rows
    live.executor.executeOp({ opId: 't:0:1', kind: 'WRITE', atWord: 0, text: 'live row', rowId: 'w9' }, 1, 't');
    gsap.globalTimeline.progress(1);            // settle the write tween -> board_report
    expect(live.reports.some((r) => r.type === 'board_report')).toBe(true);
    expect(live.reports.some((r) => (r.rows || []).some((row: any) => row.text === 'note row')))
      .toBe(false);   // the preview's rows never leak into the live report stream
  });
});

describe('lesson notes export', () => {
  beforeAll(() => {
    const ctx = new Proxy({}, {
      get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
      set: () => true,
    });
    HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
  });

  const mockStage = () => {
    const toDataURL = vi.spyOn(Konva.Stage.prototype, 'toDataURL')
      .mockReturnValue('data:image/jpeg;base64,AAAA');
    return { toDataURL };
  };

  it('export_collects_pages', async () => {
    const { toDataURL } = mockStage();
    const commit = byId('c_p1_1');
    const pageA: SnapshotPage = {
      pageId: 'L_x_p0', title: 'A', commit, subPages: [{ subId: 'L_x_p0', ops: [] }],
    };
    const pageB: SnapshotPage = {
      pageId: 'L_x_p1', title: 'B', commit,
      subPages: [{ subId: 'L_x_p1', ops: [] }, { subId: 'L_x_p1_p2', ops: [] }],
    };
    const blob = await exportLessonNotesPdf([pageA, pageB], 'BPT');
    expect(blob).toBeInstanceOf(Blob);
    // one PDF page per sub-page, one rendered image each
    expect(pdfInstances[0].pages).toBe(3);
    expect(pdfInstances[0].images).toBe(3);
    expect(toDataURL).toHaveBeenCalledTimes(3);
    toDataURL.mockRestore();
  });

  it('export_renders_page_commit_blocks', async () => {
    const commitPage = vi.spyOn(CommandExecutor.prototype, 'commitPage');
    const commitDiagram = vi.spyOn(CommandExecutor.prototype, 'commitDiagram');
    const { toDataURL } = mockStage();
    const commit = byId('c_p1_2');       // two figure blocks
    const page: SnapshotPage = {
      pageId: 'L_x_p0', commit, subPages: [{ subId: 'L_x_p0', ops: [] }],
    };
    await exportLessonNotesPdf([page]);
    expect(commitPage).toHaveBeenCalledWith(commit, true);
    expect(commitDiagram).not.toHaveBeenCalled();
    const executor = commitPage.mock.instances[0] as unknown as CommandExecutor;
    expect(executor.activeAnchors.length).toBeGreaterThan(0);

    // legacy diagram fallback + a page with neither still yields a PDF page (never a crash)
    const diagram = byId('c_p1_1');
    await exportLessonNotesPdf([{
      pageId: 'legacy', diagram: diagram.blocks[0] as any, subPages: [{ subId: 'legacy', ops: [] }],
    }]);
    expect(commitDiagram).toHaveBeenCalled();
    const before = pdfInstances.length;
    await exportLessonNotesPdf([{ pageId: 'empty', subPages: [{ subId: 'empty', ops: [] }] }]);
    expect(pdfInstances[before].images).toBe(1);
    void toDataURL;
    commitPage.mockRestore();
    commitDiagram.mockRestore();
    toDataURL.mockRestore();
  });
});

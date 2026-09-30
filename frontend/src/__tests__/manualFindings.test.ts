// frontend/src/__tests__/manualFindings.test.ts
// Board regressions found in manual runs, driven through the real router + executor + layout
// (happy-dom Konva), not mocks.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import Konva from 'konva';
import { routeTutorEvent, RouterDeps } from '../app/eventRouter';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';

function rig() {
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
  const refs = { currentTurnId: { current: '' }, currentGeneration: { current: 0 } };
  const ui = {
    setThinking: vi.fn(), setLocked: vi.fn(), setHasDrawn: vi.fn(), setCanContinue: vi.fn(),
    showNotice: vi.fn(), setDraft: vi.fn(), showSessionEnded: vi.fn(), setLessonPlan: vi.fn(),
    setCurrentPage: vi.fn(),
  };
  const deps: RouterDeps = { executor, matcher: null, layout, feeder: null, refs, ui };
  const rows = () => [...drawLayer.getChildren(), ...animLayer.getChildren()]
    .map((n) => n.name() || '').filter((n) => n.startsWith('row_'));
  const route = (evt: any) => routeTutorEvent(evt, deps);
  return { executor, layout, drawLayer, animLayer, deps, rows, route, reports };
}

function write(route: (e: any) => void, turnId: string, gen: number, idx: number, rowId: string, text: string) {
  route({
    type: 'step', generation: gen,
    step: {
      turnId, generation: gen, stepIndex: idx, spokenText: 'x', words: ['x'],
      ops: [{ opId: `${turnId}:${idx}:0`, kind: 'WRITE', atWord: 0, text, rowId }],
    },
  });
}

const figureCommit = (turnId: string, pageId: string, gen: number) => ({
  type: 'page_commit', generation: gen, turnId, pageId, commitId: `c_${pageId}`,
  workRect: { x: 40, y: 72, width: 340, height: 608 },
  blocks: [{
    id: 'fig', role: 'figure', rect: { x: 420, y: 40, width: 720, height: 600 },
    commands: [{ type: 'DRAW_SEGMENT', params: [[0, 0], [100, 100]], anchorId: 'seg_AB' }],
    anchors: [{ id: 'seg_AB', box: { x: 0, y: 0, width: 100, height: 100 }, labels: ['AB'] }],
    reveals: [],
  }],
});

beforeAll(() => {
  const ctx = new Proxy({}, {
    get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
    set: () => true,
  });
  HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
});

describe('chapter page advance starts a clean work column', () => {
  it('page_1_does_not_write_over_page_0_rows', () => {
    const { route, rows, layout } = rig();
    route({ type: 'turn_started', generation: 2, turnId: 't0', kind: 'lesson', pageId: 'L_x_p0',
            newPage: true, pageIndex: 0 });
    write(route, 't0', 2, 0, 'w1', 'Topic: distance of a point from a line');
    write(route, 't0', 2, 1, 'w2', 'd = |ax0 + by0 + c| / sqrt(a^2 + b^2)');
    expect(rows().length).toBe(2);

    // Page 1 of the chapter: newPage stays false by design; its page_commit follows.
    route({ type: 'turn_started', generation: 2, turnId: 't1', kind: 'lesson', pageId: 'L_x_p1',
            newPage: false, pageIndex: 1 });
    route(figureCommit('t1', 'L_x_p1', 2));

    expect(rows()).toEqual([]);                                // page 0's ink is gone
    expect(layout.currentPageId).toBe('L_x_p1');               // reports carry page 1's id
    write(route, 't1', 2, 0, 'w3', 'Derivation');
    expect(rows()).toEqual(['row_w3']);
  });

  it('snapshot_subpage_then_resume_same_page_keeps_rows', () => {
    // board_snapshot re-roots the layout at a SUB-page id (L_x_p1_p2); the resume turn names
    // the root page id. That is the same page: nothing may be cleared.
    const { route, rows } = rig();
    route({ type: 'board_snapshot', generation: 3, current: { pageId: 'L_x_p1', subPages: [
      { subId: 'L_x_p1_p2', ops: [{ opId: 'a:0:0', kind: 'WRITE', atWord: 0, text: 'kept row', rowId: 'w7' }] }] } });
    expect(rows()).toEqual(['row_w7']);
    route({ type: 'turn_started', generation: 3, turnId: 'tr', kind: 'resume', pageId: 'L_x_p1',
            newPage: false, pageIndex: 1 });
    expect(rows()).toEqual(['row_w7']);
  });

  it('same_board_doubt_keeps_rows', () => {
    const { route, rows } = rig();
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'L_x_p1',
            newPage: true, pageIndex: 1 });
    write(route, 't0', 1, 0, 'w1', 'row');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'L_x_p1',
            newPage: false, pageIndex: 1 });
    expect(rows()).toEqual(['row_w1']);
  });
});

describe('a new-figure doubt keeps the board until its content arrives', () => {
  it('board_kept_while_the_doubt_is_prepared', () => {
    const { route, rows } = rig();
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'p_0',
            newPage: true });
    write(route, 't0', 1, 0, 'w1', 'sin 30 = 1/2');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'p_d1',
            newPage: true });
    expect(rows()).toEqual(['row_w1']);                        // not wiped 30 s early
  });

  it('cleared_when_the_doubt_figure_arrives', () => {
    const { route, rows, layout } = rig();
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'p_0',
            newPage: true });
    write(route, 't0', 1, 0, 'w1', 'sin 30 = 1/2');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'p_d1',
            newPage: true });
    route(figureCommit('d1', 'p_d1', 2));
    expect(rows()).toEqual([]);
    expect(layout.currentPageId).toBe('p_d1');
  });

  it('cleared_when_the_first_doubt_step_arrives', () => {
    const { route, rows } = rig();
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'p_0',
            newPage: true });
    write(route, 't0', 1, 0, 'w1', 'old');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'p_d1',
            newPage: true });
    write(route, 'd1', 2, 0, 'w2', 'new');
    expect(rows()).toEqual(['row_w2']);
  });

  it('failed_doubt_leaves_the_board_untouched', () => {
    const { route, rows } = rig();
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'p_0',
            newPage: true });
    write(route, 't0', 1, 0, 'w1', 'old');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'p_d1',
            newPage: true });
    route({ type: 'turn_ended', generation: 2, turnId: 'd1', status: 'partial' });
    // A later same-board turn must not trigger the stale pending wipe either.
    write(route, 'd1', 2, 0, 'w9', 'late');
    expect(rows()).toContain('row_w1');
  });

  it('works_when_deps_are_rebuilt_per_event_like_App', () => {
    // App.tsx passes a NEW deps/refs object literal to routeTutorEvent for every event.
    const { deps, rows } = rig();
    const route = (evt: any) => routeTutorEvent(evt, { ...deps, refs: { ...deps.refs } });
    route({ type: 'turn_started', generation: 1, turnId: 't0', kind: 'lesson', pageId: 'p_0',
            newPage: true });
    write(route, 't0', 1, 0, 'w1', 'old');
    route({ type: 'turn_started', generation: 2, turnId: 'd1', kind: 'doubt', pageId: 'p_d1',
            newPage: true });
    expect(rows()).toEqual(['row_w1']);
    write(route, 'd1', 2, 0, 'w2', 'new');
    expect(rows()).toEqual(['row_w2']);
  });
});

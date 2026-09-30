// frontend/src/__tests__/pageBreak.test.ts
// Pins the page-turn contract: a page turn erases the work column but KEEPS the verified
// figure (reference anchor) and advances to a new page id. clearWorkRows() must never call
// txManager.clearAll(), which would destroy the whole draw layer including the diagram.
import { describe, it, expect, beforeAll } from 'vitest';
import Konva from 'konva';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';

describe('Page turn / [PAGE_BREAK]', () => {
  beforeAll(() => {
    if (typeof HTMLCanvasElement !== 'undefined') {
      const dummyCtx = new Proxy(
        {},
        {
          get: (_t, prop) => {
            if (prop === 'measureText') return () => ({ width: 50 });
            if (prop === 'getImageData') return () => ({ data: new Uint8ClampedArray(4) });
            return () => {};
          },
          set: () => true,
        }
      );
      HTMLCanvasElement.prototype.getContext = (() => dummyCtx) as any;
    }
  });

  function setup(initialMode: 'DIAGRAM_STANDARD_1COL' | 'TEXT_ONLY_3COL' = 'DIAGRAM_STANDARD_1COL') {
    let executor!: CommandExecutor;
    const layout = new BoardLayout('p1', (newPageId) => executor.onOverflowTurn(newPageId), initialMode);
    const txManager = new TransactionManager();
    const stage = new Konva.Stage({ container: document.createElement('div'), width: 1200, height: 700 });
    const drawLayer = new Konva.Layer({ listening: false });
    const animLayer = new Konva.Layer({ listening: false });
    const spotlightLayer = new Konva.Layer({ listening: false });
    const highlightLayer = new Konva.Layer({ listening: false });
    const cursorLayer = new Konva.Layer({ listening: false });
    [drawLayer, animLayer, spotlightLayer, highlightLayer, cursorLayer].forEach((l) => stage.add(l));
    txManager.setLayers(animLayer, drawLayer);

    executor = new CommandExecutor({
      layout,
      txManager,
      drawLayer,
      animLayer,
      spotlightLayer,
      highlightLayer,
      cursorLayer,
      onSendReport: () => {},
    });
    executor.setGeneration(1);

    const diagram: any = {
      commands: [
        { type: 'DRAW_POINT', params: [100, 100, 3.5], anchor_id: 'A' },
        { type: 'DRAW_POINT', params: [300, 300, 3.5], anchor_id: 'B' },
        { type: 'DRAW_LINE', params: [100, 100, 300, 300], anchor_id: 'AB', semantic_ref: { entity_id: 'AB' } },
      ],
      anchors: [
        { id: 'A', x: 95, y: 95, width: 10, height: 10, labels: ['A'] },
        { id: 'B', x: 295, y: 295, width: 10, height: 10, labels: ['B'] },
        { id: 'AB', x: 100, y: 100, width: 200, height: 200, labels: [] },
      ],
      reveals: [],
    };

    return { executor, layout, drawLayer, animLayer, spotlightLayer, diagram };
  }

  it('clearWorkRows removes rows but keeps the diagram and its focus targets', () => {
    const { executor, drawLayer, diagram } = setup();
    executor.commitDiagram(diagram, 't', 1, true);
    executor.executeOp({ opId: 't:0:0', kind: 'WRITE', atWord: 0, text: 'row one', rowId: 'w1' } as any, 1, 't', true);

    const before = drawLayer.getChildren().map((c) => c.name());
    expect(before).toContain('block_diagram');
    expect(before).toContain('row_w1');

    executor.clearWorkRows();

    const after = drawLayer.getChildren().map((c) => c.name());
    expect(after).toContain('block_diagram'); // figure survives
    expect(after).not.toContain('row_w1'); // work erased
    expect(executor.activeDiagram).not.toBeNull();

    // The kept figure must still be focusable on the new page.
    executor.executeOp({ opId: 't:1:0', kind: 'FOCUS', atWord: 0, entityId: 'AB' } as any, 1, 't');
    expect((executor as any).activeFocusNodes.length).toBeGreaterThan(0);
  });

  it('[PAGE_BREAK] keeps the figure, advances the page id, and stays focusable', () => {
    const { executor, layout, drawLayer, diagram } = setup();
    executor.commitDiagram(diagram, 't', 1, true);
    executor.executeOp({ opId: 't:0:0', kind: 'WRITE', atWord: 0, text: 'row one', rowId: 'w1' } as any, 1, 't', true);

    const pageBefore = layout.currentPageId;

    executor.executeOp({ opId: 't:0:1', kind: 'PAGE_BREAK', atWord: 2, pageTitle: 'Congruence' } as any, 1, 't');

    expect(layout.currentPageId).not.toBe(pageBefore); // new page id, not an in-place reset
    expect(drawLayer.getChildren().map((c) => c.name())).toContain('block_diagram');
    expect(drawLayer.getChildren().map((c) => c.name())).not.toContain('row_w1');
    expect(executor.activeDiagram).not.toBeNull();

    executor.executeOp({ opId: 't:0:2', kind: 'FOCUS', atWord: 3, entityId: 'A' } as any, 1, 't');
    expect((executor as any).activeFocusNodes.length).toBeGreaterThan(0);
  });

  it('[PAGE_BREAK] in a text-only lesson still turns a clean page', () => {
    const { executor, layout, drawLayer, diagram } = setup('TEXT_ONLY_3COL');
    // no diagram committed
    void diagram;
    executor.executeOp({ opId: 't:0:0', kind: 'WRITE', atWord: 0, text: 'row one', rowId: 'w1' } as any, 1, 't', true);
    const pageBefore = layout.currentPageId;

    executor.executeOp({ opId: 't:0:1', kind: 'PAGE_BREAK', atWord: 2, pageTitle: 'Applications' } as any, 1, 't');

    expect(layout.currentPageId).not.toBe(pageBefore);
    expect(drawLayer.getChildren().map((c) => c.name())).not.toContain('row_w1');
  });

  it('break_before_write', () => {
    const { executor, layout } = setup('TEXT_ONLY_3COL');
    const pageBefore = layout.currentPageId;

    // The parser orders a step's ops [PAGE_BREAK, WRITE a, WRITE b].
    const stepOps = [
      { opId: 't:0:0', kind: 'PAGE_BREAK', atWord: 0, pageTitle: 'Part 2' },
      { opId: 't:0:1', kind: 'WRITE', atWord: 0, text: 'a = 1', rowId: 'w1' },
      { opId: 't:0:2', kind: 'WRITE', atWord: 0, text: 'b = 2', rowId: 'w2' },
    ];
    for (const op of stepOps) executor.executeOp(op as any, 1, 't');

    expect(layout.currentPageId).not.toBe(pageBefore);
    expect(layout.visibleRows.map((r) => r.rowId)).toEqual(['w1', 'w2']);
    expect(layout.getBoardReport().pageId).toBe(layout.currentPageId);
  });

  it('ack_includes_prebreak_ops', () => {
    const reports: any[] = [];
    const { executor, layout } = setup('TEXT_ONLY_3COL');
    (executor as any).onSendReport = (r: any) => reports.push(r);

    const stepOps = [
      { opId: 't:0:0', kind: 'PAGE_BREAK', atWord: 0, pageTitle: 'Part 2' },
      { opId: 't:0:1', kind: 'WRITE', atWord: 0, text: 'a = 1', rowId: 'w1' },
      { opId: 't:0:2', kind: 'WRITE', atWord: 0, text: 'b = 2', rowId: 'w2' },
    ];
    for (const op of stepOps) executor.executeOp(op as any, 1, 't');

    executor.reportCompleted(
      { turnId: 't', generation: 1, stepIndex: 0, spokenText: 'part two', words: ['part', 'two'], ops: stepOps as any },
      'words',
    );

    const ack = reports.filter((r) => r.type === 'step_progress').at(-1);
    expect(ack.drawnOpIds).toEqual(['t:0:0', 't:0:1', 't:0:2']);
    expect(layout.currentPageId).not.toBe('p1');
  });

  it('replay_before_break_does_not_redraw_on_new_page', () => {
    const { executor, layout } = setup('TEXT_ONLY_3COL');
    const oldWrite = { opId: 't:0:1', kind: 'WRITE', atWord: 0, text: 'old row', rowId: 'w1' };
    executor.executeOp(oldWrite as any, 1, 't');
    executor.executeOp({ opId: 't:1:0', kind: 'PAGE_BREAK', atWord: 0 } as any, 1, 't');
    expect(layout.visibleRows).toEqual([]);

    // Replaying the earlier step's op (whose ink lives on the previous sub-page) draws nothing.
    executor.executeOp(oldWrite as any, 1, 't');
    expect(layout.visibleRows).toEqual([]);
  });

  it('overflow_reports_page_turned', () => {
    const reports: any[] = [];
    const { executor, layout } = setup('DIAGRAM_STANDARD_1COL');
    (executor as any).onSendReport = (r: any) => reports.push(r);

    let i = 0;
    while (layout.currentPageId === 'p1' && i < 80) {
      executor.executeOp(
        { opId: `t:${i}:0`, kind: 'WRITE', atWord: 0, text: `row number ${i}`, rowId: `w${i}` } as any,
        1, 't', true,
      );
      i += 1;
    }

    expect(layout.currentPageId).not.toBe('p1');
    const turned = reports.filter((r) => r.type === 'page_turned').at(-1);
    expect(turned.cause).toBe('overflow');
    expect(turned.rootPageId).toBe('p1');
    expect(turned.toSubId).toBe(layout.currentPageId);
    expect(turned.atOpId).toBe(`t:${i - 1}:0`);   // the WRITE that did not fit
  });

  it('page_break_reports_page_turned', () => {
    const reports: any[] = [];
    const { executor } = setup('TEXT_ONLY_3COL');
    (executor as any).onSendReport = (r: any) => reports.push(r);

    executor.executeOp({ opId: 't:0:0', kind: 'PAGE_BREAK', atWord: 0, pageTitle: 'Part 2' } as any, 1, 't');

    const turned = reports.filter((r) => r.type === 'page_turned').at(-1);
    expect(turned).toMatchObject({ cause: 'page_break', rootPageId: 'p1', fromSubId: 'p1', toSubId: 'p1_p2' });
  });
});

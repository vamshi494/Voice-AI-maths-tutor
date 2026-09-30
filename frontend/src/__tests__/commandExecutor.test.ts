// frontend/src/__tests__/commandExecutor.test.ts
import { describe, it, expect, beforeAll } from 'vitest';
import Konva from 'konva';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { BoardOp, Step } from '../types/events';

describe('CommandExecutor op execution', () => {
  beforeAll(() => {
    // Mock 2D context using Proxy for headless testing
    if (typeof HTMLCanvasElement !== 'undefined') {
      const dummyCtx = new Proxy(
        {},
        {
          get: (target, prop) => {
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

  function setupTestExecutor() {
    const layout = new BoardLayout('p1');
    const txManager = new TransactionManager();

    const stage = new Konva.Stage({
      container: document.createElement('div'),
      width: 1920,
      height: 1080,
    });

    const drawLayer = new Konva.Layer({ listening: false });
    const animLayer = new Konva.Layer({ listening: false });
    const spotlightLayer = new Konva.Layer({ listening: false });
    const highlightLayer = new Konva.Layer({ listening: false });
    const cursorLayer = new Konva.Layer({ listening: false });

    stage.add(drawLayer);
    stage.add(animLayer);
    stage.add(spotlightLayer);
    stage.add(highlightLayer);
    stage.add(cursorLayer);

    txManager.setLayers(animLayer, drawLayer);

    const reports: any[] = [];
    const executor = new CommandExecutor({
      layout,
      txManager,
      drawLayer,
      animLayer,
      spotlightLayer,
      highlightLayer,
      cursorLayer,
      onSendReport: (r) => reports.push(r),
    });

    return { executor, layout, txManager, drawLayer, spotlightLayer, reports };
  }

  it('executes WRITE op and maintains idempotency on duplicate replay', () => {
    const { executor, layout, drawLayer } = setupTestExecutor();
    executor.setGeneration(1);

    const op: BoardOp = {
      opId: 't1:0:0',
      kind: 'WRITE',
      atWord: 0,
      text: '3x + 1 = 10',
      rowId: 'w1',
    };

    // First execution
    executor.executeOp(op, 1, 't1', true);
    expect(executor.drawnOpIds.has('t1:0:0')).toBe(true);
    expect(layout.visibleRows.length).toBe(1);
    expect(layout.visibleRows[0].text).toBe('3x + 1 = 10');

    // Duplicate replay execution -> skipped!
    executor.executeOp(op, 1, 't1', true);
    // Rows should NOT have duplicated
    expect(layout.visibleRows.length).toBe(1);
  });

  it('drops events carrying an older generation', () => {
    const { executor, layout } = setupTestExecutor();
    executor.setGeneration(2); // Active generation is 2

    const oldOp: BoardOp = {
      opId: 'old:0:0',
      kind: 'WRITE',
      atWord: 0,
      text: 'stale text',
      rowId: 'w1',
    };

    // Attempt to execute with old generation 1
    executor.executeOp(oldOp, 1, 't1', true);

    // Dropped
    expect(executor.drawnOpIds.has('old:0:0')).toBe(false);
    expect(layout.visibleRows.length).toBe(0);
  });

  it('pageRestore resets layout, clears drawnOpIds, and restores ops instantly', () => {
    const { executor, layout } = setupTestExecutor();
    executor.setGeneration(1);

    const op: BoardOp = {
      opId: 't1:0:0',
      kind: 'WRITE',
      atWord: 0,
      text: 'restored row',
      rowId: 'w1',
    };

    executor.pageRestore('p_restored', null, [op], 2);

    expect(executor.activeGeneration).toBe(2);
    expect(layout.currentPageId).toBe('p_restored');
    expect(layout.visibleRows.length).toBe(1);
    expect(layout.visibleRows[0].text).toBe('restored row');
    expect(executor.drawnOpIds.has('t1:0:0')).toBe(true);
  });

  it('highlights reveal group and line segments when diagram has snake_case keys', () => {
    const { executor, spotlightLayer } = setupTestExecutor();
    executor.setGeneration(1);

    const wireDiagram: any = {
      commands: [
        { type: 'DRAW_POINT', params: [780, 158.97, 3.5], anchor_id: 'A' },
        { type: 'DRAW_POINT', params: [490.34, 521.03, 3.5], anchor_id: 'B' },
        { type: 'DRAW_LINE', params: [490.34, 521.03, 780, 158.97], anchor_id: 'AB', semantic_ref: { entity_id: 'AB' } },
        { type: 'DRAW_LINE', params: [664.14, 303.79, 698.9, 521.03], anchor_id: 'DF', semantic_ref: { entity_id: 'DF' } },
        { type: 'DRAW_LINE', params: [780, 158.97, 837.93, 521.03], anchor_id: 'AE', semantic_ref: { entity_id: 'AE' } },
      ],
      anchors: [
        { id: 'A', x: 775, y: 133.97, width: 10, height: 29, labels: ['A'] },
        { id: 'B', x: 485.34, y: 496.03, width: 10, height: 29, labels: ['B'] },
        { id: 'DF', x: 664.14, y: 303.79, width: 34.76, height: 217.24, labels: [] },
        { id: 'AE', x: 780, y: 158.97, width: 57.93, height: 362.06, labels: [] },
      ],
      reveals: [
        {
          target_id: 'setup',
          command_indices: [0, 1, 2, 3, 4],
          kind: 'reveal',
        },
      ],
    };

    executor.commitDiagram(wireDiagram, 't1', 1);

    // Step 0: Focus reveal group "setup"
    executor.executeOp({ opId: 't1:0:0', kind: 'FOCUS', atWord: 5, entityId: 'setup' }, 1, 't1');
    expect(spotlightLayer.getChildren().length).toBeGreaterThan(0);

    // Step 3: Focus line segments "DF" and "AE" in the same step
    executor.executeOp({ opId: 't1:3:0', kind: 'FOCUS', atWord: 3, entityId: 'DF' }, 1, 't1');
    executor.executeOp({ opId: 't1:3:1', kind: 'FOCUS', atWord: 8, entityId: 'AE' }, 1, 't1');

    // Both highlights should be active in spotlightLayer
    expect(spotlightLayer.getChildren().length).toBeGreaterThanOrEqual(2);

    // Step 4: Step transition cleanly replaces previous focus
    executor.executeOp({ opId: 't1:4:0', kind: 'FOCUS', atWord: 4, entityId: 'B' }, 1, 't1');
    // SpotlightLayer has active elements for step 4
    expect(spotlightLayer.getChildren().length).toBeGreaterThan(0);
  });

  it('heard_up_to_contiguous', () => {
    const { executor, reports } = setupTestExecutor();
    executor.setGeneration(1);
    const mk = (idx: number): Step => ({
      turnId: 't1', generation: 1, stepIndex: idx, spokenText: `s${idx}`, words: [], ops: [],
    });
    const progress = () => reports.filter((r) => r.type === 'step_progress');
    const last = () => progress()[progress().length - 1];

    executor.reportStarted(mk(0));
    executor.reportCompleted(mk(0), 'words');
    executor.reportCompleted(mk(1), 'flush');           // flush is not heard
    expect(last().heardUpTo).toBe(0);
    expect(last().completedBy).toBe('flush');

    executor.reportCompleted(mk(2), 'stall');           // gap at 1: cursor stays 0
    expect(last().heardUpTo).toBe(0);

    executor.reportCompleted(mk(1), 'words');           // gap filled: cursor jumps to 2
    expect(last().heardUpTo).toBe(2);
    expect(last().startedUpTo).toBe(0);

    executor.reportFinal();
    expect(last().event).toBe('final');
    expect(last().heardUpTo).toBe(2);
  });
});

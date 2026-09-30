import { describe, it, expect, beforeAll } from 'vitest';
import Konva from 'konva';
import gsap from 'gsap';
import fs from 'fs';
import path from 'path';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { TranscriptSyncMatcher } from '../whiteboard/transcriptSync';
import { Step } from '../types/events';

describe('End-to-End Mock Test with langsmith_11.json', () => {
  beforeAll(() => {
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

  it('checks if GSAP animates Konva node opacity', async () => {
    const line = new Konva.Line({ points: [0, 0, 100, 100], opacity: 0 });
    expect(line.opacity()).toBe(0);
    gsap.to(line, { opacity: 1, duration: 0.05 });
    await new Promise((r) => setTimeout(r, 100));
    console.log('line.opacity():', line.opacity());
    console.log('line.attrs.opacity:', line.attrs.opacity);
    console.log('(line as any).opacity property:', (line as any).opacity);
  });

  function setupHarness() {
    const layout = new BoardLayout('p1');
    const txManager = new TransactionManager();

    const stage = new Konva.Stage({
      container: document.createElement('div'),
      width: 1200,
      height: 700,
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

    const executedOps: any[] = [];
    const completedSteps: Step[] = [];

    const executor = new CommandExecutor({
      layout,
      txManager,
      drawLayer,
      animLayer,
      spotlightLayer,
      highlightLayer,
      cursorLayer,
      onSendReport: () => {},
    });

    const syncMatcher = new TranscriptSyncMatcher(
      (op, generation, turnId) => {
        executedOps.push({ op, generation, turnId });
        executor.executeOp(op, generation, turnId);
      },
      (step: Step) => {
        completedSteps.push(step);
      }
    );

    return {
      executor,
      layout,
      spotlightLayer,
      drawLayer,
      syncMatcher,
      executedOps,
      completedSteps,
    };
  }

  it('runs the exact langsmith_11.json trace through syncMatcher and executor', () => {
    const tracePath = path.resolve(__dirname, '../../../langsmith_11.json');
    const traceData = JSON.parse(fs.readFileSync(tracePath, 'utf-8'));

    const { executor, spotlightLayer, syncMatcher, executedOps } = setupHarness();

    const turnId = traceData.inputs.request.turn_id;
    const generation = traceData.inputs.request.generation;
    const diagram = traceData.outputs.diagram;
    const steps = traceData.outputs.steps;

    executor.setGeneration(generation);
    syncMatcher.beginTurn(turnId);

    // 1. Commit diagram
    executor.commitDiagram(diagram, turnId, generation, true);
    expect(executor.activeDiagram).toBeDefined();
    expect(executor.activeAnchors.length).toBeGreaterThan(0);

    // Verify anchors exist for DE, DF, AE, CA, AB, BC, etc.
    const anchorIds = executor.activeAnchors.map((a) => a.id);
    expect(anchorIds).toContain('DE');
    expect(anchorIds).toContain('DF');
    expect(anchorIds).toContain('AE');

    // 2. Enqueue steps from trace directly (raw snake_case)
    for (const s of steps) {
      syncMatcher.enqueueStep(s);
    }

    // Step 0: "look at this triangle figure we want to prove..."
    // Ops: WRITE "To prove: BF/FE = BE/EC"
    syncMatcher.onIncomingTranscript('look at this triangle figure we want to prove a lovely ratio result');
    syncMatcher.onIncomingTranscript('about the segments on the base');

    // Step 1: "look at this figure with triangle a b c and points d f e inside it"
    // Ops: FOCUS g_setup (at_word: 4)
    expect(spotlightLayer.getChildren().length).toBe(0);
    syncMatcher.onIncomingTranscript('look at this figure with');
    // At word 4, g_setup should fire
    const focusOpsStep1 = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'g_setup');
    expect(focusOpsStep1.length).toBe(1);
    expect(spotlightLayer.getChildren().length).toBeGreaterThan(0);

    // Feed remaining words of step 1
    syncMatcher.onIncomingTranscript('triangle a b c and points d f e inside it');

    // Step 2: "Notice segment DE is parallel to side AC in our figure."
    // Ops: FOCUS DE (at_word 3), FOCUS CA (at_word 8), WRITE DE || AC
    syncMatcher.onIncomingTranscript('notice segment de is');
    const focusOpsStep2DE = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'DE');
    expect(focusOpsStep2DE.length).toBe(1);
    expect(spotlightLayer.getChildren().length).toBeGreaterThan(0);

    syncMatcher.onIncomingTranscript('parallel to side ac in our figure');
    const focusOpsStep2CA = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'CA');
    expect(focusOpsStep2CA.length).toBe(1);

    // Step 3: "Follow segment DF which is parallel to segment AE ."
    // Ops: FOCUS DF (at_word 3), FOCUS AE (at_word 8), WRITE DF || AE
    syncMatcher.onIncomingTranscript('follow segment df which is');
    const focusOpsStep3DF = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'DF');
    expect(focusOpsStep3DF.length).toBe(1);

    syncMatcher.onIncomingTranscript('parallel to segment ae');
    const focusOpsStep3AE = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'AE');
    expect(focusOpsStep3AE.length).toBe(1);

    // Step 4: "Now look at the smaller triangle with vertex A and vertex B and E on the base."
    // Ops: FOCUS A (at_word 8), FOCUS B (at_word 11)
    syncMatcher.onIncomingTranscript('now look at the smaller triangle with vertex a');
    const focusOpsStep4A = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'A');
    expect(focusOpsStep4A.length).toBe(1);

    syncMatcher.onIncomingTranscript('and vertex b and e on the base');
    const focusOpsStep4B = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'B');
    expect(focusOpsStep4B.length).toBe(1);

    // Step 5: "By Basic Proportionality Theorem, a parallel line divides the two sides in the same ratio."
    // Ops: WRITE "BF/FE = BD/DA"
    syncMatcher.onIncomingTranscript('by basic proportionality theorem a parallel line divides the two sides in the same ratio');

    // Step 6: "This is side AB where point D lies, part of the big triangle."
    // Ops: FOCUS AB (at_word 3), FOCUS D (at_word 7), WRITE In △ABC, DE || AC
    syncMatcher.onIncomingTranscript('this is side ab where');
    const focusOpsStep6AB = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'AB');
    expect(focusOpsStep6AB.length).toBe(1);

    syncMatcher.onIncomingTranscript('point d lies part of the big triangle');
    const focusOpsStep6D = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'D');
    expect(focusOpsStep6D.length).toBe(1);

    // Step 7: "Follow the base BC up to point C to see the second division."
    // Ops: FOCUS BC (at_word 3), FOCUS C (at_word 7), WRITE BD/DA = BE/EC
    syncMatcher.onIncomingTranscript('follow the base bc up to');
    const focusOpsStep7BC = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'BC');
    expect(focusOpsStep7BC.length).toBe(1);

    syncMatcher.onIncomingTranscript('point c to see the second division');
    const focusOpsStep7C = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'C');
    expect(focusOpsStep7C.length).toBe(1);

    // Step 8: "Notice point F and point E give the same middle ratio B D by D A, so the two outer ratios match."
    // Ops: FOCUS F (at_word 2), FOCUS E (at_word 5), WRITE BF/FE = BE/EC
    syncMatcher.onIncomingTranscript('notice point f and');
    const focusOpsStep8F = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'F');
    expect(focusOpsStep8F.length).toBe(1);

    syncMatcher.onIncomingTranscript('point e give the same middle ratio');
    const focusOpsStep8E = executedOps.filter((e) => (e.op.entityId || e.op.entity_id) === 'E');
    expect(focusOpsStep8E.length).toBe(1);

    // Verify all 13 focus operations were executed successfully in exact speech synchronization!
    const allFocusOps = executedOps.filter((e) => e.op.kind === 'FOCUS');
    expect(allFocusOps.length).toBe(13);

    // Verify spotlightLayer has the active highlight shapes for the final step (points F and E)
    const activeSpotlightChildren = spotlightLayer.getChildren();
    expect(activeSpotlightChildren.length).toBeGreaterThan(0);
  });
});

// frontend/src/__tests__/highlightCoverage.test.ts
// [FOCUS] must highlight EVERY command type the server emits, capped at FOCUS_CAP.
import { describe, it, expect, beforeAll } from 'vitest';
import Konva from 'konva';
import commits from './fixtures/diagram_commits.json';
import { CommandExecutor, FOCUS_CAP } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { DiagramCommitEvent } from '../types/events';

const events = commits as unknown as DiagramCommitEvent[];

function setup() {
  const layout = new BoardLayout('p1');
  const txManager = new TransactionManager();
  const stage = new Konva.Stage({ container: document.createElement('div'), width: 1200, height: 700 });
  const drawLayer = new Konva.Layer({ listening: false });
  const animLayer = new Konva.Layer({ listening: false });
  const spotlightLayer = new Konva.Layer({ listening: false });
  stage.add(drawLayer, animLayer, spotlightLayer);
  txManager.setLayers(animLayer, drawLayer);
  const executor = new CommandExecutor({
    layout, txManager, drawLayer, animLayer, spotlightLayer,
    highlightLayer: new Konva.Layer({ listening: false }),
    cursorLayer: new Konva.Layer({ listening: false }),
    onSendReport: () => {},
  });
  executor.setGeneration(1);
  return { executor, spotlightLayer };
}

describe('highlight coverage', () => {
  beforeAll(() => {
    const ctx = new Proxy({}, {
      get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
      set: () => true,
    });
    HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
  });

  it('every_command_type_highlightable', () => {
    type Case = { diagram: DiagramCommitEvent['diagram']; anchorId: string };
    const byType = new Map<string, Case>();
    for (const entry of events) {
      for (const cmd of entry.diagram.commands) {
        if (!byType.has(cmd.type) && cmd.anchorId) {
          byType.set(cmd.type, { diagram: entry.diagram, anchorId: cmd.anchorId });
        }
      }
    }
    expect(byType.size).toBeGreaterThanOrEqual(14);

    for (const [type, { diagram, anchorId }] of byType) {
      const { executor, spotlightLayer } = setup();
      executor.commitDiagram(diagram, 't', 1, true);
      executor.executeOp({ opId: `t:0:${type}`, kind: 'FOCUS', atWord: 0, entityId: anchorId }, 1, 't');
      const groups = spotlightLayer.getChildren().filter((n) => n.name() === 'focus_group') as Konva.Group[];
      expect(groups.length, type).toBeGreaterThan(0);
      expect(groups[0].getChildren().length, type).toBeGreaterThan(0);
    }
  });

  it('cap_two_highlights', () => {
    const { executor } = setup();
    const diagram = events[0].diagram;
    const alias = diagram.aliasMap || {};
    executor.commitDiagram(diagram, 't', 1, true);
    // Same step prefix (t:0) so the step transition never fades them. Anchors carry
    // canonical ids; resolve the old LLM ids through the wire aliasMap.
    executor.executeOp({ opId: 't:0:0', kind: 'FOCUS', atWord: 0, entityId: alias['tri']! }, 1, 't');
    executor.executeOp({ opId: 't:0:1', kind: 'FOCUS', atWord: 0, entityId: alias['circ']! }, 1, 't');

    const active = () => (executor as any).activeFocusGroups as Konva.Group[];
    expect(active().length).toBe(FOCUS_CAP);

    executor.executeOp({ opId: 't:0:2', kind: 'FOCUS', atWord: 0, entityId: alias['mk']! }, 1, 't');
    expect(active().length).toBe(FOCUS_CAP);
    // The oldest (tri) is evicted from active tracking; the newest is.
    expect((executor as any).activeFocusNodes.length).toBe(FOCUS_CAP);
  });
});

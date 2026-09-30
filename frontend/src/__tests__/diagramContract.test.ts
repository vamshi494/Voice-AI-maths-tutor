// frontend/src/__tests__/diagramContract.test.ts
// Cross-stack contract: renders REAL diagram_commit events produced by the backend compiler
// (fixtures/diagram_commits.json, regenerate with backend/tests/make_frontend_fixture.py).
// Anchors carry x/y/width/height: commitDiagram throws on the first anchor whose shape it
// cannot read, so no figure is drawn unless every anchor matches.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import Konva from 'konva';
import events from './fixtures/diagram_commits.json';
import { CommandExecutor, DIAGRAM_ZONE } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { DiagramCommitEvent, Step } from '../types/events';

const commits = events as unknown as DiagramCommitEvent[];

function setup() {
  const layout = new BoardLayout('p1');
  const txManager = new TransactionManager();
  // Layers live in a Stage exactly as in Whiteboard.tsx (Konva draws semi-transparent groups
  // through the stage's buffer canvas).
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

describe('diagram_commit contract (backend fixture -> Konva)', () => {
  beforeAll(() => {
    const ctx = new Proxy({}, {
      get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
      set: () => true,
    });
    HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
  });

  it('fixture covers every command type the server can emit', () => {
    const types = new Set(commits.flatMap((e) => e.diagram.commands.map((c) => c.type)));
    for (const t of ['DRAW_POINT', 'DRAW_LINE', 'DRAW_RAY', 'DRAW_CIRCLE', 'DRAW_ARC', 'DRAW_POLYLINE',
      'DRAW_ANGLE_MARK', 'DRAW_RIGHT_ANGLE_MARK', 'DRAW_TICK', 'DRAW_DIMENSION', 'DRAW_AXES',
      'DRAW_CURVE', 'DRAW_NUMBER_LINE', 'LABEL']) {
      expect(types.has(t), t).toBe(true);
    }
  });

  it.each(commits.map((e) => [e.turnId, e] as const))('%s: every command renders real ink', (_name, evt) => {
    const { executor } = setup();
    for (const cmd of evt.diagram.commands) {
      const g = new Konva.Group();
      expect(executor.renderDiagramCommand(cmd, g), `${cmd.type} rendered nothing`).toBe(true);
      expect(countShapes(g), cmd.type).toBeGreaterThan(0);
    }
  });

  it('commitDiagram draws instantly without throwing and exposes x/y anchors', () => {
    const { executor, drawLayer, layout } = setup();
    const evt = commits[0];
    expect(() => executor.commitDiagram(evt.diagram, evt.turnId, 1, true)).not.toThrow();
    expect(countShapes(drawLayer)).toBeGreaterThanOrEqual(evt.diagram.commands.length);
    expect(layout.getLayoutMode()).toBe('DIAGRAM_STANDARD_1COL');
    const targets = layout.getMarkingCandidates(executor.activeAnchors).filter((t) => t.kind === 'diagram');
    expect(targets.length).toBe(evt.diagram.anchors.length);
    for (const t of targets) {
      expect(Number.isFinite(t.rect.x + t.rect.y + t.rect.width + t.rect.height)).toBe(true);
      expect(t.rect.x).toBeGreaterThanOrEqual(DIAGRAM_ZONE.x - 1);
    }
  });

  it('animated commit draws the base figure immediately even when reveal groups exist', () => {
    const { executor, animLayer } = setup();
    const evt = commits[0];
    expect((evt.diagram.reveals || []).length).toBeGreaterThan(0);
    executor.commitDiagram(evt.diagram, evt.turnId, 1, false);
    const base = animLayer.findOne('.reveal_base') as Konva.Group;
    expect(base, 'commands outside reveal groups were never drawn').toBeTruthy();
    const covered = new Set((evt.diagram.reveals || []).flatMap((r) => r.commandIndices));
    expect(countShapes(base)).toBeGreaterThanOrEqual(evt.diagram.commands.length - covered.size);
  });

  it('a reveal scheduled before clearBoard never lands on the new board', () => {
    vi.useFakeTimers();
    const { executor, animLayer } = setup();
    executor.commitDiagram(commits[0].diagram, commits[0].turnId, 1, false);
    executor.clearBoard(true);
    vi.advanceTimersByTime(5000);
    expect(countShapes(animLayer)).toBe(0);
    vi.useRealTimers();
  });

  it('FOCUS highlights a real anchor; ANNOTATE reveals the withheld answer', () => {
    const { executor, spotlightLayer, drawLayer } = setup();
    const evt = commits[0];
    executor.commitDiagram(evt.diagram, evt.turnId, 1, true);
    const anchor = evt.diagram.anchors[0];
    executor.executeOp({ opId: 'f1', kind: 'FOCUS', atWord: 0, entityId: anchor.id }, 1, evt.turnId);
    expect(spotlightLayer.getChildren().length).toBe(1);
    const before = drawLayer.find('Text').map((t) => (t as Konva.Text).text());
    expect(before).not.toContain('AC = 13 cm');
    const deferred = (evt.diagram.deferredAnnotations || [])[0];
    executor.executeOp({ opId: 'a1', kind: 'ANNOTATE', atWord: 0, entityId: deferred.entityId }, 1, evt.turnId);
    const after = drawLayer.find('Text').map((t) => (t as Konva.Text).text());
    expect(after).toContain('AC = 13 cm');
    expect(after).toEqual(expect.arrayContaining(['A', 'B', 'C', 'AB = 5 cm']));
  });

  it('step_progress reports only the ops of that step', () => {
    const { executor, reports } = setup();
    executor.executeOp({ opId: 'old:0:0', kind: 'PAUSE', atWord: 0 }, 1, 't');
    const step: Step = { turnId: 't', generation: 1, stepIndex: 1, spokenText: 'x', words: ['x'],
      ops: [{ opId: 't:1:0', kind: 'PAUSE', atWord: 0 }, { opId: 't:1:1', kind: 'PAUSE', atWord: 0 }] };
    executor.executeOp(step.ops[0], 1, 't');
    executor.reportCompleted(step, 'words');
    expect(reports.at(-1)).toMatchObject({ type: 'step_progress', stepIndex: 1, drawnOpIds: ['t:1:0'] });
  });
});

// frontend/src/__tests__/executorLifecycle.test.tsx
// Regression guard for two distinct failures:
//  1. The executor must survive parent re-renders (otherwise handleFocus runs against
//     activeDiagram = null and every [FOCUS] highlight is silently dropped).
//  2. Under React 18 StrictMode (enabled in main.tsx) react-konva destroys and rebuilds the
//     Konva.Stage on the simulated remount, so the executor MUST rebind to the fresh layers,
//     or all ink is drawn into a dead stage and the board stays blank.
import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import React, { StrictMode, act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Konva from 'konva';
import { Whiteboard } from '../whiteboard/Whiteboard';
import { CommandExecutor } from '../whiteboard/commandExecutor';

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe('CommandExecutor lifecycle', () => {
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

  let root: ReturnType<typeof createRoot> | null = null;
  let container: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => {
      root?.unmount();
    });
    container?.remove();
    root = null;
    container = null;
  });

  it('rebinds to the live StrictMode stage and survives parent re-renders', () => {
    const executors: CommandExecutor[] = [];
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);

    const Parent: React.FC = () => {
      const [tick, setTick] = useState(0);
      (globalThis as any).__bump = () => setTick((t) => t + 1);
      return (
        <StrictMode>
          <Whiteboard
            onExecutorReady={(ex) => executors.push(ex)}
            // Deliberately inline (new identity each render), matching how App.tsx passes it.
            onSendReport={() => {}}
          />
        </StrictMode>
      );
    };

    act(() => {
      root!.render(<Parent />);
    });

    expect(executors.length).toBeGreaterThanOrEqual(1);
    // Use the LAST executor handed to App: under StrictMode that is the one bound to the
    // freshly rebuilt Konva stage.
    const executor = executors[executors.length - 1];
    const drawLayer = (executor as any).drawLayer as Konva.Layer;

    // The executor must be wired to the stage the user can actually see.
    expect(Konva.stages).toContain(drawLayer.getStage());
    const mountedCount = executors.length;

    // Parent re-renders (setIsSpeaking / setHasDrawnContent / setIsLocked in App) must not
    // rebuild the executor, or its committed diagram/highlight state is thrown away.
    act(() => {
      (globalThis as any).__bump();
    });
    act(() => {
      (globalThis as any).__bump();
    });
    expect(executors.length).toBe(mountedCount);

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

    act(() => {
      executor.commitDiagram(diagram, 't1', 1, true);
      executor.executeOp(
        { opId: 't1:0:0', kind: 'FOCUS', atWord: 0, entityId: 'AB' } as any,
        1,
        't1'
      );
    });

    // commitDiagram calls onLayoutChanged -> Whiteboard re-renders; the executor must survive.
    expect(executors.length).toBe(mountedCount);
    expect(executor.activeDiagram).not.toBeNull();
    expect((executor as any).activeFocusNodes.length).toBeGreaterThan(0);
  });
});

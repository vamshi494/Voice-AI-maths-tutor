// frontend/src/__tests__/notesDrawer.test.tsx
// The drawer's offscreen preview survives a StrictMode double mount
// with exactly one live stage, and it re-renders/refetches only on selection change —
// live tutor events never touch it because it subscribes to nothing.
import React from 'react';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import Konva from 'konva';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { NotesDrawer } from '../components/NotesDrawer';

const snapshot = {
  pageId: 'L_x_p0',
  title: 'What BPT says',
  commit: {
    type: 'page_commit', generation: 1, turnId: 't', pageId: 'L_x_p0', commitId: 'c1',
    workRect: { x: 40, y: 72, width: 340, height: 608 },
    blocks: [{
      id: 'fig', role: 'figure', rect: { x: 444, y: 64, width: 324, height: 552 },
      commands: [{ type: 'DRAW_POINT', params: [500, 120, 4] }], anchors: [], reveals: [],
    }],
  },
  subPages: [{ subId: 'L_x_p0', ops: [] }],
};

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe('NotesDrawer', () => {
  beforeAll(() => {
    (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
    const ctx = new Proxy({}, {
      get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 50 }) : () => {}),
      set: () => true,
    });
    HTMLCanvasElement.prototype.getContext = (() => ctx) as any;
  });

  it('strictmode_single_stage_and_no_event_driven_refetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => snapshot });
    vi.stubGlobal('fetch', fetchMock);
    const destroy = vi.spyOn(Konva.Stage.prototype, 'destroy');
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const pages = [{ pageId: 'L_x_p0', index: 0, title: 'What BPT says' }];

    await act(async () => {
      root.render(
        <React.StrictMode>
          <NotesDrawer open boardId="b1" pages={pages} apiBase="http://api" onClose={() => {}} />
        </React.StrictMode>,
      );
    });
    await flush();

    // Exactly one live stage after the StrictMode double mount (the abandoned mount never
    // built one because the snapshot had not arrived; the live one is destroyed on unmount).
    expect(host.querySelectorAll('.konvajs-content').length).toBe(1);
    const fetchesAfterMount = fetchMock.mock.calls.length;
    expect(fetchesAfterMount).toBeGreaterThanOrEqual(1);

    // Re-renders with the same selection never refetch (a live board_report cannot reach
    // this component: it has no tutor-event subscription at all).
    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        root.render(
          <React.StrictMode>
            <NotesDrawer open boardId="b1" pages={[{ ...pages[0] }]} apiBase="http://api"
                         onClose={() => {}} />
          </React.StrictMode>,
        );
      });
      await flush();
    }
    expect(fetchMock.mock.calls.length).toBe(fetchesAfterMount);
    expect(host.querySelectorAll('.konvajs-content').length).toBe(1);

    await act(async () => {
      root.unmount();
    });
    expect(host.querySelectorAll('.konvajs-content').length).toBe(0);
    expect(destroy.mock.calls.length).toBeGreaterThanOrEqual(1);

    vi.unstubAllGlobals();
    destroy.mockRestore();
    host.remove();
  });
});

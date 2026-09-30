// frontend/src/components/NotesDrawer.tsx
// The notes drawer. A text list of page titles plus a read-only preview of the
// selected page on its OWN offscreen stage (opening it never touches the live board,
// the matcher or the audio).
import React, { useEffect, useRef, useState } from 'react';
import Konva from 'konva';
import { Download, X } from 'lucide-react';
import { BoardLayout } from '../whiteboard/boardLayout';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { exportLessonNotesPdf } from '../whiteboard/export';
import { TransactionManager } from '../whiteboard/drawTransactions';
import { LOGICAL_HEIGHT, LOGICAL_WIDTH } from '../whiteboard/Whiteboard';
import { PageHeader, SnapshotPage } from '../types/events';

interface NotesDrawerProps {
  open: boolean;
  onClose: () => void;
  boardId: string;
  pages: PageHeader[];
  apiBase: string;
}

export const NotesDrawer: React.FC<NotesDrawerProps> = ({ open, onClose, boardId, pages, apiBase }) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open) setSelected(pages[0]?.pageId ?? null);
  }, [open, pages]);

  // Fetch one page snapshot per selection; every path clears the spinner.
  useEffect(() => {
    if (!open || !selected || !boardId) {
      setSnapshot(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSnapshot(null);
    const url = `${apiBase}/boards/${encodeURIComponent(boardId)}/pages/${encodeURIComponent(selected)}`;
    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setSnapshot(data as SnapshotPage);
      })
      .catch((err) => {
        console.warn('[notes] page fetch failed', err);
        if (!cancelled) setError("Couldn't load that page.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, selected, boardId, apiBase]);

  // Read-only render: a private stage + executor, created exactly once per snapshot and
  // destroyed on cleanup (StrictMode-safe: the cleanup always runs before the next effect).
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !snapshot) return;
    container.innerHTML = '';
    const stage = new Konva.Stage({ container, width: LOGICAL_WIDTH, height: LOGICAL_HEIGHT });
    const drawLayer = new Konva.Layer({ listening: false });
    const animLayer = new Konva.Layer({ listening: false });
    const spotlightLayer = new Konva.Layer({ listening: false });
    const highlightLayer = new Konva.Layer({ listening: false });
    const cursorLayer = new Konva.Layer({ listening: false });
    const dummyLayer = new Konva.Layer({ listening: false });
    stage.add(drawLayer, animLayer, spotlightLayer, highlightLayer, cursorLayer);

    const txManager = new TransactionManager();
    txManager.setLayers(animLayer, drawLayer);
    const layout = new BoardLayout(snapshot.pageId || 'notes');
    const executor = new CommandExecutor({
      layout, txManager, drawLayer, animLayer, spotlightLayer, highlightLayer, cursorLayer,
      onSendReport: () => {},
    });
    try {
      executor.clearBoard(true);
      layout.reset(snapshot.pageId || 'notes');
      if (snapshot.commit) {
        executor.commitPage(snapshot.commit, true);
      } else if (snapshot.diagram) {
        executor.commitDiagram(snapshot.diagram, '', 1, true);
      } else {
        layout.setLayoutMode('TEXT_ONLY_3COL');
      }
      const last = snapshot.subPages?.[snapshot.subPages.length - 1];
      for (const op of last?.ops || []) executor.executeOp(op, 1, '', true);
      const width = container.clientWidth || LOGICAL_WIDTH;
      const scale = Math.min(1, width / LOGICAL_WIDTH);
      stage.scale({ x: scale, y: scale });
      stage.size({ width: LOGICAL_WIDTH * scale, height: LOGICAL_HEIGHT * scale });
      stage.batchDraw();
    } catch (err) {
      // A malformed page must never break the drawer.
      console.error('[notes] preview render failed; continuing read-only', err);
    }
    return () => {
      stage.destroy();
    };
  }, [snapshot]);

  if (!open) return null;

  const handleExport = async () => {
    if (!boardId || pages.length === 0 || exporting) return;
    setExporting(true);
    setError(null);
    try {
      // Collect every page of the lesson (the drawer only holds the selected snapshot).
      const snapshots: SnapshotPage[] = [];
      for (const page of pages) {
        const url = `${apiBase}/boards/${encodeURIComponent(boardId)}/pages/${encodeURIComponent(page.pageId)}`;
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        snapshots.push((await res.json()) as SnapshotPage);
      }
      const blob = await exportLessonNotesPdf(snapshots, 'Lesson notes');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'lesson-notes.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (err) {
      console.warn('[notes] export failed', err);
      setError("Couldn't export the notes. Please try again.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <div
      style={{
        position: 'absolute',
        top: 0,
        right: 0,
        bottom: 0,
        width: 480,
        maxWidth: '95vw',
        backgroundColor: 'rgba(15, 23, 42, 0.97)',
        borderLeft: '1px solid rgba(245, 158, 11, 0.35)',
        zIndex: 60,
        display: 'flex',
        flexDirection: 'column',
        padding: 16,
        gap: 12,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <strong style={{ color: '#fef3c7', fontSize: 15 }}>Lesson notes</strong>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button
            onClick={handleExport}
            disabled={exporting || pages.length === 0}
            title="Export notes as PDF"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              background: 'rgba(16, 185, 129, 0.15)',
              border: '1px solid rgba(16, 185, 129, 0.5)',
              color: '#6ee7b7',
              borderRadius: 6,
              padding: '4px 10px',
              fontSize: 12,
              cursor: exporting || pages.length === 0 ? 'not-allowed' : 'pointer',
              opacity: exporting || pages.length === 0 ? 0.5 : 1,
            }}
          >
            <Download size={13} />
            {exporting ? 'Exporting…' : 'Export notes'}
          </button>
          <button
            onClick={onClose}
            title="Close notes"
            style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }}
          >
            <X size={18} />
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 120, overflowY: 'auto' }}>
        {pages.length === 0 && (
          <span style={{ color: '#94a3b8', fontSize: 12 }}>No saved pages yet.</span>
        )}
        {pages.map((page) => {
          const active = page.pageId === selected;
          return (
            <button
              key={page.pageId}
              onClick={() => setSelected(page.pageId)}
              style={{
                fontSize: 12,
                padding: '4px 10px',
                borderRadius: 12,
                cursor: 'pointer',
                border: `1px solid ${active ? 'rgba(245,158,11,0.8)' : 'rgba(255,255,255,0.12)'}`,
                backgroundColor: active ? 'rgba(245,158,11,0.18)' : 'rgba(255,255,255,0.04)',
                color: active ? '#fde68a' : '#cbd5e1',
              }}
            >
              {page.index + 1}. {page.title}
            </button>
          );
        })}
      </div>

      {loading && <div style={{ color: '#94a3b8', fontSize: 13 }}>Loading page…</div>}
      {error && <div role="status" style={{ color: '#fca5a5', fontSize: 13 }}>{error}</div>}
      <div
        ref={containerRef}
        data-testid="notes-preview"
        style={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          background: '#16201b',
          borderRadius: 8,
          border: '1px solid rgba(255,255,255,0.08)',
        }}
      />
    </div>
  );
};

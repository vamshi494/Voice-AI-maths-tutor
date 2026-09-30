// frontend/src/whiteboard/export.ts
// Export — board snapshot and the lesson notes PDF.

import Konva from 'konva';
import jsPDF from 'jspdf';
import { SnapshotPage, VerifiedDiagram } from '../types/events';
import { LOGICAL_HEIGHT, LOGICAL_WIDTH } from './Whiteboard';
import { BoardLayout } from './boardLayout';
import { TransactionManager } from './drawTransactions';
import { CommandExecutor } from './commandExecutor';

/**
 * Capture high-res snapshot of current board.
 * Only Background and Draw layers are visible in export.
 */
export async function exportBoardSnapshot(stage: Konva.Stage): Promise<string> {
  const animLayer = stage.findOne('.animLayer');
  const spotlightLayer = stage.findOne('.spotlightLayer');
  const cursorLayer = stage.findOne('.cursorLayer');
  const highlightLayer = stage.findOne('.highlightLayer');

  // Temporarily hide non-draw layers
  if (animLayer) animLayer.visible(false);
  if (spotlightLayer) spotlightLayer.visible(false);
  if (cursorLayer) cursorLayer.visible(false);
  if (highlightLayer) highlightLayer.visible(false);

  stage.batchDraw();

  const dataUrl = stage.toDataURL({ pixelRatio: 2 });

  // Restore layer visibility
  if (animLayer) animLayer.visible(true);
  if (spotlightLayer) spotlightLayer.visible(true);
  if (cursorLayer) cursorLayer.visible(true);
  if (highlightLayer) highlightLayer.visible(true);

  stage.batchDraw();

  return dataUrl;
}

/**
 * Generate a multi-page PDF of the lesson notes.
 *
 * Each sub-page of each SnapshotPage becomes one PDF page: the page's `commit` renders
 * through `commitPage(..., instant=true)` when present (multi-block figures/tables/text),
 * else the legacy `diagram` through `commitDiagram`, then that sub-page's acked ops execute
 * instantly. Rendering happens on a private offscreen stage that is always destroyed.
 */
export async function exportLessonNotesPdf(pages: SnapshotPage[], lessonTitle: string = 'Lesson Notes'): Promise<Blob> {
  const pdf = new jsPDF({
    orientation: 'landscape',
    unit: 'pt',
    format: [LOGICAL_WIDTH, LOGICAL_HEIGHT],
  });

  // Create temporary offscreen container
  const container = document.createElement('div');
  container.style.width = `${LOGICAL_WIDTH}px`;
  container.style.height = `${LOGICAL_HEIGHT}px`;
  container.style.position = 'absolute';
  container.style.left = '-9999px';
  document.body.appendChild(container);

  const stage = new Konva.Stage({
    container,
    width: LOGICAL_WIDTH,
    height: LOGICAL_HEIGHT,
  });

  const bgLayer = new Konva.Layer({ listening: false });
  const drawLayer = new Konva.Layer({ listening: false });
  const animLayer = new Konva.Layer({ listening: false });
  const spotlightLayer = new Konva.Layer({ listening: false });
  const highlightLayer = new Konva.Layer({ listening: false });
  const cursorLayer = new Konva.Layer({ listening: false });
  const dummyLayer = new Konva.Layer({ listening: false });

  bgLayer.add(
    new Konva.Rect({
      x: 0,
      y: 0,
      width: LOGICAL_WIDTH,
      height: LOGICAL_HEIGHT,
      fill: '#16201b',
    })
  );

  stage.add(bgLayer, drawLayer, animLayer);

  const layout = new BoardLayout('export_p1');
  const txManager = new TransactionManager();
  txManager.setLayers(animLayer, drawLayer);

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

  try {
    let pdfPage = 0;
    for (const page of pages) {
      const subPages = page.subPages && page.subPages.length > 0
        ? page.subPages
        : [{ subId: page.pageId || 'export', ops: [] }];
      for (const sub of subPages) {
        if (pdfPage > 0) {
          pdf.addPage([LOGICAL_WIDTH, LOGICAL_HEIGHT], 'landscape');
        }
        pdfPage += 1;

        executor.clearBoard(true);
        layout.reset(sub.subId || page.pageId || 'export');
        if (page.commit) {
          executor.commitPage(page.commit, true);
        } else if (page.diagram) {
          executor.commitDiagram(page.diagram as VerifiedDiagram, '', 1, true);
        } else {
          layout.setLayoutMode('TEXT_ONLY_3COL');
        }
        for (const op of sub.ops || []) {
          executor.executeOp(op, 1, '', true);
        }
        stage.batchDraw();

        const dataUrl = stage.toDataURL({ pixelRatio: 1.5 });
        pdf.addImage(dataUrl, 'JPEG', 0, 0, LOGICAL_WIDTH, LOGICAL_HEIGHT);
      }
    }

    return pdf.output('blob');
  } finally {
    // Cleanup offscreen DOM on every path (success, empty, or a thrown render).
    stage.destroy();
    document.body.removeChild(container);
  }
}

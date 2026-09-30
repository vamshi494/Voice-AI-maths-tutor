// frontend/src/whiteboard/boardLayout.ts
// Layout engine (the single layout owner; client-only)
// Extended for dynamic 3-section multi-column chalkboard layout

import { DiagramAnchor, BoardReport } from '../types/events';
import { LogicalRow, MarkingTarget, PhysicalLinePlacement, Rect } from '../types/whiteboard';

// Figures are always projected into sections 2+3 (x 420..1140), so student rows must stay
// in section 1: there is no "compact" mode that writes them into section 2.
export type LayoutMode = 'TEXT_ONLY_3COL' | 'DIAGRAM_STANDARD_1COL';

export interface ColumnDef {
  x: number;
  width: number;
}

export const SECTION_1_X = 40;
export const SECTION_1_WIDTH = 340;
export const SECTION_2_X = 420;
export const SECTION_2_WIDTH = 340;
export const SECTION_3_X = 800;
export const SECTION_3_WIDTH = 340;

export const WORK_COLUMN_X = SECTION_1_X;
export const WORK_COLUMN_WIDTH = SECTION_1_WIDTH;
export const WORK_START_Y = 72;
export const WORK_MAX_Y = 680;
export const WORK_CONTINUATION_INDENT = 24;
export const DEFAULT_FONT_SIZE = 24;

export class BoardLayout {
  private rootPageId: string = 'p1';
  private pageTurnCounter: number = 1; // 1 = root page, 2 = _p2, etc.
  private currentY: number = WORK_START_Y;
  private currentColumnIndex: number = 0;
  private mode: LayoutMode = 'DIAGRAM_STANDARD_1COL';
  private columns: ColumnDef[] = [{ x: SECTION_1_X, width: SECTION_1_WIDTH }];
  private rows: LogicalRow[] = [];
  private onPageTurnCallback?: (newPageId: string) => void;
  private canvasContext?: CanvasRenderingContext2D;

  constructor(
    rootPageId: string = 'p1',
    onPageTurn?: (newPageId: string) => void,
    initialMode: LayoutMode = 'DIAGRAM_STANDARD_1COL'
  ) {
    this.rootPageId = rootPageId;
    this.pageTurnCounter = 1;
    this.currentY = WORK_START_Y;
    this.currentColumnIndex = 0;
    this.rows = [];
    this.onPageTurnCallback = onPageTurn;
    this.setLayoutMode(initialMode);

    // Use offscreen canvas for exact text measurement
    if (typeof document !== 'undefined') {
      const canvas = document.createElement('canvas');
      this.canvasContext = canvas.getContext('2d') || undefined;
    }
  }

  public setLayoutMode(mode: LayoutMode): void {
    this.mode = mode;
    switch (mode) {
      case 'TEXT_ONLY_3COL':
        this.columns = [
          { x: SECTION_1_X, width: SECTION_1_WIDTH },
          { x: SECTION_2_X, width: SECTION_2_WIDTH },
          { x: SECTION_3_X, width: SECTION_3_WIDTH },
        ];
        break;
      case 'DIAGRAM_STANDARD_1COL':
      default:
        this.columns = [{ x: SECTION_1_X, width: SECTION_1_WIDTH }];
        break;
    }
  }

  /** The server's page_commit workRect chooses the work-column mode;
   *  width > 400 -> TEXT_ONLY_3COL, else DIAGRAM_STANDARD_1COL. Null = diagram rect. */
  public setWorkRect(rect: Rect | null): void {
    if (rect && rect.width > 400) this.setLayoutMode('TEXT_ONLY_3COL');
    else this.setLayoutMode('DIAGRAM_STANDARD_1COL');
  }

  public getLayoutMode(): LayoutMode {
    return this.mode;
  }

  public getDividers(): number[] {
    switch (this.mode) {
      case 'TEXT_ONLY_3COL':
        return [395, 785];
      case 'DIAGRAM_STANDARD_1COL':
      default:
        return [395];
    }
  }

  public reset(rootPageId: string) {
    this.rootPageId = rootPageId;
    this.pageTurnCounter = 1;
    this.currentColumnIndex = 0;
    this.currentY = WORK_START_Y;
    this.rows = [];
  }

  /**
   * Advance to the next page for a planned [PAGE_BREAK] without changing the layout mode or
   * root id. Mirrors the automatic overflow page turn (pageTurnCounter += 1) so `currentPageId`
   * becomes `root_p2`, `root_p3`, ... and board_report/page_ops never overwrite the prior page.
   */
  public turnPage(): string {
    this.pageTurnCounter += 1;
    this.currentColumnIndex = 0;
    this.currentY = WORK_START_Y;
    this.rows = [];
    return this.currentPageId;
  }

  public get currentPageId(): string {
    if (this.pageTurnCounter === 1) {
      return this.rootPageId;
    }
    return `${this.rootPageId}_p${this.pageTurnCounter}`;
  }

  public get visibleRows(): LogicalRow[] {
    return [...this.rows];
  }

  public get rowsRemaining(): number {
    const lineHeight = DEFAULT_FONT_SIZE * 1.6;
    const remainingYInCurrentCol = Math.max(0, WORK_MAX_Y - this.currentY);
    const rowsInCurrentCol = Math.floor(remainingYInCurrentCol / lineHeight);
    const totalLinesPerCol = Math.floor((WORK_MAX_Y - WORK_START_Y) / lineHeight);
    const remainingCols = Math.max(0, this.columns.length - 1 - this.currentColumnIndex);
    return rowsInCurrentCol + remainingCols * totalLinesPerCol;
  }

  private measureText(text: string, fontSize: number): number {
    if (this.canvasContext) {
      this.canvasContext.font = `600 ${fontSize}px Caveat, cursive`;
      return this.canvasContext.measureText(text).width;
    }
    // Fallback heuristic: avg character width ~0.55 * fontSize
    return text.length * fontSize * 0.55;
  }

  /**
   * Wrap text into lines fitting within available width, never splitting words.
   */
  private wrapText(text: string, fontSize: number, columnWidth: number): string[] {
    const words = text.split(/\s+/).filter(Boolean);
    if (words.length === 0) return [''];

    const lines: string[] = [];
    let currentLine = '';

    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const isFirstLine = lines.length === 0;
      const maxWidth = isFirstLine ? columnWidth : columnWidth - WORK_CONTINUATION_INDENT;

      const testLine = currentLine ? `${currentLine} ${word}` : word;
      const testWidth = this.measureText(testLine, fontSize);

      if (testWidth <= maxWidth || !currentLine) {
        currentLine = testLine;
      } else {
        lines.push(currentLine);
        currentLine = word;
      }
    }

    if (currentLine) {
      lines.push(currentLine);
    }

    return lines;
  }

  /**
   * Place a WRITE row in the active work column.
   * If current column overflows, spills into the next column on the right.
   * If all columns overflow, triggers page turn.
   */
  public placeRow(rawText: string, rowId: string, fontSize: number = DEFAULT_FONT_SIZE): PhysicalLinePlacement[] {
    const text = (rawText || '').replace(/[∥‖]/g, '||');
    const lineHeight = fontSize * 1.6;
    let currentCol = this.columns[this.currentColumnIndex] || this.columns[0];
    let lines = this.wrapText(text, fontSize, currentCol.width);
    let neededHeight = lines.length * lineHeight;

    // Check if row would pass y = 680
    if (this.currentY + neededHeight > WORK_MAX_Y && this.rows.length > 0) {
      if (this.currentColumnIndex + 1 < this.columns.length) {
        // Spill into next column on the right
        this.currentColumnIndex += 1;
        this.currentY = WORK_START_Y;
        currentCol = this.columns[this.currentColumnIndex];
        lines = this.wrapText(text, fontSize, currentCol.width);
        neededHeight = lines.length * lineHeight;
      } else {
        // All available columns are full -> turn to a fresh page
        this.pageTurnCounter += 1;
        this.currentColumnIndex = 0;
        this.currentY = WORK_START_Y;
        this.rows = [];
        currentCol = this.columns[0];
        lines = this.wrapText(text, fontSize, currentCol.width);
        neededHeight = lines.length * lineHeight;

        if (this.onPageTurnCallback) {
          this.onPageTurnCallback(this.currentPageId);
        }
      }
    }

    const placements: PhysicalLinePlacement[] = [];
    const minX = currentCol.x;
    let maxX = minX;
    const startY = this.currentY;

    for (let i = 0; i < lines.length; i++) {
      const lineText = lines[i];
      const x = i === 0 ? currentCol.x : currentCol.x + WORK_CONTINUATION_INDENT;
      const y = this.currentY;
      const width = this.measureText(lineText, fontSize);
      const height = lineHeight;

      placements.push({
        rowId,
        lineIndex: i,
        text: lineText,
        x,
        y,
        width,
        height,
      });

      maxX = Math.max(maxX, x + width);
      this.currentY += lineHeight;
    }

    // Add extra padding after logical row
    this.currentY += 8;

    const rowRect: Rect = {
      x: minX,
      y: startY,
      width: Math.min(currentCol.width, maxX - minX),
      height: this.currentY - startY - 8,
    };

    const logicalRow: LogicalRow = {
      rowId,
      text,
      rect: rowRect,
      lines: placements,
    };

    this.rows.push(logicalRow);
    return placements;
  }

  public getBoardReport(): BoardReport {
    return {
      type: 'board_report',
      pageId: this.currentPageId,
      rows: this.rows.map((r) => ({ rowId: r.rowId, text: r.text })),
      rowsRemaining: this.rowsRemaining,
    };
  }

  public getMarkingCandidates(anchors: DiagramAnchor[] = []): MarkingTarget[] {
    const targets: MarkingTarget[] = [];

    // Work column rows
    for (const r of this.rows) {
      targets.push({
        kind: 'work',
        id: r.rowId,
        rect: r.rect,
        text: r.text,
      });
    }

    // Diagram anchors
    for (const a of anchors) {
      targets.push({
        kind: 'diagram',
        id: a.id,
        rect: { x: a.x, y: a.y, width: a.width, height: a.height },
        text: (a.labels || []).join(', ') || a.id,
      });
    }

    return targets;
  }
}

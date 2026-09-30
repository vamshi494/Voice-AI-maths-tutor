// frontend/src/__tests__/boardLayout.test.ts
import { describe, it, expect } from 'vitest';
import { BoardLayout, WORK_COLUMN_X, WORK_START_Y, WORK_MAX_Y } from '../whiteboard/boardLayout';

describe('BoardLayout (the single layout owner)', () => {
  it('places a single row starting at work column coordinates', () => {
    const layout = new BoardLayout('page_root');
    const placements = layout.placeRow('x + 2 = 5', 'w1');

    expect(placements.length).toBe(1);
    expect(placements[0].rowId).toBe('w1');
    expect(placements[0].x).toBe(WORK_COLUMN_X);
    expect(placements[0].y).toBe(WORK_START_Y);

    const report = layout.getBoardReport();
    expect(report.pageId).toBe('page_root');
    expect(report.rows.length).toBe(1);
    expect(report.rows[0].rowId).toBe('w1');
    expect(report.rows[0].text).toBe('x + 2 = 5');
    expect(report.rowsRemaining).toBeGreaterThan(0);
  });

  it('wraps long equation into multiple physical lines with continuation indent sharing one rowId', () => {
    const layout = new BoardLayout('p1');
    // Long sentence that will wrap in 320px column
    const longText = 'We multiply both sides of the equation by five to isolate the variable x completely';
    const placements = layout.placeRow(longText, 'w2');

    expect(placements.length).toBeGreaterThan(1);
    // Every physical line belongs to w2
    for (const pl of placements) {
      expect(pl.rowId).toBe('w2');
    }
    // Continuation lines are indented
    expect(placements[1].x).toBe(WORK_COLUMN_X + 24);

    // But reported as ONE logical row
    const report = layout.getBoardReport();
    expect(report.rows.length).toBe(1);
    expect(report.rows[0].rowId).toBe('w2');
    expect(report.rows[0].text).toBe(longText);
  });

  it('handles page turn and derives sub-page IDs on overflow beyond y=680', () => {
    let turnedToPage: string | null = null;
    const layout = new BoardLayout('root123', (newPageId) => {
      turnedToPage = newPageId;
    });

    // Fill the work column until it exceeds 680 (15 rows fit ~608px)
    for (let i = 1; i <= 15; i++) {
      layout.placeRow(`Step ${i}: calculation row`, `w${i}`);
    }

    // Overflow should have occurred
    expect(layout.currentPageId).toBe('root123_p2');
    expect(turnedToPage).toBe('root123_p2');

    // Continuing to add rows derives _p3
    for (let i = 16; i <= 30; i++) {
      layout.placeRow(`Step ${i}: further row`, `w${i}`);
    }
    expect(layout.currentPageId).toBe('root123_p3');
  });

  it('spills across 3 sections in TEXT_ONLY_3COL mode without prematurely turning page', () => {
    const layout = new BoardLayout('root_3col', undefined, 'TEXT_ONLY_3COL');

    // Fill column 1 (~15 rows)
    for (let i = 1; i <= 15; i++) {
      layout.placeRow(`Col1 Step ${i}`, `w${i}`);
    }

    // Row 15 should have spilled into Column 2 (x=420)
    const rows = layout.visibleRows;
    expect(rows.length).toBe(15);
    const col2Row = rows[14]; // 15th row
    expect(col2Row.lines[0].x).toBe(420);
    // Page has NOT turned because Column 2 has space!
    expect(layout.currentPageId).toBe('root_3col');

    // Add more rows to fill column 2 and spill into column 3 (x=800)
    for (let i = 16; i <= 30; i++) {
      layout.placeRow(`Col2/3 Step ${i}`, `w${i}`);
    }
    const allRows = layout.visibleRows;
    expect(allRows.length).toBe(30);
    const col3Row = allRows[29]; // 30th row
    expect(col3Row.lines[0].x).toBe(800);
    expect(layout.currentPageId).toBe('root_3col');
  });

  it('provides correct dynamic divider lines per layout mode', () => {
    const layout = new BoardLayout('p1');
    expect(layout.getDividers()).toEqual([395]);

    layout.setLayoutMode('TEXT_ONLY_3COL');
    expect(layout.getDividers()).toEqual([395, 785]);

    // DIAGRAM_COMPACT_2COL is not a layout mode: rows would land in the diagram zone.
  });
});


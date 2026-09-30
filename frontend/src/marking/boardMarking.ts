// frontend/src/marking/boardMarking.ts
// Mark & Ask: classify a pen stroke and resolve it to a work row or diagram anchor.

import { DoubtMark, MarkGesture, MarkTargetKind } from '../types/events';
import { MarkingTarget, Rect } from '../types/whiteboard';

export interface Point {
  x: number;
  y: number;
  t?: number;
}

export interface Stroke {
  points: Point[];
}

/**
 * Check if point is inside a rectangle.
 */
export function pointInRect(p: Point, rect: Rect): boolean {
  return p.x >= rect.x && p.x <= rect.x + rect.width && p.y >= rect.y && p.y <= rect.y + rect.height;
}

/**
 * Calculate bounding box of stroke.
 */
export function getStrokeBounds(points: Point[]): Rect {
  if (points.length === 0) return { x: 0, y: 0, width: 0, height: 0 };
  let minX = points[0].x;
  let maxX = points[0].x;
  let minY = points[0].y;
  let maxY = points[0].y;

  for (let i = 1; i < points.length; i++) {
    const p = points[i];
    minX = Math.min(minX, p.x);
    maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }

  return {
    x: minX,
    y: minY,
    width: maxX - minX,
    height: maxY - minY,
  };
}

/**
 * Compute total length of stroke path.
 */
export function getStrokeLength(points: Point[]): number {
  let len = 0;
  for (let i = 1; i < points.length; i++) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    len += Math.sqrt(dx * dx + dy * dy);
  }
  return len;
}

/**
 * Classify a stroke gesture: circle | underline | strike | scribble | point.
 * Heuristic thresholds and shape tests.
 */
export function classifyGesture(points: Point[]): MarkGesture {
  if (points.length <= 3) {
    return 'point';
  }

  const bounds = getStrokeBounds(points);
  const pathLength = getStrokeLength(points);
  const diag = Math.sqrt(bounds.width * bounds.width + bounds.height * bounds.height);

  // Point / Tap: very short stroke
  if (pathLength < 16 && diag < 16) {
    return 'point';
  }

  // Underline / Strike: wide, low aspect ratio (width >> height)
  if (bounds.width > 24 && bounds.height < 30 && bounds.width / Math.max(bounds.height, 1) > 2.2) {
    // Underline if flat, strike if straight through
    return 'underline';
  }

  // Circle: start and end points close together compared to perimeter
  const start = points[0];
  const end = points[points.length - 1];
  const closureDist = Math.sqrt((start.x - end.x) ** 2 + (start.y - end.y) ** 2);

  if (closureDist < diag * 0.45 && pathLength > diag * 1.5) {
    return 'circle';
  }

  // Scribble: path length much longer than perimeter of bounds (lots of back-and-forth)
  const perimeter = 2 * (bounds.width + bounds.height);
  if (pathLength > perimeter * 1.6) {
    return 'scribble';
  }

  return 'circle'; // default enclosure gesture
}

/**
 * Resolve a classified stroke against marking candidates (work rows and diagram anchors).
 */
export function resolveStrokeToTarget(
  stroke: Stroke,
  gesture: MarkGesture,
  candidates: MarkingTarget[]
): DoubtMark {
  const bounds = getStrokeBounds(stroke.points);

  // Find candidate with maximum intersection or closest distance
  let bestCandidate: MarkingTarget | null = null;
  let maxScore = -1;

  for (const cand of candidates) {
    const interX = Math.max(0, Math.min(bounds.x + bounds.width, cand.rect.x + cand.rect.width) - Math.max(bounds.x, cand.rect.x));
    const interY = Math.max(0, Math.min(bounds.y + bounds.height, cand.rect.y + cand.rect.height) - Math.max(bounds.y, cand.rect.y));
    const interArea = interX * interY;

    if (interArea > maxScore && interArea > 0) {
      maxScore = interArea;
      bestCandidate = cand;
    }
  }

  // If no intersection, check if any point lands inside a candidate
  if (!bestCandidate) {
    for (const p of stroke.points) {
      for (const cand of candidates) {
        if (pointInRect(p, cand.rect)) {
          bestCandidate = cand;
          break;
        }
      }
      if (bestCandidate) break;
    }
  }

  if (bestCandidate) {
    if (bestCandidate.kind === 'work') {
      return {
        gesture,
        targetKind: 'work',
        rowId: bestCandidate.id,
        text: bestCandidate.text,
      };
    } else {
      return {
        gesture,
        targetKind: 'diagram',
        entityId: bestCandidate.id,
        text: bestCandidate.text,
      };
    }
  }

  // Landed on empty area
  return {
    gesture,
    targetKind: 'empty',
    text: '',
  };
}

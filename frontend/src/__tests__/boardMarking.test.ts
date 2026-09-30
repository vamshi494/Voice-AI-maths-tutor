// frontend/src/__tests__/boardMarking.test.ts
import { describe, it, expect } from 'vitest';
import { classifyGesture, resolveStrokeToTarget, Point, Stroke } from '../marking/boardMarking';
import { MarkingTarget } from '../types/whiteboard';

describe('boardMarking gesture classification and target resolution', () => {
  it('classifies point (tap) gesture', () => {
    const tapPoints: Point[] = [
      { x: 100, y: 100 },
      { x: 101, y: 101 },
    ];
    expect(classifyGesture(tapPoints)).toBe('point');
  });

  it('classifies underline gesture', () => {
    // Horizontal wide stroke, low height
    const underlinePoints: Point[] = [
      { x: 50, y: 120 },
      { x: 100, y: 121 },
      { x: 150, y: 120 },
      { x: 200, y: 122 },
    ];
    expect(classifyGesture(underlinePoints)).toBe('underline');
  });

  it('classifies circle gesture', () => {
    // Circular loop ending near start
    const circlePoints: Point[] = [
      { x: 100, y: 50 },
      { x: 150, y: 100 },
      { x: 100, y: 150 },
      { x: 50, y: 100 },
      { x: 98, y: 52 }, // Close to start (100, 50)
    ];
    expect(classifyGesture(circlePoints)).toBe('circle');
  });

  it('classifies scribble gesture', () => {
    // Many back-and-forth zig-zags with high path length relative to bounding perimeter
    const scribblePoints: Point[] = [];
    for (let i = 0; i < 20; i++) {
      scribblePoints.push({ x: 50 + (i % 2) * 40, y: 50 + i * 2 });
    }
    expect(classifyGesture(scribblePoints)).toBe('scribble');
  });

  it('resolves stroke to work target', () => {
    const candidates: MarkingTarget[] = [
      {
        kind: 'work',
        id: 'w1',
        rect: { x: 40, y: 72, width: 200, height: 38 },
        text: '2x + 3 = 7',
      },
    ];

    const stroke: Stroke = {
      points: [
        { x: 50, y: 80 },
        { x: 150, y: 80 },
        { x: 150, y: 100 },
        { x: 50, y: 100 },
        { x: 50, y: 80 },
      ],
    };

    const mark = resolveStrokeToTarget(stroke, 'circle', candidates);
    expect(mark.targetKind).toBe('work');
    expect(mark.rowId).toBe('w1');
    expect(mark.text).toBe('2x + 3 = 7');
  });

  it('resolves stroke to diagram target', () => {
    const candidates: MarkingTarget[] = [
      {
        kind: 'diagram',
        id: 'seg_AB',
        rect: { x: 450, y: 100, width: 80, height: 40 },
        text: 'side AB',
      },
    ];

    const stroke: Stroke = {
      points: [
        { x: 460, y: 110 },
        { x: 462, y: 112 },
      ],
    };

    const mark = resolveStrokeToTarget(stroke, 'point', candidates);
    expect(mark.targetKind).toBe('diagram');
    expect(mark.entityId).toBe('seg_AB');
    expect(mark.text).toBe('side AB');
  });

  it('resolves mark on empty area', () => {
    const candidates: MarkingTarget[] = [
      {
        kind: 'work',
        id: 'w1',
        rect: { x: 40, y: 72, width: 200, height: 38 },
      },
    ];

    const stroke: Stroke = {
      points: [
        { x: 800, y: 600 },
        { x: 801, y: 601 },
      ],
    };

    const mark = resolveStrokeToTarget(stroke, 'point', candidates);
    expect(mark.targetKind).toBe('empty');
    expect(mark.rowId).toBeUndefined();
    expect(mark.entityId).toBeUndefined();
  });
});

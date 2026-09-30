// frontend/src/marking/useBoardMarking.ts
// Mouse/touch gesture hook for pen marking.

import { useState, useCallback, useRef } from 'react';
import { DoubtMark } from '../types/events';
import { MarkingTarget } from '../types/whiteboard';
import { classifyGesture, Point, resolveStrokeToTarget, Stroke } from './boardMarking';

/** Candidates may be a getter: resolved at stroke END, so a figure committed after the last
 *  React render is still hittable (a render-time array goes stale on every diagram commit). */
export type CandidateSource = MarkingTarget[] | (() => MarkingTarget[]);

export function useBoardMarking(
  candidates: CandidateSource,
  scale: number = 1,
  enabled: boolean = true
) {
  const [marks, setMarks] = useState<DoubtMark[]>([]);
  const [currentStroke, setCurrentStroke] = useState<Point[] | null>(null);
  const isDrawing = useRef(false);

  const startStroke = useCallback(
    (clientX: number, clientY: number, canvasRect: DOMRect) => {
      if (!enabled) return;
      isDrawing.current = true;
      const logicalX = (clientX - canvasRect.left) / scale;
      const logicalY = (clientY - canvasRect.top) / scale;
      setCurrentStroke([{ x: logicalX, y: logicalY, t: Date.now() }]);
    },
    [enabled, scale]
  );

  const moveStroke = useCallback(
    (clientX: number, clientY: number, canvasRect: DOMRect) => {
      if (!isDrawing.current || !enabled) return;
      const logicalX = (clientX - canvasRect.left) / scale;
      const logicalY = (clientY - canvasRect.top) / scale;
      setCurrentStroke((prev) => (prev ? [...prev, { x: logicalX, y: logicalY, t: Date.now() }] : null));
    },
    [enabled, scale]
  );

  const endStroke = useCallback(() => {
    if (!isDrawing.current || !currentStroke || currentStroke.length === 0) {
      isDrawing.current = false;
      setCurrentStroke(null);
      return;
    }

    isDrawing.current = false;
    const stroke: Stroke = { points: currentStroke };
    const gesture = classifyGesture(currentStroke);
    const targets = typeof candidates === 'function' ? candidates() : candidates;
    const resolvedMark = resolveStrokeToTarget(stroke, gesture, targets);

    setMarks((prev) => [...prev, resolvedMark]);
    setCurrentStroke(null);
  }, [currentStroke, candidates]);

  const clearMarks = useCallback(() => {
    setMarks([]);
    setCurrentStroke(null);
  }, []);

  return {
    marks,
    currentStroke,
    startStroke,
    moveStroke,
    endStroke,
    clearMarks,
  };
}

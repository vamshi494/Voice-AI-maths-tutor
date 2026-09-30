// frontend/src/marking/BoardMarkingLayer.tsx
// Canvas overlay for pen marking. Uses a plain canvas, never a Konva layer.

import React, { useRef, useEffect } from 'react';
import { DoubtMark } from '../types/events';
import { CandidateSource, useBoardMarking } from './useBoardMarking';

interface Props {
  width: number;
  height: number;
  scale: number;
  candidates: CandidateSource;
  canMark: boolean;
  onSubmitMarks: (marks: DoubtMark[]) => void;
  onExplain?: () => void;
  /** First pen-down: the student picked up the marker (server pauses the tutor). */
  onStrokeStart?: () => void;
  /** Student pressed Clear. */
  onMarksCleared?: () => void;
  /** Parent increments this after a doubt is sent, so old marks never ride along again. */
  clearSignal?: number;
}

export const BoardMarkingLayer: React.FC<Props> = ({
  width,
  height,
  scale,
  candidates,
  canMark,
  onSubmitMarks,
  onExplain,
  onStrokeStart,
  onMarksCleared,
  clearSignal = 0,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { marks, currentStroke, startStroke, moveStroke, endStroke, clearMarks } =
    useBoardMarking(candidates, scale, canMark);

  useEffect(() => {
    if (clearSignal > 0) clearMarks();
  }, [clearSignal, clearMarks]);

  // Synchronize live marks to parent whenever marks change
  useEffect(() => {
    onSubmitMarks(marks);
  }, [marks, onSubmitMarks]);

  // Render current stroke on canvas
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, width, height);

    // Draw active stroke with rough pencil / chalk style
    if (currentStroke && currentStroke.length > 1) {
      ctx.save();
      ctx.scale(scale, scale);
      ctx.strokeStyle = '#f43f5e'; // Coral chalk for student doubt marks
      ctx.lineWidth = 3;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.shadowColor = 'rgba(244, 63, 94, 0.4)';
      ctx.shadowBlur = 4;

      ctx.beginPath();
      ctx.moveTo(currentStroke[0].x, currentStroke[0].y);
      for (let i = 1; i < currentStroke.length; i++) {
        ctx.lineTo(currentStroke[i].x, currentStroke[i].y);
      }
      ctx.stroke();
      ctx.restore();
    }
  }, [currentStroke, width, height, scale]);

  const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!canMark) return;
    const rect = e.currentTarget.getBoundingClientRect();
    onStrokeStart?.();
    startStroke(e.clientX, e.clientY, rect);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    moveStroke(e.clientX, e.clientY, rect);
  };

  const handlePointerUp = () => {
    endStroke();
  };

  return (
    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width,
        height,
        pointerEvents: canMark ? 'auto' : 'none',
        zIndex: 20,
      }}
    >
      <canvas
        ref={canvasRef}
        width={width}
        height={height}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        style={{
          width: '100%',
          height: '100%',
          cursor: canMark ? 'crosshair' : 'default',
          touchAction: 'none',
        }}
      />

      {/* Floating Mark & Ask prompt badge when marks exist */}
      {marks.length > 0 && (
        <div
          className="glass-panel"
          style={{
            position: 'absolute',
            bottom: 16,
            right: 16,
            padding: '8px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            zIndex: 25,
            pointerEvents: 'auto',
          }}
        >
          <span style={{ fontSize: 13, color: '#fef3c7' }}>
            {marks.length} part{marks.length > 1 ? 's' : ''} marked
          </span>
          <button
            onClick={() => {
              onSubmitMarks(marks);
              if (onExplain) onExplain();
              clearMarks();
            }}
            style={{
              backgroundColor: '#f59e0b',
              color: '#000',
              fontWeight: 600,
              fontSize: 13,
              border: 'none',
              borderRadius: 8,
              padding: '6px 14px',
              cursor: 'pointer',
            }}
          >
            Explain this
          </button>
          <button
            onClick={() => {
              clearMarks();
              onMarksCleared?.();
            }}
            style={{
              backgroundColor: 'transparent',
              color: '#cbd5e1',
              fontSize: 12,
              border: 'none',
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
          >
            Clear
          </button>
        </div>
      )}
    </div>
  );
};

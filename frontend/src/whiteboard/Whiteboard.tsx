// frontend/src/whiteboard/Whiteboard.tsx
// 6-layer Konva stage (1200x700 logical)

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Stage } from 'react-konva';
import Konva from 'konva';
import { BackgroundLayer } from './layers/BackgroundLayer';
import { HighlightLayer } from './layers/HighlightLayer';
import { DrawLayer } from './layers/DrawLayer';
import { AnimLayer } from './layers/AnimLayer';
import { SpotlightLayer } from './layers/SpotlightLayer';
import { CursorLayer } from './layers/CursorLayer';
import { BoardLayout } from './boardLayout';
import { TransactionManager } from './drawTransactions';
import { CommandExecutor } from './commandExecutor';
import { BoardMarkingLayer } from '../marking/BoardMarkingLayer';
import { DoubtMark } from '../types/events';

export const LOGICAL_WIDTH = 1200;
export const LOGICAL_HEIGHT = 700;

interface WhiteboardProps {
  onSendReport?: (report: any) => void;
  onSubmitDoubtMarks?: (marks: DoubtMark[]) => void;
  onExplainMarks?: () => void;
  canMark?: boolean;
  onExecutorReady?: (executor: CommandExecutor, layout: BoardLayout) => void;
  onStrokeStart?: () => void;
  onMarksCleared?: () => void;
  clearMarksSignal?: number;
}

export const Whiteboard: React.FC<WhiteboardProps> = ({
  onSendReport = () => {},
  onSubmitDoubtMarks = () => {},
  onExplainMarks,
  canMark = true,
  onExecutorReady,
  onStrokeStart,
  onMarksCleared,
  clearMarksSignal = 0,
}) => {
  // Re-render when the executor changes layout mode (dividers are derived from it).
  const [, setLayoutTick] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<Konva.Stage>(null);

  const highlightLayerRef = useRef<Konva.Layer>(null);
  const drawLayerRef = useRef<Konva.Layer>(null);
  const animLayerRef = useRef<Konva.Layer>(null);
  const spotlightLayerRef = useRef<Konva.Layer>(null);
  const cursorLayerRef = useRef<Konva.Layer>(null);

  const [dimensions, setDimensions] = useState({
    width: LOGICAL_WIDTH,
    height: LOGICAL_HEIGHT,
    scale: 1,
  });

  const layoutRef = useRef<BoardLayout>(new BoardLayout('p1', (newPageId) => {
    // Page turn callback on overflow
    console.log('[board_page_turn] Overflow page turn:', newPageId);
    executorRef.current?.onOverflowTurn(newPageId);
  }, 'TEXT_ONLY_3COL'));

  const txManagerRef = useRef<TransactionManager>(new TransactionManager());
  const executorRef = useRef<CommandExecutor | null>(null);

  // Resize handler for uniform scaling & letterboxing
  useEffect(() => {
    const updateSize = () => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const containerWidth = rect.width;
      const containerHeight = rect.height;

      const scale = Math.min(
        containerWidth / LOGICAL_WIDTH,
        containerHeight / LOGICAL_HEIGHT
      );

      setDimensions({
        width: LOGICAL_WIDTH * scale,
        height: LOGICAL_HEIGHT * scale,
        scale,
      });
    };

    updateSize();
    window.addEventListener('resize', updateSize);
    return () => window.removeEventListener('resize', updateSize);
  }, []);

  // Keep the latest callbacks in refs: the executor is built once and must never capture a
  // stale closure when the parent re-renders with a new inline callback identity.
  const onSendReportRef = useRef(onSendReport);
  const onExecutorReadyRef = useRef(onExecutorReady);
  useEffect(() => {
    onSendReportRef.current = onSendReport;
    onExecutorReadyRef.current = onExecutorReady;
  }, [onSendReport, onExecutorReady]);

  // Initialize TransactionManager and CommandExecutor on mount.
  // The executor owns the active diagram/anchors/highlight state in memory. Depending on the
  // inline `onSendReport` prop would replace the executor on every App re-render with a blank
  // one and silently disable every [FOCUS] highlight. With EMPTY deps, parent re-renders keep
  // the executor alive.
  //
  // Do NOT add a `if (executorRef.current) return;` guard and do NOT add a cleanup that
  // destroys Konva layers. Under React 18 StrictMode (enabled in main.tsx) react-konva's
  // StageWrap layout effect destroys the Konva.Stage on the simulated unmount and builds a
  // NEW one on remount (ReactKonvaCore.js `stage.current.destroy()`). The effect must run
  // again on that remount so the executor rebinds to the fresh layers; a ref guard would
  // leave every op drawing into the destroyed stage and the board would stay blank.
  useEffect(() => {
    if (
      animLayerRef.current &&
      drawLayerRef.current &&
      highlightLayerRef.current &&
      spotlightLayerRef.current &&
      cursorLayerRef.current
    ) {
      txManagerRef.current.setLayers(animLayerRef.current, drawLayerRef.current);

      const executor = new CommandExecutor({
        layout: layoutRef.current,
        txManager: txManagerRef.current,
        drawLayer: drawLayerRef.current,
        animLayer: animLayerRef.current,
        spotlightLayer: spotlightLayerRef.current,
        highlightLayer: highlightLayerRef.current,
        cursorLayer: cursorLayerRef.current,
        onSendReport: (report) => onSendReportRef.current(report),
      });

      executorRef.current = executor;
      executor.onLayoutChanged = () => setLayoutTick((t) => t + 1);
      onExecutorReadyRef.current?.(executor, layoutRef.current);
    }
  }, []);

  // Evaluated when a stroke ENDS, not at render time: committing a figure does not re-render
  // React, so a render-time array would have no diagram targets until something else changed.
  const getMarkingCandidates = useCallback(
    () => layoutRef.current.getMarkingCandidates(executorRef.current?.activeAnchors || []),
    []
  );

  return (
    <div
      ref={containerRef}
      className="stage-wrapper"
      style={{
        width: '100%',
        height: '100%',
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        className="konva-board-shadow"
        style={{
          width: dimensions.width,
          height: dimensions.height,
          position: 'relative',
        }}
      >
        {/* 6-Layer Konva Stage (1200x700 scaled) */}
        <Stage
          ref={stageRef}
          width={dimensions.width}
          height={dimensions.height}
          scaleX={dimensions.scale}
          scaleY={dimensions.scale}
        >
          {/* 1. Background */}
          <BackgroundLayer dividers={layoutRef.current?.getDividers() || [395, 785]} />
          {/* 2. Highlight */}
          <HighlightLayer layerRef={highlightLayerRef} />
          {/* 3. Draw (authoritative committed ink) */}
          <DrawLayer layerRef={drawLayerRef} />
          {/* 4. Anim (in-flight animation) */}
          <AnimLayer layerRef={animLayerRef} />
          {/* 5. Spotlight (veil, cutouts, focus) */}
          <SpotlightLayer layerRef={spotlightLayerRef} />
          {/* 6. Cursor (virtual cursor, duster) */}
          <CursorLayer layerRef={cursorLayerRef} />
        </Stage>

        {/* Mark & Ask Overlay: Canvas positioned directly above Stage */}
        <BoardMarkingLayer
          width={dimensions.width}
          height={dimensions.height}
          scale={dimensions.scale}
          candidates={getMarkingCandidates}
          canMark={canMark}
          onSubmitMarks={onSubmitDoubtMarks}
          onExplain={onExplainMarks}
          onStrokeStart={onStrokeStart}
          onMarksCleared={onMarksCleared}
          clearSignal={clearMarksSignal}
        />
      </div>
    </div>
  );
};

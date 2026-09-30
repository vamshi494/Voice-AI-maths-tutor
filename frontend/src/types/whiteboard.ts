// frontend/src/types/whiteboard.ts
import { DiagramAnchor, VerifiedDiagram } from './events';

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PhysicalLinePlacement {
  rowId: string;
  lineIndex: number;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LogicalRow {
  rowId: string;
  text: string;
  rect: Rect;
  lines: PhysicalLinePlacement[];
}

export interface MarkingTarget {
  kind: 'work' | 'diagram';
  id: string; // rowId or entityId
  rect: Rect;
  text?: string;
}

export interface WhiteboardDimensions {
  width: number;
  height: number;
  scale: number;
  offsetX: number;
  offsetY: number;
}

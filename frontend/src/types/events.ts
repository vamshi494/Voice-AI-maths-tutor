// frontend/src/types/events.ts
// TypeScript mirror of backend Pydantic contracts. Every shape here was checked
// against backend JSON produced by model_dump_json(by_alias=True, exclude_none=True)
// (backend/tests/test_geometry_pipeline_offline.py pins the same wire shape).
import type { Rect } from './whiteboard';

export type OpKind = 'WRITE' | 'FOCUS' | 'EMPHASIZE' | 'ANNOTATE' | 'PAUSE' | 'PAGE_BREAK';

export interface BoardOp {
  opId: string;
  kind: OpKind;
  atWord: number;
  text?: string;
  rowId?: string;
  durationMs?: number;
  entityId?: string;
  focusMode?: 'outline' | 'spotlight' | 'pulse';
  emphasizeRowId?: string;
  pageTitle?: string;
}

export interface Step {
  turnId: string;
  generation: number;
  stepIndex: number;
  spokenText: string;
  words: string[];
  ops: BoardOp[];
  sourceStepIndex?: number;
  isLast?: boolean;
}

/**
 * One drawing primitive in BOARD coordinates (1200x700 logical). Param layouts:
 *  DRAW_POINT            [x, y, r]
 *  DRAW_LINE / DRAW_RAY  [x1, y1, x2, y2]            (already clipped to the diagram zone)
 *  DRAW_CIRCLE           [cx, cy, r]
 *  DRAW_ARC              [cx, cy, r, startDeg, endDeg]   screen angles, sweep start -> end increasing
 *  DRAW_POLYLINE         [x1, y1, x2, y2, ...]          (closed polygons repeat the first point)
 *  DRAW_ANGLE_MARK       [vx, vy, r, dir1Deg, dir2Deg]  draw the minor arc between the arms
 *  DRAW_RIGHT_ANGLE_MARK [vx, vy, size, dir1Deg, dir2Deg]
 *  DRAW_TICK             [mx, my, segmentDirDeg, count]
 *  DRAW_DIMENSION        [x1, y1, x2, y2, offsetPx]    + text
 *  DRAW_AXES             [x0, y0, x1, y1, ox, oy, unitPx]  (x0,y0) = (xmin,ymin) corner
 *  DRAW_CURVE            [x1, y1, x2, y2, ...]
 *  DRAW_NUMBER_LINE      [x1, y, x2, tickPx, minValue, tickStep]
 *  LABEL                 [x, y, fontSize]              + text (x, y = top-left)
 */
export interface DiagramCommand {
  type: string;
  params: number[];
  text?: string;
  anchorId?: string;
  semanticRef?: { entityId: string; role?: string };
  visualStyle?: Record<string, unknown>;
}

/** Hit/focus box in board coordinates, as x/y/width/height (never a
 *  [minX,minY,maxX,maxY] box). */
export interface DiagramAnchor {
  id: string;
  labels: string[];
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RevealGroup {
  targetId: string;
  commandIndices: number[];
  narration?: string;
  kind?: string;
}

export interface LabelFact {
  symbol: string;
  title: string;
  value?: string;
  provenance?: string;
}

export interface DeferredAnnotation {
  entityId: string;
  commands: DiagramCommand[];
}

export interface VerifiedDiagram {
  id?: string;
  name: string;
  namespace?: string;
  commands: DiagramCommand[];
  anchors: DiagramAnchor[];
  reveals?: RevealGroup[];
  deferredAnnotations?: DeferredAnnotation[];
  labelGlossary?: Record<string, LabelFact>;
  aliasMap?: Record<string, string>;
  caption?: string;
  promptAddon?: string;
}

export type MarkGesture = 'circle' | 'underline' | 'strike' | 'scribble' | 'point';
export type MarkTargetKind = 'work' | 'diagram' | 'empty';

export interface DoubtMark {
  gesture: MarkGesture;
  targetKind: MarkTargetKind;
  rowId?: string;
  entityId?: string;
  text?: string;
}

// ---- Server -> Client Wire Events ("tutor.events" topic) ----
// Every event carries `generation` and a per-session monotonic `seq` (dedupe key).

interface EvtBase {
  generation: number;
  seq?: number;
  epoch?: string;
}

export interface TurnStartedEvent extends EvtBase {
  type: 'turn_started';
  turnId: string;
  kind: 'lesson' | 'doubt' | 'resume';
  pageId: string;
  newPage: boolean;
  lessonId?: string;
  pageIndex?: number;
  pageTitle?: string;
  sourceRunId?: string;
}

export interface DiagramCommitEvent extends EvtBase {
  type: 'diagram_commit';
  reference?: boolean;
  turnId: string;
  pageId: string;
  diagram: VerifiedDiagram;
}

export interface Block {
  id: string;
  role: 'figure' | 'table' | 'text';
  rect: Rect;
  sticky?: boolean;
  commands?: DiagramCommand[];
  anchors?: DiagramAnchor[];
  reveals?: RevealGroup[];
  deferredAnnotations?: DeferredAnnotation[];
  labelGlossary?: Record<string, LabelFact>;
  aliasMap?: Record<string, string>;
  namespace?: string;
  textLines?: string[];
  revealedIds?: string[];
}

export interface PageCommitEvent extends EvtBase {   // idempotent by (pageId, commitId)
  type: 'page_commit';
  turnId: string;
  pageId: string;
  commitId: string;
  workRect: Rect | null;
  blocks: Block[];
  carriedIds?: string[];
  reference?: boolean;
}

export interface StepEvent extends EvtBase {
  type: 'step';
  step: Step;
}

export interface TurnEndedEvent extends EvtBase {
  type: 'turn_ended';
  turnId: string;
  status: 'complete' | 'partial';
  visualStatus: 'validated' | 'text_only' | 'retry_required';
  pageOnly?: boolean;         // a non-last chapter page ended
}

export interface TurnCancelledEvent extends EvtBase {
  type: 'turn_cancelled';
  turnId: string;
  keepBoard: boolean;
}

export interface ReplayFromStepEvent extends EvtBase {
  type: 'replay_from_step';
  turnId: string;
  stepIndex: number;
}

export interface PageRestoreEvent extends EvtBase {
  type: 'page_restore';
  pageId: string;
  diagram?: VerifiedDiagram | null;
  ops: BoardOp[];
}

export interface ConvStateEvent extends EvtBase {
  type: 'conv_state';
  state: string;
  doubtAwaitingResolution: boolean;
  canContinueLesson: boolean;
  holds?: string[];
  audioMode?: 'voice' | 'captions';
  lessonId?: string | null;
  pageIndex?: number | null;
  pageCount?: number | null;
}

export interface QuestionDraftEvent extends EvtBase {
  type: 'question_draft';
  text: string;
}

export interface NoticeEvent extends EvtBase {
  type: 'notice';
  message: string;
}

export type AsideKind = 'greeting' | 'welcome' | 'redirect' | 'goodbye' | 'bridge' | 'filler';

export interface AsideEvent extends EvtBase {
  type: 'aside';
  phase: 'start' | 'end';
  kind: AsideKind;
}

export interface SnapshotSubPage {
  subId: string;
  ops: BoardOp[];
}

export interface SnapshotPage {
  pageId: string;
  title?: string;
  commit?: PageCommitEvent | null;     // multi-block page commit
  diagram?: VerifiedDiagram | null;
  subPages: SnapshotSubPage[];
}

export interface PageHeader {
  pageId: string;
  index: number;
  title: string;
}

export interface BoardSnapshotEvent extends EvtBase {
  type: 'board_snapshot';
  current: SnapshotPage;
  stack: PageHeader[];
  activeTurn?: TurnStartedEvent | null;
  pendingSteps: Step[];
  canContinueLesson: boolean;
}

export interface AudioStatusEvent extends EvtBase {
  type: 'audio_status';
  mode: 'voice' | 'captions';
  reason?: string;
}

export interface SessionEndedEvent extends EvtBase {
  type: 'session_ended';
  reason: 'opened_elsewhere' | 'ended';
}

export interface LessonPlanEvent extends EvtBase {
  type: 'lesson_plan';
  lessonId: string;
  title: string;
  pages: PageHeader[];
}

export type TutorWireEvent =
  | TurnStartedEvent
  | DiagramCommitEvent
  | PageCommitEvent
  | StepEvent
  | TurnEndedEvent
  | TurnCancelledEvent
  | ReplayFromStepEvent
  | PageRestoreEvent
  | ConvStateEvent
  | QuestionDraftEvent
  | NoticeEvent
  | AsideEvent
  | BoardSnapshotEvent
  | AudioStatusEvent
  | SessionEndedEvent
  | LessonPlanEvent;

// ---- Client -> Server Wire Reports ("tutor.report" topic) ----

export interface StepAckReport {
  type: 'step_ack';
  turnId: string;
  generation: number;
  stepIndex: number;
  drawnOpIds: string[];
}

export interface StepProgressReport {
  type: 'step_progress';
  turnId: string;
  generation: number;
  stepIndex: number;
  event: 'started' | 'completed' | 'final';
  completedBy?: 'words' | 'stall' | 'flush' | 'caption';
  drawnOpIds: string[];
  startedUpTo: number;
  heardUpTo: number;
  firstWordMs?: number;
  lastWordMs?: number;
  earlyOps: number;
  lookaheadJumps: number;
}

export interface BoardReport {
  type: 'board_report';
  pageId: string;
  rows: Array<{ rowId: string; text: string }>;
  rowsRemaining: number;
}

export interface ResyncRequestReport {
  type: 'resync_request';
  epoch: string | null;
  lastSeq: number;
  reason: 'gap' | 'reconnected' | 'epoch';
  wantSnapshot: boolean;
}

export interface ClientErrorReport {
  type: 'client_error';
  where: 'render' | 'sequencer' | 'audio';
  message: string;
  turnId?: string;
  blockId?: string;
}

export interface PageTurnedReport {
  type: 'page_turned';
  rootPageId: string;
  fromSubId: string;
  toSubId: string;
  cause: 'overflow' | 'page_break';
  atOpId?: string;
}

export type TutorClientReport =
  | StepAckReport
  | StepProgressReport
  | BoardReport
  | PageTurnedReport
  | ResyncRequestReport
  | ClientErrorReport;

// ---- RPC responses ----

export type RpcAckReason = 'not_now' | 'nothing_to_continue' | 'session_ended' | 'speed_unsupported';

export interface RpcAck {
  ok: boolean;
  reason?: RpcAckReason | null;
}

export interface RpcSubmitQuestionPayload {
  text: string;
  intent?: 'new' | 'auto' | 'topic';
}

export type RpcHoldReason = 'marker' | 'user_pause' | 'audio_blocked';

export interface RpcHoldPayload {
  reason: RpcHoldReason;
}

// frontend/src/whiteboard/commandExecutor.ts
// Command executor: animates board ops on the Konva canvas

import Konva from 'konva';
import gsap from 'gsap';
import { Block, BoardOp, DiagramAnchor, DiagramCommand, PageCommitEvent, PageTurnedReport, Step, StepProgressReport, VerifiedDiagram } from '../types/events';
import { Rect } from '../types/whiteboard';
import { BoardLayout } from './boardLayout';
import { BoardLedger } from './boardLedger';
import { TransactionManager } from './drawTransactions';
import { RevealClock, StagedGroup } from './revealClock';
import { CompletedBy, EMA_MAX_MS, EMA_MIN_MS } from './transcriptSync';

/** Sections 2 + 3 of the board: must match backend scene_engine/project.py ZONE_* */
export const DIAGRAM_ZONE = { x: 420, y: 40, width: 720, height: 600 };

/** A page-commit block that left the page fades out over this many ms before it is destroyed. */
export const BLOCK_FADE_MS = 300;

/** A carried sticky block tweens from its old rect to the new one over this long. */
export const STICKY_TWEEN_MS = 500;

/** Table rendering. */
export const TABLE_ROW_H = 36;
export const TABLE_FONT_PX = 18;
export const TABLE_ROW_H_SMALL = 30;
export const TABLE_FONT_PX_SMALL = 16;

/** Simultaneous [FOCUS] highlights; the oldest fades on the third. */
export const FOCUS_CAP = 2;

/** Style override so a highlight renders ANY DiagramCommand type. */
export interface DiagramRenderStyle {
  stroke: string;
  strokeWidthMul: number;
  shadowBlur: number;
  fill: string;
}
const CHALK = '#fef3c7';
const POINT = '#67e8f9';
const MARK = '#fbbf24';
const AXIS = '#cbd5e1';
const LABEL_FILL = '#fde047';
const FONT = 'Caveat, cursive';

/** Stroked circular arc from startDeg to endDeg (screen angles, clockwise-positive y-down). */
function arcShape(cx: number, cy: number, r: number, startDeg: number, endDeg: number, stroke: string, width: number): Konva.Shape {
  return new Konva.Shape({
    stroke,
    strokeWidth: width,
    lineCap: 'round',
    sceneFunc: (ctx, shape) => {
      ctx.beginPath();
      ctx.arc(cx, cy, r, (startDeg * Math.PI) / 180, (endDeg * Math.PI) / 180, false);
      ctx.strokeShape(shape);
    },
    // hit region is irrelevant (layers are non-listening); keep bounds for getClientRect
    x: 0,
    y: 0,
  });
}

export interface CommandExecutorOptions {
  layout: BoardLayout;
  txManager: TransactionManager;
  drawLayer: Konva.Layer;
  animLayer: Konva.Layer;
  spotlightLayer: Konva.Layer;
  highlightLayer: Konva.Layer;
  cursorLayer: Konva.Layer;
  onSendReport: (report: any) => void;
}

export class CommandExecutor {
  private layout: BoardLayout;
  private txManager: TransactionManager;
  private drawLayer: Konva.Layer;
  private animLayer: Konva.Layer;
  private spotlightLayer: Konva.Layer;
  private highlightLayer: Konva.Layer;
  private cursorLayer: Konva.Layer;
  private onSendReport: (report: any) => void;

  /** The blocks of the current page commit (for focus/anchors/clearDiagram). */
  private currentBlocks: Block[] = [];

  public activeGeneration: number = 0;
  private ledger: BoardLedger;
  public activeDiagram: VerifiedDiagram | null = null;
  public activeAnchors: DiagramAnchor[] = [];
  public deferredAnnotations: Map<string, DiagramCommand[]> = new Map();
  /** Page-commit block groups, keyed by block id (name `block_{id}`). */
  private blockGroups = new Map<string, Konva.Group>();
  /** Staged figure groups reveal when the speech reaches them. */
  public revealClock = new RevealClock();
  /** A page_commit is idempotent by (pageId, commitId); a re-sent commit is skipped. */
  private lastPageId = '';
  private lastCommitId = '';
  /** The rect each block group was last placed in (sticky tween origin) and the swap
   *  functions of in-flight sticky tweens (finished before any newer commit for that block). */
  private blockRects = new Map<string, Rect>();
  private stickySwaps = new Map<string, () => void>();

  // Highlight/spotlight state
  private currentFocusStepKey: string = '';
  private activeFocusNodes: (Konva.Group | Konva.Shape)[] = [];
  private activeFocusGroups: Konva.Group[] = [];
  private activeFocusTweens: gsap.core.Tween[] = [];
  private msPerWordEMA: number = 300; // ms per word exponential moving average
  private placingOp: BoardOp | null = null; // the WRITE currently being placed (overflow reports)

  constructor(opts: CommandExecutorOptions) {
    this.layout = opts.layout;
    this.txManager = opts.txManager;
    this.drawLayer = opts.drawLayer;
    this.animLayer = opts.animLayer;
    this.spotlightLayer = opts.spotlightLayer;
    this.highlightLayer = opts.highlightLayer;
    this.cursorLayer = opts.cursorLayer;
    this.onSendReport = opts.onSendReport;
    this.ledger = new BoardLedger(opts.layout.currentPageId);
  }

  /** Compatibility view: op ids drawn on the current sub-page. */
  public get drawnOpIds(): Set<string> {
    return new Set(this.ledger.opsOf(this.ledger.currentSubId).map((op) => op.opId));
  }

  public setGeneration(generation: number) {
    if (generation < this.activeGeneration) return; // Drop old generation
    this.activeGeneration = generation;
  }

  /** Matcher's smoothed tempo: paces WRITE animations with the tutor's actual speech. */
  public setMsPerWord(ms: number): void {
    if (Number.isFinite(ms) && ms > EMA_MIN_MS && ms < EMA_MAX_MS) {
      this.msPerWordEMA = ms;
    }
  }

  /** Bumped on every board reset/commit; delayed reveal timers check it so a reveal scheduled
   *  for a board that was cleared (new page, supersede) never lands on the new board. */
  private boardEpoch = 0;
  public onLayoutChanged: () => void = () => {};

  public clearBoard(instant: boolean = true): void {
    this.boardEpoch += 1;
    this.clearFocus();
    this.txManager.clearAll();
    for (const group of this.blockGroups.values()) {
      gsap.killTweensOf(group);                  // incl. an in-flight sticky tween
      gsap.killTweensOf(group.getChildren());
    }
    this.drawLayer.destroyChildren();
    this.animLayer.destroyChildren();
    this.highlightLayer.destroyChildren();
    this.spotlightLayer.destroyChildren();
    this.drawLayer.batchDraw();
    this.animLayer.batchDraw();
    this.highlightLayer.batchDraw();
    this.spotlightLayer.batchDraw();
    this.ledger.reset(this.layout.currentPageId);
    this.blockGroups.clear();
    this.blockRects.clear();
    this.stickySwaps.clear();
    this.currentBlocks = [];
    this.revealClock.reset();
    this.lastPageId = '';
    this.lastCommitId = '';
    this.activeDiagram = null;
    this.activeAnchors = [];
    this.deferredAnnotations.clear();
    this.onLayoutChanged();
  }

  /**
   * Commit a verified diagram (legacy path): mapped to an internal one-block
   * PageCommit whose block occupies the diagram zone {420,40,720,600}. The diagram
   * always occupies sections 2+3 (DIAGRAM_STANDARD_1COL): the server projects into
   * x 420..1140: student rows must stay in section 1 and never overlap the figure.
   */
  public commitDiagram(diagram: VerifiedDiagram, turnId: string, generation: number, instant: boolean = false): void {
    if (generation < this.activeGeneration) return;
    const rawCommands = Array.isArray(diagram.commands) ? diagram.commands : [];
    const commands: DiagramCommand[] = rawCommands.map((c: any) => ({
      ...c,
      anchorId: c.anchorId || c.anchor_id,
      semanticRef: c.semanticRef || (c.semantic_ref ? { entityId: c.semantic_ref.entity_id || c.semantic_ref.entityId } : undefined),
    }));

    const rawReveals = Array.isArray(diagram.reveals) ? diagram.reveals : [];
    const normalizedReveals = rawReveals.map((r: any) => ({
      ...r,
      targetId: r.targetId || r.target_id || r.id,
      commandIndices: Array.isArray(r.commandIndices) ? r.commandIndices : (Array.isArray(r.command_indices) ? r.command_indices : []),
    }));

    const normalizedDiagram: VerifiedDiagram = {
      ...diagram,
      commands,
      reveals: normalizedReveals,
      labelGlossary: diagram.labelGlossary || (diagram as any).label_glossary,
    };

    const block: Block = {
      id: 'diagram',
      role: 'figure',
      rect: { x: DIAGRAM_ZONE.x, y: DIAGRAM_ZONE.y, width: DIAGRAM_ZONE.width, height: DIAGRAM_ZONE.height },
      commands,
      anchors: Array.isArray(diagram.anchors) ? diagram.anchors : [],
      reveals: normalizedReveals,
      deferredAnnotations: normalizedDiagram.deferredAnnotations,
      labelGlossary: normalizedDiagram.labelGlossary,
      aliasMap: (diagram as any).aliasMap || (diagram as any).alias_map,
      namespace: (diagram as any).namespace,
    };
    const pc: PageCommitEvent = {
      type: 'page_commit',
      generation,
      turnId,
      pageId: this.layout.currentPageId,
      commitId: 'legacy',
      workRect: { x: 40, y: 72, width: 340, height: 608 },
      blocks: [block],
    };
    this.commitPage(pc, instant);
  }

  /**
   * Commit a page's blocks. workRect chooses the work-column mode (a null
   * workRect is never sent by this plan; treat it as the diagram work rect and warn).
   * Blocks not in the commit fade out (BLOCK_FADE_MS) and are destroyed; a block with an
   * existing group is replaced in place (the sticky tween); new figure blocks
   * render into `block_{id}` with the base and reveal groups drawn hidden and staged in the
   * RevealClock. A block whose render throws is reported as
   * client_error{where:'render'} and the rest still render.
   */
  public commitPage(pc: PageCommitEvent, instant: boolean = false): void {
    const generation = typeof pc.generation === 'number' ? pc.generation : 0;
    if (generation < this.activeGeneration) return;

    // Idempotent by (pageId, commitId). A re-sent / replayed commit must never
    // re-render: it would destroy the RevealClock's staged groups and dump hidden ink.
    if (pc.pageId === this.lastPageId && pc.commitId === this.lastCommitId) return;
    this.lastPageId = pc.pageId;
    this.lastCommitId = pc.commitId;

    if (!pc.workRect) {
      console.warn('[layout] null workRect');
    }
    this.layout.setWorkRect(pc.workRect || null);

    // Blocks that left the page fade out, then are destroyed.
    const keep = new Set((pc.blocks || []).map((b) => b.id));
    for (const [id, group] of [...this.blockGroups.entries()]) {
      if (keep.has(id)) continue;
      this.blockGroups.delete(id);
      this.blockRects.delete(id);
      this.stickySwaps.delete(id);
      this.revealClock.unstage(id);      // a later step start must not animate the dying block
      gsap.killTweensOf(group);
      gsap.killTweensOf(group.getChildren());
      const layer = group.getLayer();
      gsap.to(group, {
        opacity: 0,
        duration: BLOCK_FADE_MS / 1000,
        onUpdate: () => layer?.batchDraw(),
        onComplete: () => {
          group.destroy();
          layer?.batchDraw();
        },
      });
    }

    for (const block of pc.blocks || []) {
      try {
        this.renderBlock(block, instant, pc.reference === true);
      } catch (err) {
        console.error('[page_commit] block render failed', block.id, err);
        this.onSendReport({
          type: 'client_error',
          where: 'render',
          message: String(err).slice(0, 500),
          turnId: pc.turnId,
          blockId: block.id,
        });
      }
    }

    // The marking targets come from every block's anchors; the legacy FOCUS/ANNOTATE
    // paths operate on an aggregate single-figure view built from every figure block, so
    // [FOCUS] keeps working on multi-figure pages.
    this.currentBlocks = pc.blocks || [];
    this.refreshAnchorsAndDiagram();
    this.onLayoutChanged();
  }

  /** Rebuild activeAnchors / activeDiagram / deferredAnnotations from the current blocks. */
  private refreshAnchorsAndDiagram(): void {
    this.activeAnchors = this.currentBlocks.flatMap((b) => b.anchors || []);
    const figureBlocks = this.currentBlocks.filter((b) => b.role === 'figure');
    if (figureBlocks.length === 0) {
      this.activeDiagram = null;
    } else {
      // Reveal groups keep block-local command indices; shift them by the cumulative command
      // offset so they index into the flattened commands of the aggregate view.
      let cmdOffset = 0;
      const allCommands: DiagramCommand[] = [];
      const allReveals: any[] = [];
      for (const b of figureBlocks) {
        const cmds = b.commands || [];
        allCommands.push(...cmds);
        for (const r of b.reveals || []) {
          allReveals.push({
            ...r,
            commandIndices: (Array.isArray(r.commandIndices) ? r.commandIndices : []).map((i) => i + cmdOffset),
          });
        }
        cmdOffset += cmds.length;
      }
      this.activeDiagram = {
        name: 'page_commit',
        commands: allCommands,
        anchors: figureBlocks.flatMap((b) => b.anchors || []),
        reveals: allReveals,
        deferredAnnotations: figureBlocks.flatMap((b) => b.deferredAnnotations || []),
        labelGlossary: Object.assign({}, ...figureBlocks.map((b) => b.labelGlossary || {})),
        aliasMap: Object.assign({}, ...figureBlocks.map((b) => b.aliasMap || {})),
      };
    }
    this.deferredAnnotations.clear();
    for (const b of figureBlocks) {
      for (const da of b.deferredAnnotations || []) {
        this.deferredAnnotations.set(da.entityId, da.commands || []);
      }
    }
  }

  /** Renders one block into its `block_{id}` group. Tables and text blocks
   *  have no reveal groups and draw at commit time. */
  private renderBlock(block: Block, instant: boolean, reference: boolean): void {
    if (block.role === 'table' || block.role === 'text') {
      this.renderStaticBlock(block);
      return;
    }
    if (block.role !== 'figure') {
      console.warn('[page_commit] unsupported block role:', block.role);
      return;
    }
    const commands = block.commands || [];
    const existing = this.blockGroups.get(block.id);

    // A carried sticky block tweens from its old rect to the new one, then its
    // children are replaced by the new commands with `revealedIds` shown.
    if (existing && block.sticky && !instant) {
      this.finishStickySwap(block.id);
      this.stickySwap(existing, block);
      return;
    }

    if (instant) {
      // Instant draw (snapshot restores, notes view), or an instant recommit replacing the
      // block's contents in place.
      const group = existing || new Konva.Group({ name: `block_${block.id}` });
      this.finishStickySwap(block.id);
      gsap.killTweensOf(group);
      gsap.killTweensOf(group.getChildren());
      group.position({ x: 0, y: 0 });
      group.scale({ x: 1, y: 1 });
      group.destroyChildren();
      group.opacity(1);
      for (const cmd of commands) this.renderDiagramCommand(cmd, group);
      if (!existing) {
        this.drawLayer.add(group);
        this.blockGroups.set(block.id, group);
      }
      this.blockRects.set(block.id, block.rect);
      this.drawLayer.batchDraw();
      return;
    }

    // Staged: base + reveal groups drawn hidden; the RevealClock reveals them.
    // A recommit (new commitId, same block id — e.g. a resume re-activation) re-stages in
    // place instead of wiping the staged groups and dumping all ink at full opacity.
    const reveals = (block.reveals || []).filter((r) => Array.isArray(r.commandIndices));
    const covered = new Set(reveals.flatMap((r) => r.commandIndices));
    let baseIdx = commands.map((_, i) => i).filter((i) => !covered.has(i));
    let activeReveals = [...reveals];

    // If the compiler placed ALL commands into reveal groups (leaving no uncovered commands
    // for the base setup), treat the first reveal group (the initial figure / setup) as the
    // base group so it reveals on the first spoken word instead of leaving the canvas empty.
    if (baseIdx.length === 0 && activeReveals.length > 0) {
      baseIdx = activeReveals[0].commandIndices;
      activeReveals = activeReveals.slice(1);
    }

    const groups = [{ targetId: 'base', commandIndices: baseIdx }, ...activeReveals]
      .filter((g) => g.commandIndices.length > 0);

    const container = existing ?? new Konva.Group({ name: `block_${block.id}` });
    if (existing) {
      this.finishStickySwap(block.id);
      gsap.killTweensOf(container);
      gsap.killTweensOf(container.getChildren());
      container.position({ x: 0, y: 0 });
      container.scale({ x: 1, y: 1 });
      container.destroyChildren();
      container.opacity(1);
    } else {
      this.animLayer.add(container);
      this.blockGroups.set(block.id, container);
    }
    this.blockRects.set(block.id, block.rect);

    const getCmdEntityId = (c: any): string => {
      return (
        c.anchorId ||
        c.anchor_id ||
        c.semanticRef?.entityId ||
        c.semantic_ref?.entity_id ||
        ''
      );
    };

    const staged: StagedGroup[] = [];
    for (const rev of groups) {
      const group = new Konva.Group({ name: `reveal_${rev.targetId}`, opacity: 0 });
      const entityIds = new Set<string>();
      if (rev.targetId) entityIds.add(rev.targetId);
      for (const cmdIdx of rev.commandIndices) {
        const cmd = commands[cmdIdx];
        if (cmd) {
          this.renderDiagramCommand(cmd, group);
          const eid = getCmdEntityId(cmd);
          if (eid) entityIds.add(eid);
        }
      }
      container.add(group);
      staged.push({ targetId: rev.targetId, nodes: group, entityIds });
    }
    this.animLayer.batchDraw();
    this.revealClock.stage(block.id, staged, reference);
  }

  /** Finish an in-flight sticky tween immediately (a newer commit supersedes it). */
  private finishStickySwap(blockId: string): void {
    const swap = this.stickySwaps.get(blockId);
    if (swap) swap();                       // the swap removes itself from the map
  }

  /** Tween an existing sticky group from its old rect to the new one, then
   *  replace its children with the new commands (`revealedIds` already shown). */
  private stickySwap(group: Konva.Group, block: Block): void {
    const oldRect = this.blockRects.get(block.id) ?? block.rect;
    const rect = block.rect;
    const same = oldRect.x === rect.x && oldRect.y === rect.y
      && oldRect.width === rect.width && oldRect.height === rect.height;
    if (same || oldRect.width <= 0 || oldRect.height <= 0 || rect.width <= 0 || rect.height <= 0) {
      this.applyStickyContents(group, block);
      return;
    }
    // Move the children into group-local coordinates so the group transform can hold the tween.
    const dx = -oldRect.x;
    const dy = -oldRect.y;
    for (const child of group.getChildren()) {
      child.x(child.x() + dx);
      child.y(child.y() + dy);
    }
    group.position({ x: oldRect.x, y: oldRect.y });
    group.scale({ x: 1, y: 1 });
    const layer = group.getLayer();
    const swap = () => {
      this.stickySwaps.delete(block.id);
      this.applyStickyContents(group, block);
    };
    this.stickySwaps.set(block.id, swap);
    gsap.killTweensOf(group);
    gsap.to(group, {
      x: rect.x,
      y: rect.y,
      scaleX: rect.width / oldRect.width,
      scaleY: rect.height / oldRect.height,
      duration: STICKY_TWEEN_MS / 1000,
      onUpdate: () => layer?.batchDraw(),
      onComplete: swap,
    });
  }

  /** Draw a carried sticky's new commands: base + already-revealed groups visible (it was
   *  shown on the previous page), the rest staged hidden for this page's speech. */
  private applyStickyContents(group: Konva.Group, block: Block): void {
    gsap.killTweensOf(group);
    gsap.killTweensOf(group.getChildren());
    group.position({ x: 0, y: 0 });
    group.scale({ x: 1, y: 1 });
    group.destroyChildren();
    group.opacity(1);
    const commands = block.commands || [];
    const reveals = (block.reveals || []).filter((r) => Array.isArray(r.commandIndices));
    const revealed = new Set(block.revealedIds || []);
    const covered = new Set(reveals.flatMap((r) => r.commandIndices));
    let baseIdx = commands.map((_, i) => i).filter((i) => !covered.has(i));
    let activeReveals = [...reveals];

    if (baseIdx.length === 0 && activeReveals.length > 0) {
      baseIdx = activeReveals[0].commandIndices;
      activeReveals = activeReveals.slice(1);
    }

    baseIdx.forEach((i) => {
      const cmd = commands[i];
      if (cmd) this.renderDiagramCommand(cmd, group);
    });

    const getCmdEntityId = (c: any): string => {
      return (
        c.anchorId ||
        c.anchor_id ||
        c.semanticRef?.entityId ||
        c.semantic_ref?.entity_id ||
        ''
      );
    };

    const staged: StagedGroup[] = [];
    for (const rev of activeReveals) {
      if (revealed.has(rev.targetId)) {
        for (const idx of rev.commandIndices) {
          const cmd = commands[idx];
          if (cmd) this.renderDiagramCommand(cmd, group);
        }
      } else {
        const hidden = new Konva.Group({ name: `reveal_${rev.targetId}`, opacity: 0 });
        const entityIds = new Set<string>();
        if (rev.targetId) entityIds.add(rev.targetId);
        for (const idx of rev.commandIndices) {
          const cmd = commands[idx];
          if (cmd) {
            this.renderDiagramCommand(cmd, hidden);
            const eid = getCmdEntityId(cmd);
            if (eid) entityIds.add(eid);
          }
        }
        group.add(hidden);
        staged.push({ targetId: rev.targetId, nodes: hidden, entityIds });
      }
    }
    if (staged.length > 0) this.revealClock.stage(block.id, staged, false);
    else this.revealClock.unstage(block.id);
    this.blockRects.set(block.id, block.rect);
    group.getLayer()?.batchDraw();
  }

  /** Table/text blocks: fully visible at commit; recommits replace in place. */
  private renderStaticBlock(block: Block): void {
    const existing = this.blockGroups.get(block.id);
    const group = existing || new Konva.Group({ name: `block_${block.id}` });
    if (existing) {
      gsap.killTweensOf(group);
      gsap.killTweensOf(group.getChildren());
      group.destroyChildren();
      group.opacity(1);
    }
    if (block.role === 'table') this.renderTableBlock(block, group);
    else this.renderTextBlock(block, group);
    if (!existing) {
      this.drawLayer.add(group);
      this.blockGroups.set(block.id, group);
    }
    this.blockRects.set(block.id, block.rect);
    this.drawLayer.batchDraw();
  }

  /** Table: `cells = textLines[i].split(' | ')`, first line = header, cell text centred
   *  with 8 px top padding, 2 px header underline, 1 px column rules, 1.5 px outer border. */
  private renderTableBlock(block: Block, group: Konva.Group): void {
    const lines = block.textLines || [];
    if (lines.length === 0) return;
    const rect = block.rect;
    const rows = lines.map((l) => l.split(' | '));
    const columns = Math.max(...rows.map((r) => r.length));
    const colW = rect.width / columns;

    let rowH = TABLE_ROW_H;
    let font = TABLE_FONT_PX;
    if (rows.length * TABLE_ROW_H > rect.height) {
      rowH = TABLE_ROW_H_SMALL;
      font = TABLE_FONT_PX_SMALL;
    }
    // Rows that still do not fit are dropped; the last visible row shows "…".
    const maxRows = Math.max(1, Math.floor(rect.height / rowH));
    const shown = rows.slice(0, maxRows);
    const truncated = rows.length > maxRows;

    for (let r = 0; r < shown.length; r++) {
      const isEllipsisRow = truncated && r === shown.length - 1;
      const cells = isEllipsisRow ? ['…'] : shown[r];
      for (let c = 0; c < columns; c++) {
        const text = (cells[c] ?? '').replace(/[∥‖]/g, '||');
        if (!text) continue;
        group.add(new Konva.Text({
          x: rect.x + c * colW,
          y: rect.y + r * rowH + 8,
          width: colW,
          text,
          fontSize: font,
          fontFamily: FONT,
          fill: CHALK,
          align: 'center',
        }));
      }
      // 1 px vertical lines at column boundaries.
      for (let c = 1; c < columns; c++) {
        group.add(new Konva.Line({
          points: [rect.x + c * colW, rect.y + r * rowH, rect.x + c * colW, rect.y + (r + 1) * rowH],
          stroke: CHALK,
          strokeWidth: 1,
        }));
      }
      if (r === 0) {
        // Header row = first line, underlined by a 2 px line.
        group.add(new Konva.Line({
          points: [rect.x, rect.y + rowH, rect.x + rect.width, rect.y + rowH],
          stroke: CHALK,
          strokeWidth: 2,
        }));
      }
    }
    group.add(new Konva.Rect({
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: shown.length * rowH,
      stroke: CHALK,
      strokeWidth: 1.5,
    }));
  }

  /** Text block: 24 px Caveat lines, 38 px line height, 12 px padding, 1.5 px border;
   *  lines are clamped to the block's inner width and word-wrapped. The clamp never goes to
   *  zero: Konva treats width 0 as unconstrained, which would let the line overflow again. */
  private renderTextBlock(block: Block, group: Konva.Group): void {
    const rect = block.rect;
    const innerWidth = Math.max(1, rect.width - 24);
    (block.textLines || []).forEach((text, i) => {
      group.add(new Konva.Text({
        x: rect.x + 12,
        y: rect.y + 12 + i * 38,
        width: innerWidth,
        text: (text || '').replace(/[∥‖]/g, '||'),
        fontSize: 24,
        fontFamily: FONT,
        fill: CHALK,
        wrap: 'word',
      }));
    });
    group.add(new Konva.Rect({
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      stroke: CHALK,
      strokeWidth: 1.5,
    }));
  }

  /** Destroy one block group (all when blockId is omitted). The removed block's
   *  anchors leave the marking targets and its staged groups are forgotten by the clock. */
  public clearDiagram(blockId?: string): void {
    const destroy = (group: Konva.Group) => {
      gsap.killTweensOf(group);
      gsap.killTweensOf(group.getChildren());
      group.destroy();
    };
    if (blockId) {
      const group = this.blockGroups.get(blockId);
      if (group) {
        destroy(group);
        this.blockGroups.delete(blockId);
      }
      this.blockRects.delete(blockId);
      this.stickySwaps.delete(blockId);
      this.revealClock.unstage(blockId);
      this.currentBlocks = this.currentBlocks.filter((b) => b.id !== blockId);
    } else {
      for (const group of this.blockGroups.values()) destroy(group);
      this.blockGroups.clear();
      this.blockRects.clear();
      this.stickySwaps.clear();
      this.currentBlocks = [];
      this.revealClock.reset();
      // The board's commit identity is gone with the blocks: an identical (pageId, commitId)
      // re-commit afterwards is a legitimate re-render, not a duplicate (guard reset).
      this.lastPageId = '';
      this.lastCommitId = '';
    }
    this.refreshAnchorsAndDiagram();
    this.drawLayer.batchDraw();
    this.animLayer.batchDraw();
  }

  /** Renders every DiagramCommand type the backend emits (see types/events.ts for layouts).
   *  An optional `style` overrides stroke/fill/width/shadow for highlights.
   *  Unknown types are skipped; returns false if nothing drawn. */
  public renderDiagramCommand(cmd: DiagramCommand, container: Konva.Container, style?: DiagramRenderStyle): boolean {
    const p = (cmd.params || []).map(Number);
    const ok = p.every((v) => Number.isFinite(v));
    if (!ok) {
      console.warn('[diagram] non-finite params, skipped', cmd);
      return false;
    }
    const stroke = style?.stroke ?? CHALK;
    const mark = style?.stroke ?? MARK;
    const axis = style?.stroke ?? AXIS;
    const label = style?.stroke ?? LABEL_FILL;
    const width = (base: number) => base * (style?.strokeWidthMul ?? 1);
    const shadow = style
      ? { shadowColor: style.stroke, shadowBlur: style.shadowBlur, shadowOpacity: 1 }
      : {};
    const line = (points: number[], extra: Partial<Konva.LineConfig> = {}) =>
      new Konva.Line({ points, stroke, strokeWidth: width(2), lineCap: 'round', lineJoin: 'round', ...shadow, ...extra });
    const rad = (d: number) => (d * Math.PI) / 180;
    const arc = (cx: number, cy: number, r: number, a1: number, a2: number, color: string, w: number) => {
      const shape = arcShape(cx, cy, r, a1, a2, color, w);
      if (style) {
        shape.shadowColor(style.stroke);
        shape.shadowBlur(style.shadowBlur);
        shape.shadowOpacity(1);
      }
      return shape;
    };

    switch (cmd.type) {
      case 'DRAW_POINT': {
        const [x, y, r] = p;
        container.add(new Konva.Circle({
          x, y, radius: r || 3, fill: style?.fill ?? POINT,
          stroke: style ? stroke : undefined, strokeWidth: style ? width(1.5) : 0, ...shadow,
        }));
        return true;
      }
      case 'DRAW_LINE': {
        if (p.length < 4) return false;
        container.add(line(p.slice(0, 4)));
        return true;
      }
      case 'DRAW_RAY': {
        if (p.length < 4) return false;
        container.add(new Konva.Arrow({ points: p.slice(0, 4), stroke, fill: stroke, strokeWidth: width(2), pointerLength: 8, pointerWidth: 7, ...shadow }));
        return true;
      }
      case 'DRAW_CIRCLE': {
        const [x, y, r] = p;
        container.add(new Konva.Circle({ x, y, radius: r, stroke, strokeWidth: width(2), ...shadow }));
        return true;
      }
      case 'DRAW_POLYLINE':
      case 'DRAW_CURVE': {
        if (p.length < 4) return false;
        container.add(line(p));
        return true;
      }
      case 'DRAW_ARC': {
        const [cx, cy, r, start, end] = p;
        const sweep = (((end - start) % 360) + 360) % 360 || 360;
        container.add(arc(cx, cy, r, start, start + sweep, stroke, width(2)));
        return true;
      }
      case 'DRAW_ANGLE_MARK': {
        const [vx, vy, r, a1, a2] = p;
        let d = (((a2 - a1) % 360) + 360) % 360;
        let from = a1;
        if (d > 180) {            // always the interior (minor) angle between the two arms
          from = a2;
          d = 360 - d;
        }
        container.add(arc(vx, vy, r || 22, from, from + d, mark, width(1.6)));
        return true;
      }
      case 'DRAW_RIGHT_ANGLE_MARK': {
        const [vx, vy, s, a1, a2] = p;
        const size = s || 14;
        const u = [Math.cos(rad(a1)), Math.sin(rad(a1))];
        const w = [Math.cos(rad(a2)), Math.sin(rad(a2))];
        container.add(line([
          vx + size * u[0], vy + size * u[1],
          vx + size * (u[0] + w[0]), vy + size * (u[1] + w[1]),
          vx + size * w[0], vy + size * w[1],
        ], { stroke: mark, strokeWidth: width(1.6) }));
        return true;
      }
      case 'DRAW_TICK': {
        const [mx, my, dir, countRaw] = p;
        const count = Math.max(1, Math.min(3, Math.round(countRaw || 1)));
        const along = [Math.cos(rad(dir)), Math.sin(rad(dir))];
        const perp = [-along[1], along[0]];
        for (let i = 0; i < count; i++) {
          const off = (i - (count - 1) / 2) * 5;
          const cx = mx + along[0] * off;
          const cy = my + along[1] * off;
          container.add(line([cx - perp[0] * 6, cy - perp[1] * 6, cx + perp[0] * 6, cy + perp[1] * 6], { stroke: mark, strokeWidth: width(1.6) }));
        }
        return true;
      }
      case 'DRAW_DIMENSION': {
        const [x1, y1, x2, y2, offRaw] = p;
        const len = Math.hypot(x2 - x1, y2 - y1) || 1;
        const off = offRaw || 20;
        const n = [(-(y2 - y1) / len) * off, ((x2 - x1) / len) * off];
        const a = [x1 + n[0], y1 + n[1], x2 + n[0], y2 + n[1]];
        container.add(new Konva.Arrow({ points: a, stroke: mark, fill: mark, strokeWidth: width(1.4), pointerLength: 6, pointerWidth: 6, pointerAtBeginning: true, ...shadow }));
        if (cmd.text) {
          container.add(new Konva.Text({ x: (a[0] + a[2]) / 2 + 4, y: (a[1] + a[3]) / 2 - 20, text: cmd.text, fontSize: 16, fontFamily: FONT, fill: label, ...shadow }));
        }
        return true;
      }
      case 'DRAW_AXES': {
        const [x0, y0, x1, y1, ox, oy, unit] = p;
        const arrow = { stroke: axis, fill: axis, strokeWidth: width(1.6), pointerLength: 8, pointerWidth: 7, ...shadow };
        container.add(new Konva.Arrow({ points: [x0, oy, x1, oy], ...arrow }));
        container.add(new Konva.Arrow({ points: [ox, y0, ox, y1], ...arrow }));
        if (unit >= 6) {
          const every = Math.max(1, Math.ceil(24 / unit));          // label spacing >= 24 px
          const maxTicks = 80;
          let n = 0;
          for (let k = Math.ceil((x0 - ox) / unit); ox + k * unit <= x1 && n < maxTicks; k++, n++) {
            if (k === 0) continue;
            const x = ox + k * unit;
            container.add(line([x, oy - 4, x, oy + 4], { stroke: axis, strokeWidth: width(1) }));
            if (k % every === 0) container.add(new Konva.Text({ x: x - 6, y: oy + 6, text: String(k), fontSize: 12, fill: axis, ...shadow }));
          }
          n = 0;
          for (let k = Math.ceil((oy - y0) / unit); oy - k * unit >= y1 && n < maxTicks; k++, n++) {
            if (k === 0) continue;
            const y = oy - k * unit;
            container.add(line([ox - 4, y, ox + 4, y], { stroke: axis, strokeWidth: width(1) }));
            if (k % every === 0) container.add(new Konva.Text({ x: ox - 22, y: y - 6, text: String(k), fontSize: 12, fill: axis, ...shadow }));
          }
        }
        return true;
      }
      case 'DRAW_NUMBER_LINE': {
        const [x1, y, x2, tickPx, minVal, stepRaw] = p;
        const step = stepRaw || 1;
        container.add(new Konva.Arrow({ points: [x1 - 12, y, x2 + 12, y], stroke, fill: stroke, strokeWidth: width(2), pointerLength: 8, pointerWidth: 7, pointerAtBeginning: true, ...shadow }));
        if (tickPx >= 4) {
          const count = Math.min(200, Math.floor((x2 - x1) / tickPx + 1e-6));
          const every = Math.max(1, Math.ceil(28 / tickPx));
          for (let i = 0; i <= count; i++) {
            const x = x1 + i * tickPx;
            container.add(line([x, y - 6, x, y + 6], { strokeWidth: width(1.4) }));
            if (i % every === 0) {
              const v = +(minVal + i * step).toFixed(6);
              container.add(new Konva.Text({ x: x - 6, y: y + 10, text: String(v), fontSize: 14, fontFamily: FONT, fill: axis, ...shadow }));
            }
          }
        }
        return true;
      }
      case 'LABEL': {
        const [x, y, fontSize] = p;
        const text = (cmd.text || '').replace(/[∥‖]/g, '||');
        container.add(new Konva.Text({ x, y, text, fontSize: fontSize || 18, fontFamily: FONT, fill: label, ...shadow }));
        return true;
      }
      default:
        console.warn('[diagram] unknown command type skipped:', cmd.type);
        return false;
    }
  }

  /**
   * Execute a single BoardOp.
   * Idempotency guard: if opId in drawnOpIds, skip.
   */
  public executeOp(rawOp: BoardOp, generation: number, turnId: string, instant: boolean = false): void {
    if (generation < this.activeGeneration) return;

    // Normalize from snake_case backend payloads to camelCase frontend interfaces
    const op: BoardOp = {
      ...rawOp,
      opId: rawOp.opId || (rawOp as any).op_id,
      atWord: rawOp.atWord ?? (rawOp as any).at_word,
      entityId: rawOp.entityId || (rawOp as any).entity_id,
      rowId: rawOp.rowId || (rawOp as any).row_id,
      focusMode: rawOp.focusMode || (rawOp as any).focus_mode,
      emphasizeRowId: rawOp.emphasizeRowId || (rawOp as any).emphasize_row_id,
      pageTitle: rawOp.pageTitle || (rawOp as any).page_title,
      durationMs: rawOp.durationMs ?? (rawOp as any).duration_ms,
    };

    if (this.ledger.hasDrawn(op.opId)) return; // Idempotent skip (any sub-page)
    this.ledger.record(op);

    switch (op.kind) {
      case 'WRITE':
        this.handleWrite(op, instant);
        break;
      case 'FOCUS':
        if (!instant) this.handleFocus(op);     // transient: never replayed on page_restore
        break;
      case 'EMPHASIZE':
        this.handleEmphasize(op, instant);
        break;
      case 'ANNOTATE':
        this.handleAnnotate(op, instant);
        break;
      case 'PAUSE':
        // No visual action: audio contains break
        break;
      case 'PAGE_BREAK':
        this.handlePageBreak(op, generation);
        break;
    }
  }

  private handleWrite(op: BoardOp, instant: boolean): void {
    const text = (op.text || '').replace(/[∥‖]/g, '||');
    const rowId = op.rowId || 'w1';
    this.placingOp = op;               // the overflow report names the WRITE that did not fit
    const placements = this.layout.placeRow(text, rowId);
    this.placingOp = null;

    const txId = this.txManager.beginDrawTransaction();
    const group = new Konva.Group({ name: `row_${rowId}` });
    this.animLayer.add(group);
    this.txManager.trackNode(txId, group);

    for (const pl of placements) {
      const textNode = new Konva.Text({
        x: pl.x,
        y: pl.y,
        text: pl.text,
        fontSize: 24,
        fontFamily: 'Caveat, cursive',
        fill: '#fef3c7',
        opacity: instant ? 1 : 0,
      });
      group.add(textNode);
    }

    if (instant) {
      this.txManager.commitDrawTransaction(txId);
      this.onSendReport(this.layout.getBoardReport());
      return;
    }

    // Animate write over writeMs
    const words = text.split(/\s+/).length;
    const writeMs = Math.min(6000, Math.max(400, words * this.msPerWordEMA * 0.9));

    gsap.to(group.getChildren(), {
      opacity: 1,
      duration: writeMs / 1000,
      stagger: 0.1,
      ease: 'power1.out',
      onUpdate: () => this.animLayer.batchDraw(),
      onComplete: () => {
        this.txManager.commitDrawTransaction(txId);
        this.onSendReport(this.layout.getBoardReport());
      },
    });
  }

  /** Renders ANY command type into a highlight group. */
  private createHighlightShapeForCmd(cmd: DiagramCommand, strokeColor: string): Konva.Group | null {
    const group = new Konva.Group({ name: 'focus_shape' });
    const rendered = this.renderDiagramCommand(cmd, group, {
      stroke: strokeColor,
      strokeWidthMul: 2.6,
      shadowBlur: 14,
      fill: 'rgba(0, 245, 255, 0.18)',
    });
    if (!rendered || group.getChildren().length === 0) return null;
    return group;
  }

  /** Adds a focus group and enforces FOCUS_CAP: the oldest fades out on the third. */
  private pushFocusGroup(group: Konva.Group): void {
    this.spotlightLayer.add(group);
    this.activeFocusNodes.push(group);
    this.activeFocusGroups.push(group);
    while (this.activeFocusGroups.length > FOCUS_CAP) {
      const oldest = this.activeFocusGroups.shift()!;
      this.fadeOneFocus(oldest);
    }
    this.spotlightLayer.batchDraw();
  }

  private fadeOneFocus(node: Konva.Node): void {
    const i = this.activeFocusNodes.indexOf(node as any);
    if (i >= 0) this.activeFocusNodes.splice(i, 1);
    gsap.to(node, {
      opacity: 0,
      duration: 0.5,
      ease: 'power2.out',
      onUpdate: () => this.spotlightLayer.batchDraw(),
      onComplete: () => {
        node.destroy();
        this.spotlightLayer.batchDraw();
      },
    });
  }

  /** Fade in, then breathe (or pulse) — shared by the command/reveal/anchor focus paths. */
  private animateFocusGroup(group: Konva.Group, mode: string): void {
    gsap.to(group, {
      opacity: 1,
      duration: mode === 'pulse' ? 0.25 : 0.35,
      onUpdate: () => this.spotlightLayer.batchDraw(),
    });
    const breathe = gsap.to(group, {
      opacity: mode === 'pulse' ? 0.3 : 0.7,
      duration: mode === 'pulse' ? 0.5 : 0.9,
      repeat: -1,
      yoyo: true,
      ease: mode === 'pulse' ? 'power1.inOut' : 'sine.inOut',
      onUpdate: () => this.spotlightLayer.batchDraw(),
    });
    this.activeFocusTweens.push(breathe);
  }

  private fadeOldFocus(immediate: boolean = false): void {
    const nodes = [...this.activeFocusNodes];
    const tweens = [...this.activeFocusTweens];
    this.activeFocusNodes = [];
    this.activeFocusGroups = [];
    this.activeFocusTweens = [];
    for (const tw of tweens) tw.kill();

    if (immediate) {
      for (const n of nodes) n.destroy();
      this.spotlightLayer.batchDraw();
      return;
    }

    for (const n of nodes) {
      gsap.to(n, {
        opacity: 0,
        duration: 0.5,
        ease: 'power2.out',
        onUpdate: () => this.spotlightLayer.batchDraw(),
        onComplete: () => {
          n.destroy();
          this.spotlightLayer.batchDraw();
        },
      });
    }
  }

  private handleFocus(op: BoardOp): void {
    const entityId = op.entityId;
    if (!entityId) return;

    // Ensure any staged group containing this entity is revealed
    this.revealClock.revealByEntityId(entityId);

    // Step-bound persistence: if focus belongs to a new step, smoothly fade out old highlights
    const stepKey = op.opId ? op.opId.split(':').slice(0, 2).join(':') : '';
    if (stepKey && this.currentFocusStepKey && stepKey !== this.currentFocusStepKey) {
      this.fadeOldFocus(false);
    }
    if (stepKey) {
      this.currentFocusStepKey = stepKey;
    }

    const mode = op.focusMode || 'outline';
    // High-contrast electric cyan for maximum visibility on chalkboard
    const strokeColor = mode === 'pulse' ? '#38bdf8' : '#00f5ff';

    const getCmdEntityId = (c: any): string => {
      return (
        c.anchorId ||
        c.anchor_id ||
        c.semanticRef?.entityId ||
        c.semantic_ref?.entity_id ||
        ''
      );
    };
    const reversedId = entityId.length === 2 ? entityId[1] + entityId[0] : '';
    const matchesEntity = (id: string) => id === entityId || (reversedId !== '' && id === reversedId);

    // 1. Every command carrying this entity id (and its reversed two-letter form). The styled
    //    renderer covers all command types, so FOCUS highlights whatever is actually drawn.
    const matchedCommands = (this.activeDiagram?.commands ?? []).filter((c: any) => matchesEntity(getCmdEntityId(c)));
    if (matchedCommands.length > 0) {
      const group = new Konva.Group({ opacity: 0, name: 'focus_group' });
      for (const mcmd of matchedCommands) {
        const s = this.createHighlightShapeForCmd(mcmd as DiagramCommand, strokeColor);
        if (s) group.add(s);
      }
      if (group.getChildren().length > 0) {
        this.pushFocusGroup(group);
        this.animateFocusGroup(group, mode);
        return;
      }
    }

    // 2. Reveal group (e.g. setup, g_setup, parallel, g_parallel)
    const matchRevealTarget = (targetId?: string, entId?: string) => {
      if (!targetId || !entId) return false;
      if (targetId === entId) return true;
      if (targetId === `g_${entId}` || `g_${targetId}` === entId) return true;
      return false;
    };

    const reveal = this.activeDiagram?.reveals?.find((r: any) =>
      matchRevealTarget(r.targetId || r.target_id, entityId)
    );
    const revCmdIndices = reveal ? (reveal.commandIndices || (reveal as any).command_indices) : null;
    if (reveal && Array.isArray(revCmdIndices) && revCmdIndices.length > 0) {
      const group = new Konva.Group({ opacity: 0, name: 'focus_group' });
      for (const idx of revCmdIndices) {
        const cmd = this.activeDiagram?.commands?.[idx];
        if (cmd) {
          const s = this.createHighlightShapeForCmd(cmd, strokeColor);
          if (s) group.add(s as Konva.Group);
        }
      }
      if (group.getChildren().length > 0) {
        this.pushFocusGroup(group);
        this.animateFocusGroup(group, mode);
        return;
      }
    }

    // 3. Anchor / endpoint / bounding-box fallback
    const getPointCoord = (pId: string): [number, number] | null => {
      const ptCmd = this.activeDiagram?.commands?.find(
        (c: any) => c.type === 'DRAW_POINT' && getCmdEntityId(c) === pId
      );
      if (ptCmd && ptCmd.params.length >= 2) {
        return [ptCmd.params[0], ptCmd.params[1]];
      }
      const ptAnchor = this.activeAnchors.find((a) => a.id === pId);
      if (ptAnchor) {
        return [ptAnchor.x + ptAnchor.width / 2, ptAnchor.y + ptAnchor.height / 2];
      }
      return null;
    };

    const anchor = this.activeAnchors.find((a) => a.id === entityId || (reversedId ? a.id === reversedId : false));
    const cmd = this.activeDiagram?.commands?.find((c: any) => {
      const cId = getCmdEntityId(c);
      return cId === entityId || (reversedId ? cId === reversedId : false);
    });

    // If entity is a 2-letter segment (e.g. AB, DE) and no direct line command was matched,
    // derive the line segment directly from endpoint anchors (point A -> point B)
    let endpointLine: [number, number, number, number] | null = null;
    if ((!cmd || (cmd.type !== 'DRAW_LINE' && cmd.type !== 'DRAW_RAY')) && entityId.length === 2) {
      const p1 = getPointCoord(entityId[0]);
      const p2 = getPointCoord(entityId[1]);
      if (p1 && p2) {
        endpointLine = [p1[0], p1[1], p2[0], p2[1]];
      }
    }

    if (!anchor && !cmd && !endpointLine) {
      console.warn('[focus] no geometry matched entity', entityId, {
        diagramLoaded: Boolean(this.activeDiagram),
        anchors: this.activeAnchors.length,
      });
      return;
    }

    const minX = anchor ? anchor.x : (cmd && cmd.params.length >= 2 ? cmd.params[0] - 10 : 0);
    const minY = anchor ? anchor.y : (cmd && cmd.params.length >= 2 ? cmd.params[1] - 10 : 0);
    const maxX = anchor ? anchor.x + anchor.width : minX + 20;
    const maxY = anchor ? anchor.y + anchor.height : minY + 20;

    const isPoint =
      (cmd && cmd.type === 'DRAW_POINT') ||
      entityId.length === 1 ||
      (anchor && anchor.width <= 35 && anchor.height <= 35) ||
      Boolean(
        this.activeDiagram?.labelGlossary?.[entityId]?.title?.toLowerCase().includes('point') ||
        (this.activeDiagram as any)?.label_glossary?.[entityId]?.title?.toLowerCase().includes('point')
      );

    // Construct precise geometric highlight shape based on entity type
    let focusShape: Konva.Group | Konva.Shape;
    if (endpointLine) {
      focusShape = new Konva.Line({
        points: endpointLine,
        stroke: strokeColor,
        strokeWidth: 6.5,
        lineCap: 'round',
        lineJoin: 'round',
        shadowColor: strokeColor,
        shadowBlur: 16,
        shadowOpacity: 1,
        opacity: 0,
      });
    } else if (cmd && (cmd.type === 'DRAW_LINE' || cmd.type === 'DRAW_RAY') && cmd.params.length >= 4) {
      focusShape = new Konva.Line({
        points: cmd.params.slice(0, 4),
        stroke: strokeColor,
        strokeWidth: 6.5,
        lineCap: 'round',
        lineJoin: 'round',
        shadowColor: strokeColor,
        shadowBlur: 16,
        shadowOpacity: 1,
        opacity: 0,
      });
    } else if (isPoint) {
      const coord = getPointCoord(entityId) || [
        anchor ? anchor.x + anchor.width / 2 : minX,
        anchor ? anchor.y + anchor.height / 2 : minY,
      ];
      const ptGroup = new Konva.Group({ x: coord[0], y: coord[1], opacity: 0 });
      ptGroup.add(
        new Konva.Circle({
          x: 0,
          y: 0,
          radius: 18,
          stroke: strokeColor,
          strokeWidth: 3.5,
          fill: 'rgba(0, 245, 255, 0.22)',
          shadowColor: strokeColor,
          shadowBlur: 16,
          shadowOpacity: 1,
        })
      );
      ptGroup.add(
        new Konva.Circle({
          x: 0,
          y: 0,
          radius: 4,
          fill: '#ffffff',
        })
      );
      focusShape = ptGroup;
    } else if (cmd && cmd.type === 'DRAW_CIRCLE' && cmd.params.length >= 3) {
      const [cx, cy, r] = cmd.params;
      focusShape = new Konva.Circle({
        x: cx,
        y: cy,
        radius: r,
        stroke: strokeColor,
        strokeWidth: 4.5,
        shadowColor: strokeColor,
        shadowBlur: 14,
        shadowOpacity: 1,
        opacity: 0,
      });
    } else if (cmd && (cmd.type === 'DRAW_POLYGON' || cmd.type === 'DRAW_POLYLINE') && cmd.params.length >= 6) {
      focusShape = new Konva.Line({
        points: cmd.params,
        closed: cmd.type === 'DRAW_POLYGON',
        stroke: strokeColor,
        strokeWidth: 5,
        fill: 'rgba(0, 245, 255, 0.15)',
        shadowColor: strokeColor,
        shadowBlur: 14,
        shadowOpacity: 1,
        opacity: 0,
      });
    } else {
      // Default: anchor bounding box outline
      focusShape = new Konva.Rect({
        x: minX - 4,
        y: minY - 4,
        width: Math.max(20, maxX - minX + 8),
        height: Math.max(20, maxY - minY + 8),
        stroke: strokeColor,
        strokeWidth: 3,
        cornerRadius: 6,
        fill: 'rgba(0, 245, 255, 0.12)',
        shadowColor: strokeColor,
        shadowBlur: 12,
        shadowOpacity: 1,
        opacity: 0,
      });
    }

    const wrap = new Konva.Group({ opacity: 0, name: 'focus_group' });
    if (mode === 'spotlight') {
      wrap.add(new Konva.Rect({
        x: DIAGRAM_ZONE.x,
        y: DIAGRAM_ZONE.y,
        width: DIAGRAM_ZONE.width,
        height: DIAGRAM_ZONE.height,
        fill: 'rgba(0, 0, 0, 0.45)',
        name: 'focus_veil',
      }));
    }
    wrap.add(focusShape as any);
    this.pushFocusGroup(wrap);
    this.animateFocusGroup(wrap, mode);
  }

  /** Public so App can drop a transient highlight when a turn is cancelled. */
  public clearFocus(): void {
    this.currentFocusStepKey = '';
    this.fadeOldFocus(true);
    this.spotlightLayer.destroyChildren();
    this.spotlightLayer.batchDraw();
  }

  private handleEmphasize(op: BoardOp, instant: boolean): void {
    const rowId = op.emphasizeRowId || 'w1';
    const row = this.layout.visibleRows.find((r) => r.rowId === rowId);
    if (!row) return;

    // Box around row rectangle + result highlight committed to Draw
    const highlightBox = new Konva.Rect({
      name: 'emphasize_box',
      x: row.rect.x - 4,
      y: row.rect.y - 2,
      width: row.rect.width + 8,
      height: row.rect.height + 4,
      stroke: '#fde047',
      strokeWidth: 2,
      fill: 'rgba(253, 224, 71, 0.12)',
      cornerRadius: 6,
      opacity: instant ? 1 : 0,
    });

    this.drawLayer.add(highlightBox);

    if (!instant) {
      gsap.to(highlightBox, {
        opacity: 1,
        duration: 0.3,
        onUpdate: () => this.drawLayer.batchDraw(),
      });
    } else {
      this.drawLayer.batchDraw();
    }
  }

  /**
   * Clear only work-column ink (rows and emphasize boxes), keeping the verified figure intact.
   * Used for page turns (automatic column overflow AND planned [PAGE_BREAK]) during diagram
   * lessons so the figure stays as the reference anchor.
   *
   * NEVER call `txManager.clearAll()` here: it calls `drawLayer.destroyChildren()` and erases
   * the committed diagram. Identify work ink strictly by node name so
   * diagram groups (diagram_instant / reveal_* / annot_*) and their anchors survive.
   */
  public clearWorkRows(): void {
    this.clearFocus();

    const isWorkInk = (n: Konva.Node): boolean => {
      const name = n.name() || '';
      return name.startsWith('row_') || name === 'emphasize_box';
    };

    for (const layer of [this.drawLayer, this.animLayer]) {
      const doomed = layer.getChildren().filter(isWorkInk);
      for (const node of doomed) {
        gsap.killTweensOf(node);
        if (node instanceof Konva.Group) gsap.killTweensOf(node.getChildren());
      }
      // Drop the in-flight write transactions so their (now killed) onComplete cannot
      // re-commit the destroyed row onto the draw layer.
      this.txManager.forgetNodes(doomed);
      for (const node of doomed) node.destroy();
      layer.batchDraw();
    }

    // Keep the ledger (op ids stay drawn across sub-pages); the caller has already turned
    // the layout, so remember the new sub-page's id.
    this.ledger.turn(this.layout.currentPageId);
    this.onLayoutChanged();
  }

  /** Automatic work-column overflow: the layout already incremented its page id. */
  public onOverflowTurn(newPageId: string): void {
    const fromSub = this.ledger.currentSubId;
    if (this.layout.getLayoutMode() === 'DIAGRAM_STANDARD_1COL') {
      this.clearWorkRows();
    } else {
      this.clearBoard(true);
    }
    this.ledger.turn(newPageId);
    this.reportPageTurned(fromSub, newPageId, 'overflow', this.placingOp?.opId);
  }

  private handleAnnotate(op: BoardOp, instant: boolean): void {
    const entityId = op.entityId;
    if (!entityId) return;

    const commands = this.deferredAnnotations.get(entityId);
    if (!commands || commands.length === 0) {
      console.warn('[annotate] no withheld label for', entityId);
      return;
    }

    const group = new Konva.Group({ name: `annot_${entityId}` });
    for (const cmd of commands) {
      this.renderDiagramCommand(cmd, group);
    }
    this.drawLayer.add(group);
    this.drawLayer.batchDraw();
  }

  /**
   * Handle a planned [PAGE_BREAK]: erase the work column and turn to a new page while keeping
   * the verified figure on the right as the reference anchor. In text-only lessons there is no
   * figure, so this is a clean fresh page for them too. The page id advances so board_report /
   * page_ops never overwrite the previous page.
   */
  private handlePageBreak(op: BoardOp, generation: number): void {
    console.log('[page_break] planned transition:', op.pageTitle || '(untitled)');
    const fromSub = this.layout.currentPageId;
    this.layout.turnPage();
    const toSub = this.layout.currentPageId;
    this.clearWorkRows();
    this.onLayoutChanged();
    this.onSendReport(this.layout.getBoardReport());
    this.reportPageTurned(fromSub, toSub, 'page_break');
  }

  /** page_turned: the server ledger splits/advances its sub-pages to match the client. */
  private reportPageTurned(fromSubId: string, toSubId: string, cause: 'overflow' | 'page_break', atOpId?: string): void {
    const rootPageId = this.layout.currentPageId.replace(/_p\d+$/, '');
    const report: PageTurnedReport = {
      type: 'page_turned',
      rootPageId,
      fromSubId,
      toSubId,
      cause,
      atOpId,
    };
    this.onSendReport(report);
  }

  /**
   * Step progress: cumulative `started`/`completed`/`final` reports replace the
   * legacy per-step ack. Heard = completed by words|caption|stall and contiguous from step 0;
   * a `flush` completion or a gap never advances the cursor.
   */
  private progressTurnId = '';
  private startedUpTo = -1;
  private heardUpTo = -1;
  private heardFlags = new Map<number, boolean>();

  private ensureProgressTurn(step: Step): void {
    if (step.turnId !== this.progressTurnId) {
      this.progressTurnId = step.turnId;
      this.startedUpTo = -1;
      this.heardUpTo = -1;
      this.heardFlags.clear();
    }
  }

  private sendProgress(step: Step, event: 'started' | 'completed' | 'final', by?: CompletedBy): void {
    const report: StepProgressReport = {
      type: 'step_progress',
      turnId: step.turnId,
      generation: step.generation,
      stepIndex: step.stepIndex,
      event,
      completedBy: by,
      // Only THIS step's ops that were actually drawn.
      drawnOpIds: step.ops.map((op) => op.opId).filter((id) => this.ledger.hasDrawn(id)),
      startedUpTo: this.startedUpTo,
      heardUpTo: this.heardUpTo,
      earlyOps: 0,
      lookaheadJumps: 0,
    };
    this.onSendReport(report);
  }

  public reportStarted(step: Step): void {
    if (step.generation < this.activeGeneration) return;
    this.ensureProgressTurn(step);
    this.startedUpTo = Math.max(this.startedUpTo, step.stepIndex);
    // The reveal clock learns every step start from the matcher's hook.
    this.revealClock.onStepStarted(step, step.isLast === true);
    this.sendProgress(step, 'started');
  }

  /** A turn ending without an isLast step reveals every unrevealed group. Only
   *  turn_ended does this — never turn_cancelled (an interrupted lesson withholds its
   *  construction until it is resumed). */
  public revealAll(): void {
    this.revealClock.revealAll();
  }

  public reportCompleted(step: Step, by: CompletedBy): void {
    if (step.generation < this.activeGeneration) return;
    this.ensureProgressTurn(step);

    // Gracefully fade the step's focus highlights after a brief delay so the student
    // has a moment to register the highlighted parts before the next step begins
    setTimeout(() => {
      const stepKey = `${step.turnId}:${step.stepIndex}`;
      if (this.currentFocusStepKey === stepKey) {
        this.fadeOldFocus(false);
      }
    }, 1200);

    if (by !== 'flush') {
      this.heardFlags.set(step.stepIndex, true);
      while (this.heardFlags.get(this.heardUpTo + 1)) this.heardUpTo += 1;
    }
    this.sendProgress(step, 'completed', by);
  }

  /** Sent once more on turn_ended / turn_cancelled to repair any lost report. */
  public reportFinal(): void {
    if (!this.progressTurnId) return;
    const step: Step = {
      turnId: this.progressTurnId,
      generation: this.activeGeneration,
      stepIndex: Math.max(this.startedUpTo, 0),
      spokenText: '',
      words: [],
      ops: [],
    };
    this.sendProgress(step, 'final');
  }

  /**
   * page_restore: clear Draw, redraw diagram and ops instantly (no animation), reset layout.
   */
  public pageRestore(pageId: string, diagram: VerifiedDiagram | null | undefined, ops: BoardOp[], generation: number): void {
    this.setGeneration(generation);
    this.clearBoard(true);
    this.layout.reset(pageId);

    if (diagram) {
      this.commitDiagram(diagram, '', generation, true);
    } else {
      this.layout.setLayoutMode('TEXT_ONLY_3COL');
      this.onLayoutChanged();
    }

    for (const op of ops) {
      this.executeOp(op, generation, '', true);
    }
  }
}

// frontend/src/whiteboard/revealClock.ts
// The reveal clock. Figure groups appear when the speech reaches
// them, never on wall-clock timers: the `base` group of every staged block
// reveals at the first heard word of the turn; a reveal group reveals at the start of the
// first step that FOCUSes its target id (before that step's ops fire — the matcher fires
// onStepStarted before any op); everything unrevealed reveals on the last step or turn end.
import gsap from 'gsap';
import Konva from 'konva';
import { Step } from '../types/events';

export interface StagedGroup {
  targetId: string;
  nodes: Konva.Group;
  entityIds?: Set<string>;
}

export class RevealClock {
  private staged = new Map<string, StagedGroup[]>();
  private revealed = new Set<Konva.Group>();
  private turnStarted = false;

  /** Register a block's hidden groups (drawn at opacity 0). `reference` blocks (late figures)
   *  and snapshot restores reveal everything at stage time. If the turn's first step
   *  already started (a late commit, e.g. the reference figure arriving mid-speech), the base
   *  must reveal right away — otherwise it stays hidden for the whole turn. */
  stage(blockId: string, groups: StagedGroup[], reference: boolean): void {
    this.staged.set(blockId, groups);
    if (reference) {
      for (const g of groups) this.reveal(g);
      this.staged.delete(blockId);
    } else if (this.turnStarted) {
      for (const g of groups) {
        if (g.targetId === 'base') this.reveal(g);
      }
    }
  }

  /** A block left the board (fade-out or clearDiagram(blockId)): stop tracking its groups so
   *  a later step start can never animate a destroyed node. */
  unstage(blockId: string): void {
    this.staged.delete(blockId);
  }

  /** Reveal any staged group containing the given entity ID. */
  revealByEntityId(entityId: string): void {
    if (!entityId) return;
    const cleanFid = entityId.replace(/^[a-z0-9]+_/, '');
    for (const groups of this.staged.values()) {
      for (const g of groups) {
        if (g.targetId === entityId) {
          this.reveal(g);
          continue;
        }
        if (g.entityIds) {
          if (g.entityIds.has(entityId) || g.entityIds.has(cleanFid)) {
            this.reveal(g);
            continue;
          }
          for (const eid of g.entityIds) {
            const cleanEid = eid.replace(/^[a-z0-9]+_/, '');
            if (eid === entityId || cleanEid === cleanFid || cleanEid === entityId || eid === cleanFid) {
              this.reveal(g);
              break;
            }
          }
        }
      }
    }
  }

  /** Called by the matcher's step-start hook (executor.reportStarted). */
  onStepStarted(step: Step, isLast: boolean): void {
    if (!this.turnStarted) {
      this.turnStarted = true;
      for (const groups of this.staged.values()) {
        for (const g of groups) {
          if (g.targetId === 'base') this.reveal(g);
        }
      }
    }
    const focusIds = new Set(
      (step.ops || []).filter((o) => o.kind === 'FOCUS').map((o) => o.entityId || '')
    );
    for (const groups of this.staged.values()) {
      for (const g of groups) {
        if (g.targetId !== 'base') {
          let matched = focusIds.has(g.targetId);
          if (!matched && g.entityIds && focusIds.size > 0) {
            for (const fid of focusIds) {
              if (!fid) continue;
              if (g.entityIds.has(fid)) {
                matched = true;
                break;
              }
              const cleanFid = fid.replace(/^[a-z0-9]+_/, '');
              if (g.entityIds.has(cleanFid)) {
                matched = true;
                break;
              }
              for (const eid of g.entityIds) {
                const cleanEid = eid.replace(/^[a-z0-9]+_/, '');
                if (eid === fid || cleanEid === cleanFid || cleanEid === fid || eid === cleanFid) {
                  matched = true;
                  break;
                }
                if (fid.length === 2 && eid.includes(fid[1] + fid[0])) {
                  matched = true;
                  break;
                }
                if (
                  cleanEid.startsWith('label_') &&
                  (cleanEid.endsWith(`_${cleanFid}`) || cleanEid.endsWith(`_${cleanFid.replace(/^point_/, '')}`))
                ) {
                  matched = true;
                  break;
                }
              }
              if (matched) break;
            }
          }
          if (matched) this.reveal(g);
        }
      }
    }
    if (isLast) this.revealAll();
  }

  /** Turn end without an isLast step: every unrevealed group reveals. */
  revealAll(): void {
    for (const groups of this.staged.values()) {
      for (const g of groups) this.reveal(g);
    }
    this.staged.clear();
  }

  /** New board / new turn: forget everything staged and kill any in-flight reveal tween so
   *  its onUpdate can never batchDraw a node that was cleared or unmounted. */
  reset(): void {
    for (const group of this.revealed) {
      gsap.killTweensOf(group);
    }
    this.staged.clear();
    this.revealed.clear();
    this.turnStarted = false;
  }

  private reveal(g: StagedGroup): void {
    if (this.revealed.has(g.nodes)) return;
    this.revealed.add(g.nodes);
    gsap.to(g.nodes, {
      opacity: 1,
      duration: 0.4,
      onUpdate: () => g.nodes.getLayer()?.batchDraw(),
    });
  }
}

// frontend/src/whiteboard/drawTransactions.ts
// Draw transactions: track in-flight ink and commit it to the draw layer.

import Konva from 'konva';

export interface DrawTransaction {
  id: string;
  nodes: Konva.Node[];
  inFlightAnimations: Array<{ kill: () => void }>;
  committed: boolean;
}

export class TransactionManager {
  private activeTransactions: Map<string, DrawTransaction> = new Map();
  private animLayer: Konva.Layer | null = null;
  private drawLayer: Konva.Layer | null = null;
  private txCounter: number = 0;

  public setLayers(animLayer: Konva.Layer, drawLayer: Konva.Layer) {
    this.animLayer = animLayer;
    this.drawLayer = drawLayer;
  }

  public beginDrawTransaction(): string {
    const id = `tx_${Date.now()}_${++this.txCounter}`;
    const tx: DrawTransaction = {
      id,
      nodes: [],
      inFlightAnimations: [],
      committed: false,
    };
    this.activeTransactions.set(id, tx);
    return id;
  }

  public trackNode(txId: string, node: Konva.Node) {
    const tx = this.activeTransactions.get(txId);
    if (tx) {
      tx.nodes.push(node);
    }
  }

  public trackAnimation(txId: string, killFn: () => void) {
    const tx = this.activeTransactions.get(txId);
    if (tx) {
      tx.inFlightAnimations.push({ kill: killFn });
    }
  }

  /**
   * Commit moves the tracked nodes from animLayer to drawLayer and calls drawLayer.batchDraw().
   */
  public commitDrawTransaction(txId: string): void {
    const tx = this.activeTransactions.get(txId);
    if (!tx || tx.committed) return;

    tx.committed = true;

    if (this.drawLayer) {
      for (const node of tx.nodes) {
        // Move from Anim to Draw
        node.moveTo(this.drawLayer);
      }
      this.drawLayer.batchDraw();
    }

    if (this.animLayer) {
      this.animLayer.batchDraw();
    }

    this.activeTransactions.delete(txId);
  }

  /**
   * Cancel, on turn_cancelled with keep_board=true, finishes the in-flight strokes instantly
   * and commits them: ink already visible stays.
   */
  public cancelAll(keepBoard: boolean = true): void {
    for (const [id, tx] of this.activeTransactions.entries()) {
      // Kill active in-flight animations
      for (const anim of tx.inFlightAnimations) {
        try {
          anim.kill();
        } catch {
          // ignore
        }
      }

      if (keepBoard) {
        // Commit whatever has been rendered to drawLayer
        if (this.drawLayer) {
          for (const node of tx.nodes) {
            node.moveTo(this.drawLayer);
          }
          this.drawLayer.batchDraw();
        }
      } else {
        // Drop nodes
        for (const node of tx.nodes) {
          node.destroy();
        }
      }
    }

    this.activeTransactions.clear();
    if (this.animLayer) this.animLayer.batchDraw();
    if (this.drawLayer) this.drawLayer.batchDraw();
  }

  public clearAll(): void {
    this.cancelAll(false);
    if (this.drawLayer) {
      this.drawLayer.destroyChildren();
      this.drawLayer.batchDraw();
    }
    if (this.animLayer) {
      this.animLayer.destroyChildren();
      this.animLayer.batchDraw();
    }
  }

  /**
   * Forget transactions whose tracked nodes were removed outside the manager (e.g. a page turn
   * that erases only work rows). Prevents a killed write-tween's onComplete from later
   * re-committing a destroyed node back onto the draw layer.
   */
  public forgetNodes(nodes: Konva.Node[]): void {
    if (nodes.length === 0) return;
    const removed = new Set(nodes);
    for (const [id, tx] of this.activeTransactions.entries()) {
      if (tx.nodes.some((n) => removed.has(n))) {
        for (const anim of tx.inFlightAnimations) {
          try {
            anim.kill();
          } catch {
            /* ignore */
          }
        }
        this.activeTransactions.delete(id);
      }
    }
  }
}

// frontend/src/whiteboard/boardLedger.ts
// Per-sub-page record of drawn ops: an op is drawn at most once.
import { BoardOp } from '../types/events';

export class BoardLedger {
  private subs = new Map<string, BoardOp[]>();
  private opSub = new Map<string, string>();
  private current: string;

  constructor(rootSubId: string) {
    this.current = rootSubId;
    this.subs.set(rootSubId, []);
  }

  get currentSubId(): string {
    return this.current;
  }

  record(op: BoardOp): void {
    if (this.opSub.has(op.opId)) return;
    const ops = this.subs.get(this.current);
    if (ops) ops.push(op);
    else this.subs.set(this.current, [op]);
    this.opSub.set(op.opId, this.current);
  }

  hasDrawn(opId: string): boolean {
    return this.opSub.has(opId);
  }

  subPageOfOp(opId: string): string | undefined {
    return this.opSub.get(opId);
  }

  turn(newSubId: string): void {
    this.current = newSubId;
    if (!this.subs.has(newSubId)) this.subs.set(newSubId, []);
  }

  opsOf(subId: string): BoardOp[] {
    return [...(this.subs.get(subId) ?? [])];
  }

  reset(rootSubId: string): void {
    this.subs.clear();
    this.opSub.clear();
    this.current = rootSubId;
    this.subs.set(rootSubId, []);
  }
}

// frontend/src/whiteboard/eventSequencer.ts
// (epoch, seq) ordering and duplicate suppression for tutor.events.

export type ResyncReason = 'gap' | 'reconnected' | 'epoch';

export const SEQ_GAP_HOLD_MS = 1500;
export const SEQ_MAX_HOLD_MS = 5000;
export const SEQ_MAX_HELD = 200;

export type Deliver = (evt: any) => void;
export type RequestResync = (
  epoch: string | null,
  lastSeq: number,
  reason: ResyncReason,
  wantSnapshot?: boolean,
) => void;

export class EventSequencer {
  private epoch: string | null = null;
  private next = 0;                       // first undelivered seq
  private held = new Map<number, any>();
  private heldAt = new Map<number, number>();
  private ready = false;
  private gapTimer: ReturnType<typeof setTimeout> | null = null;
  private lastResyncAt = 0;

  constructor(private deliver: Deliver, private requestResync: RequestResync) {}

  /**
   * Build a sequencer for a session. Whiteboard (a child) mounts before App's effects run, so
   * its executor-ready callback may fire while no sequencer exists yet: adopt that readiness
   * here, otherwise every event is held forever and the hold timeout floods resync requests.
   */
  public static create(deliver: Deliver, requestResync: RequestResync, executorReady: boolean): EventSequencer {
    const seq = new EventSequencer(deliver, requestResync);
    if (executorReady) seq.setReady(true);
    return seq;
  }

  public ingest(evt: any): void {
    const seq = evt?.seq;
    if (typeof seq !== 'number') {
      this.deliver(evt);                  // legacy event without a book
      return;
    }
    const epoch = evt.epoch ?? null;
    const type = evt?.type;

    // A delivered board_snapshot resets the book even inside the same epoch — the
    // server sends one exactly when the outbox cannot fill a gap, so holding it deadlocks.
    if (type === 'board_snapshot') {
      if (epoch === this.epoch && seq < this.next) return;   // stale duplicate
      this.epoch = epoch;
      this.next = seq;
      this.held.clear();
      this.heldAt.clear();
      this.clearGapTimer();
      if (this.ready) {
        this.deliverNow(evt);
        this.drain();
      } else {
        this.hold(seq, evt);
      }
      return;
    }

    if (this.epoch === null) {
      this.epoch = epoch;
      if (this.next === 0) this.next = seq;   // first event of the book
    }
    if (epoch !== this.epoch) {
      // A new epoch is adopted on seq == 1; anything else asks for a resync.
      if (seq === 1) {
        this.epoch = epoch;
        this.next = seq;
        this.held.clear();
        this.heldAt.clear();
        this.clearGapTimer();
        if (this.ready) {
          this.deliverNow(evt);
          this.drain();
        } else {
          this.hold(seq, evt);
        }
      } else {
        this.hold(seq, evt);
        this.requestResyncThrottled(epoch, 0, 'epoch');
      }
      return;
    }

    if (seq < this.next) return;          // duplicate
    if (seq === this.next && this.ready) {
      this.deliverNow(evt);
      this.drain();
    } else {
      this.hold(seq, evt);
    }
  }

  public setReady(ready: boolean): void {
    this.ready = ready;
    if (ready) this.drain();
  }

  public onReconnected(): void {
    this.requestResync(this.epoch, this.next - 1, 'reconnected');
  }

  public get lastDelivered(): number {
    return this.next - 1;
  }

  private deliverNow(evt: any): void {
    this.deliver(evt);
    if (evt?.type === 'board_snapshot') {
      this.next = (evt.seq as number) + 1;
      for (const [seq] of this.held) {
        if (seq <= evt.seq) {
          this.held.delete(seq);
          this.heldAt.delete(seq);
        }
      }
    } else {
      this.next = (evt.seq as number) + 1;
    }
  }

  private drain(): void {
    while (this.ready) {
      const evt = this.held.get(this.next);
      if (!evt) break;
      this.held.delete(this.next);
      this.heldAt.delete(this.next);
      this.deliverNow(evt);
    }
    if (this.held.size === 0) this.clearGapTimer();
  }

  private hold(seq: number, evt: any): void {
    if (!this.held.has(seq)) {
      this.held.set(seq, evt);
      this.heldAt.set(seq, Date.now());
    }
    if (this.held.size > SEQ_MAX_HELD) {
      this.requestResyncThrottled(this.epoch, this.next - 1, 'gap', true);
      return;
    }
    const oldest = Math.min(...this.heldAt.values());
    if (Date.now() - oldest > SEQ_MAX_HOLD_MS) {
      this.requestResyncThrottled(this.epoch, this.next - 1, 'gap', true);
      return;
    }
    if (this.gapTimer === null) {
      this.gapTimer = setTimeout(() => {
        this.gapTimer = null;
        this.requestResync(this.epoch, this.next - 1, 'gap');
      }, SEQ_GAP_HOLD_MS);
    }
  }

  /** Rate-limit immediate resync demands: without this every held packet past the max-hold
   *  threshold asks again, and the server's reply is held again -> an endless flood. */
  private requestResyncThrottled(
    epoch: string | null, lastSeq: number, reason: ResyncReason, wantSnapshot: boolean = false,
  ): void {
    const now = Date.now();
    if (now - this.lastResyncAt < SEQ_GAP_HOLD_MS) return;
    this.lastResyncAt = now;
    if (wantSnapshot) this.requestResync(epoch, lastSeq, reason, true);
    else this.requestResync(epoch, lastSeq, reason);
  }

  private clearGapTimer(): void {
    if (this.gapTimer !== null) {
      clearTimeout(this.gapTimer);
      this.gapTimer = null;
    }
  }
}

// frontend/src/__tests__/eventSequencer.test.ts
// (epoch, seq) ordering, dedup and resync requests.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventSequencer, SEQ_GAP_HOLD_MS, SEQ_MAX_HELD } from '../whiteboard/eventSequencer';

const evt = (seq: number, epoch = 'aaaa1111', type = 'step') => ({ type, seq, epoch, generation: 1 });

describe('EventSequencer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function make() {
    const delivered: any[] = [];
    const resync = vi.fn();
    const seq = new EventSequencer((e) => delivered.push(e), resync);
    seq.setReady(true);
    return { seq, delivered, resync };
  }

  it('dedup', () => {
    const { seq, delivered } = make();
    seq.ingest(evt(1));
    seq.ingest(evt(1));
    expect(delivered.map((e) => e.seq)).toEqual([1]);
  });

  it('holds_until_gap_filled', () => {
    const { seq, delivered } = make();
    seq.ingest(evt(1));
    seq.ingest(evt(3));                 // held: 2 is missing
    expect(delivered.map((e) => e.seq)).toEqual([1]);
    seq.ingest(evt(2));
    expect(delivered.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('gap_timeout_requests_resync', () => {
    const { seq, delivered, resync } = make();
    seq.ingest(evt(1));
    seq.ingest(evt(3));                     // held: 2 is missing
    expect(delivered.map((e) => e.seq)).toEqual([1]);
    vi.advanceTimersByTime(SEQ_GAP_HOLD_MS + 10);
    expect(resync).toHaveBeenCalledWith('aaaa1111', 1, 'gap');
  });

  it('max_hold_requests_snapshot', () => {
    const { seq, resync } = make();
    seq.ingest(evt(1));
    for (let i = 3; i <= SEQ_MAX_HELD + 3; i++) seq.ingest(evt(i));
    expect(resync).toHaveBeenCalledWith('aaaa1111', 1, 'gap', true);
  });

  it('new_epoch_on_seq1_adopted', () => {
    const { seq, delivered } = make();
    seq.ingest(evt(1, 'aaaa1111'));
    seq.ingest(evt(1, 'bbbb2222'));
    expect(delivered.map((e) => e.epoch)).toEqual(['aaaa1111', 'bbbb2222']);
  });

  it('new_epoch_mid_stream_requests_resync', () => {
    const { seq, delivered, resync } = make();
    seq.ingest(evt(1, 'aaaa1111'));
    seq.ingest(evt(5, 'bbbb2222'));
    expect(delivered.map((e) => e.epoch)).toEqual(['aaaa1111']);
    expect(resync).toHaveBeenCalledWith('bbbb2222', 0, 'epoch');
  });

  it('holds_until_executor_ready', () => {
    const delivered: any[] = [];
    const seq = new EventSequencer((e) => delivered.push(e), vi.fn());
    seq.ingest(evt(1));
    seq.ingest(evt(2));
    expect(delivered).toEqual([]);
    seq.setReady(true);
    expect(delivered.map((e) => e.seq)).toEqual([1, 2]);
  });

  it('snapshot_resets_next_seq', () => {
    const { seq, delivered } = make();
    seq.ingest(evt(1));
    seq.ingest(evt(3, 'aaaa1111', 'board_snapshot'));
    // A same-epoch snapshot resets the book even when seq 2 never arrived;
    // the snapshot is authoritative for everything up to its seq.
    expect(delivered.map((e) => e.seq)).toEqual([1, 3]);
    expect(seq.lastDelivered).toBe(3);
    seq.ingest(evt(2));                 // stale now: the snapshot superseded it
    seq.ingest(evt(3));
    expect(delivered.map((e) => e.seq)).toEqual([1, 3]);
    seq.ingest(evt(4));
    expect(delivered.map((e) => e.seq)).toEqual([1, 3, 4]);
  });

  it('snapshot_delivers_same_epoch_after_gap', () => {
    const { seq, delivered, resync } = make();
    seq.ingest(evt(1));
    seq.ingest(evt(3));                 // held gap: 2 missing
    seq.ingest(evt(4));                 // held
    seq.ingest(evt(5, 'aaaa1111', 'board_snapshot'));

    expect(delivered.map((e) => e.seq)).toEqual([1, 5]);
    expect(seq.lastDelivered).toBe(5);
    expect(resync).not.toHaveBeenCalled();   // the snapshot filled the gap; no storm
  });

  it('create_ready_when_executor_already_mounted', () => {
    // App's sequencer is created after Whiteboard's (child) mount effect fired
    // handleExecutorReady -> setReady(true) on a null ref. The factory must adopt readiness,
    // or every event is held forever and the hold timeout floods resync requests.
    const delivered: any[] = [];
    const seq = EventSequencer.create((e) => delivered.push(e), vi.fn(), true);
    seq.ingest(evt(1));
    expect(delivered.map((e) => e.seq)).toEqual([1]);
  });

  it('resync_flood_throttled', () => {
    const { seq, resync } = make();
    seq.ingest(evt(1));
    // A flood of held packets past SEQ_MAX_HELD must not ask for a resync per packet.
    for (let i = 3; i <= SEQ_MAX_HELD + 50; i++) seq.ingest(evt(i));
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it('reconnected_requests_resync', () => {
    const { seq, resync } = make();
    seq.ingest(evt(1));
    seq.onReconnected();
    expect(resync).toHaveBeenCalledWith('aaaa1111', 1, 'reconnected');
  });
});

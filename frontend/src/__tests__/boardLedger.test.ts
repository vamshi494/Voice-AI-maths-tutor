// frontend/src/__tests__/boardLedger.test.ts
// BoardLedger.
import { describe, it, expect } from 'vitest';
import { BoardLedger } from '../whiteboard/boardLedger';
import { BoardOp } from '../types/events';

const op = (opId: string): BoardOp => ({ opId, kind: 'WRITE', atWord: 0, text: opId });

describe('BoardLedger', () => {
  it('records_into_current_sub', () => {
    const ledger = new BoardLedger('p');
    ledger.record(op('a:0:0'));
    expect(ledger.currentSubId).toBe('p');
    expect(ledger.opsOf('p').map((o) => o.opId)).toEqual(['a:0:0']);
    expect(ledger.subPageOfOp('a:0:0')).toBe('p');
    expect(ledger.hasDrawn('a:0:0')).toBe(true);
  });

  it('has_drawn_across_subpages', () => {
    const ledger = new BoardLedger('p');
    ledger.record(op('a:0:0'));
    ledger.turn('p_p2');
    ledger.record(op('a:1:0'));
    expect(ledger.hasDrawn('a:0:0')).toBe(true);
    expect(ledger.hasDrawn('a:1:0')).toBe(true);
    expect(ledger.hasDrawn('a:2:0')).toBe(false);
    expect(ledger.subPageOfOp('a:0:0')).toBe('p');
    expect(ledger.subPageOfOp('a:1:0')).toBe('p_p2');
  });

  it('turn_switches_current', () => {
    const ledger = new BoardLedger('p');
    ledger.turn('p_p2');
    expect(ledger.currentSubId).toBe('p_p2');
    ledger.record(op('a:1:0'));
    expect(ledger.opsOf('p')).toEqual([]);
    expect(ledger.opsOf('p_p2').map((o) => o.opId)).toEqual(['a:1:0']);
  });

  it('reset_clears', () => {
    const ledger = new BoardLedger('p');
    ledger.record(op('a:0:0'));
    ledger.turn('p_p2');
    ledger.record(op('a:1:0'));
    ledger.reset('q');
    expect(ledger.currentSubId).toBe('q');
    expect(ledger.hasDrawn('a:0:0')).toBe(false);
    expect(ledger.hasDrawn('a:1:0')).toBe(false);
    expect(ledger.opsOf('p')).toEqual([]);
    expect(ledger.opsOf('p_p2')).toEqual([]);
    expect(ledger.opsOf('q')).toEqual([]);
  });
});

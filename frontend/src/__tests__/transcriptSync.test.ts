// frontend/src/__tests__/transcriptSync.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TranscriptSyncMatcher, TranscriptWordFeeder, normalizeWord, normalizeStep, wordsMatch, STALL_MS_SPEAKING, EMA_INITIAL_MS, EMA_ALPHA } from '../whiteboard/transcriptSync';
import { BoardOp, Step } from '../types/events';

describe('TranscriptSync speech-sync matcher', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('normalizes words identically to server', () => {
    const raw = "Let's <break time=\"0.5s\"/> find student's answer!";
    const tokens = normalizeWord(raw);
    expect(tokens).toEqual(["let's", 'find', "student's", 'answer']);

    // Curly apostrophe
    expect(normalizeWord('don’t')).toEqual(["don't"]);
  });

  it('normalize_step_keeps_source_index', () => {
    const camel = normalizeStep({
      turnId: 't1', generation: 2, stepIndex: 0, spokenText: 'a', words: ['a'], ops: [],
      sourceStepIndex: 5, isLast: true,
    });
    expect(camel.sourceStepIndex).toBe(5);
    expect(camel.isLast).toBe(true);

    const snake = normalizeStep({
      turn_id: 't2', generation: 1, step_index: 3, spoken_text: 'b', words: ['b'], ops: [],
      source_step_index: 7, is_last: false,
    });
    expect(snake.sourceStepIndex).toBe(7);
    expect(snake.isLast).toBe(false);
  });

  it('advances on exact word match and fires ops at at_word', () => {
    const firedOps: BoardOp[] = [];
    const matcher = new TranscriptSyncMatcher((op) => {
      firedOps.push(op);
    });

    const opWrite: BoardOp = {
      opId: 't1:0:0',
      kind: 'WRITE',
      atWord: 3, // Fire after 3 words ("first", "we", "solve")
      text: '2x = 6',
      rowId: 'w1',
    };

    const step: Step = {
      turnId: 't1',
      generation: 1,
      stepIndex: 0,
      spokenText: 'first we solve 2x = 6 and get the result',
      words: ['first', 'we', 'solve', 'and', 'get', 'the', 'result'],
      ops: [opWrite],
    };

    matcher.enqueueStep(step);
    matcher.setSpeaking(true);

    // Word 1: "first"
    matcher.onIncomingTranscript('first');
    expect(firedOps.length).toBe(0);

    // Word 2: "we"
    matcher.onIncomingTranscript('we');
    expect(firedOps.length).toBe(0);

    // Word 3: "solve" -> atWord 3 reached!
    matcher.onIncomingTranscript('solve');
    expect(firedOps.length).toBe(1);
    expect(firedOps[0].opId).toBe('t1:0:0');
  });

  it('ops_fire_at_given_atword', () => {
    const fired: string[] = [];
    const matcher = new TranscriptSyncMatcher((op) => {
      fired.push(op.opId);
    });
    matcher.beginTurn('t1');
    matcher.enqueueStep({
      turnId: 't1',
      generation: 1,
      stepIndex: 0,
      spokenText: 'look at the triangle now',
      words: ['look', 'at', 'the', 'triangle', 'now'],
      ops: [
        { opId: 't1:0:0', kind: 'FOCUS' as const, atWord: 1, entityId: 'tri' },
        { opId: 't1:0:1', kind: 'WRITE' as const, atWord: 5, text: 'AB = 5' },
      ],
    });
    matcher.setSpeaking(true);

    matcher.onIncomingTranscript('look');
    expect(fired).toEqual(['t1:0:0']);            // atWord 1 fires after the first word
    matcher.onIncomingTranscript('at the triangle');
    expect(fired).toEqual(['t1:0:0']);            // trailing WRITE is not retimed by the client
    matcher.onIncomingTranscript('now');
    expect(fired).toEqual(['t1:0:0', 't1:0:1']);
  });

  it('resyncs on lookahead within 6 words', () => {
    const firedOps: BoardOp[] = [];
    const matcher = new TranscriptSyncMatcher((op) => {
      firedOps.push(op);
    });

    const opWrite: BoardOp = {
      opId: 't1:0:0',
      kind: 'WRITE',
      atWord: 2,
      text: 'x = 3',
    };

    const step: Step = {
      turnId: 't1',
      generation: 1,
      stepIndex: 0,
      spokenText: 'one two three four five six seven',
      words: ['one', 'two', 'three', 'four', 'five', 'six', 'seven'],
      ops: [opWrite],
    };

    matcher.enqueueStep(step);
    matcher.setSpeaking(true);

    // A jump over > 1 word is buffered until the next incoming word confirms it.
    matcher.onIncomingTranscript('four');
    expect(firedOps.length).toBe(0);
    matcher.onIncomingTranscript('five');
    expect(firedOps.length).toBe(1);
    expect(firedOps[0].opId).toBe('t1:0:0');
  });

  it('article_a_does_not_match_at', () => {
    expect(wordsMatch('at', 'a')).toBe(false);

    const completed: number[] = [];
    const matcher = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    matcher.beginTurn('t');
    matcher.enqueueStep({
      turnId: 't', generation: 1, stepIndex: 0, spokenText: 'look at the board',
      words: ['look', 'at', 'the', 'board'], ops: [],
    });
    matcher.setSpeaking(true);
    matcher.onIncomingTranscript('a');
    expect(matcher.pointer.word).toBe(0);
    expect(completed).toEqual([]);
  });

  it('jump_requires_confirmation', () => {
    const completed: number[] = [];
    const matcher = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    matcher.beginTurn('t');
    matcher.enqueueStep({
      turnId: 't', generation: 1, stepIndex: 0, spokenText: 'alpha beta gamma delta epsilon',
      words: ['alpha', 'beta', 'gamma', 'delta', 'epsilon'], ops: [],
    });
    matcher.setSpeaking(true);

    matcher.onIncomingTranscript('alpha');
    expect(matcher.pointer.word).toBe(1);

    // Jump over two words: buffered, not applied yet.
    matcher.onIncomingTranscript('delta');
    expect(matcher.pointer.word).toBe(1);
    expect(completed).toEqual([]);

    // The word after the target confirms the jump and consumes both.
    matcher.onIncomingTranscript('epsilon');
    expect(completed).toEqual([0]);
  });

  it('short_words_never_jump', () => {
    const matcher = new TranscriptSyncMatcher(() => {});
    matcher.beginTurn('t');
    matcher.enqueueStep({
      turnId: 't', generation: 1, stepIndex: 0, spokenText: 'ab bc cd de',
      words: ['ab', 'bc', 'cd', 'de'], ops: [],
    });
    matcher.setSpeaking(true);
    matcher.onIncomingTranscript('ab');
    expect(matcher.pointer.word).toBe(1);
    matcher.onIncomingTranscript('de');   // length 2: never starts a jump
    expect(matcher.pointer.word).toBe(1);
    matcher.onIncomingTranscript('bc');
    expect(matcher.pointer.word).toBe(2);
  });

  it('ema_updates_from_word_times', () => {
    const matcher = new TranscriptSyncMatcher(() => {});
    matcher.beginTurn('t');
    matcher.enqueueStep({
      turnId: 't', generation: 1, stepIndex: 0, spokenText: 'one two three four',
      words: ['one', 'two', 'three', 'four'], ops: [],
    });
    matcher.setSpeaking(true);
    expect(matcher.msPerWord).toBe(EMA_INITIAL_MS);

    matcher.onIncomingTranscript('one');
    vi.advanceTimersByTime(500);
    matcher.onIncomingTranscript('two');
    expect(matcher.msPerWord).toBeCloseTo((1 - EMA_ALPHA) * EMA_INITIAL_MS + EMA_ALPHA * 500, 5);
  });

  it('plural_suffix_matches', () => {
    expect(wordsMatch('triangle', 'triangles')).toBe(true);
    expect(wordsMatch('triangles', 'triangle')).toBe(true);
    expect(wordsMatch('square', 'squared')).toBe(true);
    expect(wordsMatch('equal', 'equals')).toBe(true);
  });

  it('span matching collapses N letter tokens into one incoming token', () => {
    const firedOps: BoardOp[] = [];
    const matcher = new TranscriptSyncMatcher((op) => firedOps.push(op));
    matcher.beginTurn('t1');
    matcher.enqueueStep({
      turnId: 't1',
      generation: 1,
      stepIndex: 0,
      spokenText: 'look at triangle a b c with point d',
      words: ['look', 'at', 'triangle', 'a', 'b', 'c', 'with', 'point', 'd'],
      ops: [
        { opId: 't1:0:0', kind: 'FOCUS' as const, atWord: 6, entityId: 'tri_ABC' },
      ],
    });
    matcher.setSpeaking(true);

    matcher.onIncomingTranscript('look at triangle');
    expect(matcher.pointer.word).toBe(3);
    expect(firedOps.length).toBe(0);

    // Deepgram emits 'abc' as a single token!
    matcher.onIncomingTranscript('abc');
    expect(matcher.pointer.word).toBe(6);
    expect(firedOps.length).toBe(1);
    expect(firedOps[0].entityId).toBe('tri_ABC');

    matcher.onIncomingTranscript('with point d');
    // Step completed, pointer moves to next step index
    expect(matcher.pointer.step).toBe(1);
    expect(matcher.pointer.word).toBe(0);
  });

  it('stall fallback fires remaining ops after STALL_MS_SPEAKING ms of silence while speaking', () => {
    const firedOps: BoardOp[] = [];
    const matcher = new TranscriptSyncMatcher((op) => {
      firedOps.push(op);
    });

    const opWrite: BoardOp = {
      opId: 't1:0:0',
      kind: 'WRITE',
      atWord: 5,
      text: 'stalled op',
    };

    const step: Step = {
      turnId: 't1',
      generation: 1,
      stepIndex: 0,
      spokenText: 'hello world here is more text',
      words: ['hello', 'world', 'here', 'is', 'more', 'text'],
      ops: [opWrite],
    };

    matcher.enqueueStep(step);
    matcher.setSpeaking(true);

    // No transcripts arrive. Advance clock by STALL_MS_SPEAKING
    vi.advanceTimersByTime(STALL_MS_SPEAKING);

    // Op forced!
    expect(firedOps.length).toBe(1);
    expect(firedOps[0].opId).toBe('t1:0:0');
  });
});

// ---------------------------------------------------------------------------------------------
// Lifecycle hardening

describe('TranscriptSyncMatcher lifecycle', () => {
  const mkStep = (turnId: string, generation: number, stepIndex: number, text: string) => ({
    turnId, generation, stepIndex, spokenText: text,
    words: normalizeWord(text),
    ops: [{ opId: `${turnId}:${stepIndex}:0`, kind: 'WRITE' as const, atWord: 99, text: 'w' }],
  });

  it('never force-draws while the tutor is silent (steps arrive before their audio)', () => {
    vi.useFakeTimers();
    const fired: string[] = [];
    const m = new TranscriptSyncMatcher((op) => fired.push(op.opId));
    m.beginTurn('t1');
    m.enqueueStep(mkStep('t1', 1, 0, 'first we square both sides'));
    m.enqueueStep(mkStep('t1', 1, 1, 'then we add'));
    vi.advanceTimersByTime(10000);
    expect(fired).toEqual([]);
    m.setSpeaking(true);
    vi.advanceTimersByTime(STALL_MS_SPEAKING + 50);
    expect(fired).toEqual(['t1:0:0']);          // one step per stall window, not the whole board
    vi.useRealTimers();
  });

  it('a new turn discards the superseded turn and ops carry their own generation', () => {
    const fired: Array<[string, number]> = [];
    const m = new TranscriptSyncMatcher((op, gen) => fired.push([op.opId, gen]));
    m.beginTurn('old');
    m.enqueueStep(mkStep('old', 1, 0, 'the old step'));
    m.beginTurn('new');
    m.enqueueStep(mkStep('old', 1, 1, 'late old step'));   // late delivery of the old turn
    m.enqueueStep(mkStep('new', 2, 0, 'the new step'));
    m.onIncomingTranscript('the new step');
    expect(fired).toEqual([['new:0:0', 2]]);
  });

  it('reports each completed step once (drives step_ack)', () => {
    const done: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, (s) => done.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two'));
    m.enqueueStep(mkStep('t', 1, 1, 'three four'));
    m.onIncomingTranscript('one two three four');
    expect(done).toEqual([0, 1]);
  });

  it('reports_started_and_completed_by', () => {
    const started: number[] = [];
    const completed: Array<[number, string]> = [];
    const m = new TranscriptSyncMatcher(
      () => {},
      (s, by) => completed.push([s.stepIndex, by]),
      (s) => started.push(s.stepIndex),
    );
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three'));
    m.setSpeaking(true);

    m.onIncomingTranscript('one');
    expect(started).toEqual([0]);                 // onStepStarted on the first matched word
    expect(completed).toEqual([]);

    m.onIncomingTranscript('two three');
    expect(completed).toEqual([[0, 'words']]);    // completions report how they completed
  });

  it('replayFromStep re-arms from that step of the current turn only', () => {
    const fired: string[] = [];
    const m = new TranscriptSyncMatcher((op) => fired.push(op.opId));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'alpha beta'));
    m.enqueueStep(mkStep('t', 1, 1, 'gamma delta'));
    m.onIncomingTranscript('alpha beta gamma');
    m.replayFromStep('other-turn', 0);
    expect(m.pointer.step).toBe(1);
    m.replayFromStep('t', 1);
    expect(m.pointer.step).toBe(1);
    m.onIncomingTranscript('gamma delta');
    // Re-arming re-fires step 1's op on completion by design; the executor drops it by opId.
    expect(fired).toEqual(['t:0:0', 't:1:0']);
  });

  it('flushRemaining executes and completes all stranded steps immediately', () => {
    const fired: string[] = [];
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(
      (op) => fired.push(op.opId),
      (step) => completed.push(step.stepIndex)
    );
    m.beginTurn('t2');
    m.enqueueStep(mkStep('t2', 1, 0, 'step zero spoken'));
    m.enqueueStep(mkStep('t2', 1, 1, 'step one spoken'));
    m.enqueueStep(mkStep('t2', 1, 2, 'step two spoken'));

    // Turn ended arrived before transcripts finished
    m.flushRemaining('t2');

    expect(fired).toEqual(['t2:0:0', 't2:1:0', 't2:2:0']);
    expect(completed).toEqual([0, 1, 2]);
    expect(m.pointer.step).toBe(3);
  });

  it('matches math synonyms like aaa to triple and 180 to one', () => {
    const fired: string[] = [];
    const m = new TranscriptSyncMatcher((op) => fired.push(op.opId));
    m.beginTurn('t3');
    m.enqueueStep({
      turnId: 't3',
      generation: 1,
      stepIndex: 0,
      spokenText: 'by the aaa similarity criterion',
      words: ['by', 'the', 'aaa', 'similarity', 'criterion'],
      ops: [{ opId: 't3:0:0', kind: 'WRITE' as const, atWord: 3, text: 'w' }],
    });
    m.setSpeaking(true);
    // Transcript uses "triple" for "aaa"
    m.onIncomingTranscript('by the triple');
    expect(fired).toEqual(['t3:0:0']);
  });

  it('guarantees strictly monotonic step execution when steps arrive out of order', () => {
    const fired: string[] = [];
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(
      (op) => fired.push(op.opId),
      (step) => completed.push(step.stepIndex)
    );
    m.beginTurn('t_ooo');
    // Step 0 and 1 arrive first
    m.enqueueStep(mkStep('t_ooo', 1, 0, 'first step zero'));
    m.enqueueStep(mkStep('t_ooo', 1, 1, 'second step one'));

    // Step 0 and 1 complete
    m.onIncomingTranscript('first step zero second step one');
    expect(completed).toEqual([0, 1]);

    // Now Step 4 arrives before Step 2 and Step 3!
    m.enqueueStep(mkStep('t_ooo', 1, 4, 'fifth step four'));

    // Step 4's words are heard, but it MUST NOT execute because step 2 is pending
    m.onIncomingTranscript('fifth step four');
    expect(completed).toEqual([0, 1]);
    expect(fired).not.toContain('t_ooo:4:0');

    // Step 2 arrives and executes
    m.enqueueStep(mkStep('t_ooo', 1, 2, 'third step two'));
    m.onIncomingTranscript('third step two');
    expect(completed).toEqual([0, 1, 2]);
    expect(fired).toContain('t_ooo:2:0');

    // Step 3 arrives and executes
    m.enqueueStep(mkStep('t_ooo', 1, 3, 'fourth step three'));
    m.onIncomingTranscript('fourth step three');
    expect(completed).toEqual([0, 1, 2, 3]);

    // Now Step 4 can execute
    m.onIncomingTranscript('fifth step four');
    expect(completed).toEqual([0, 1, 2, 3, 4]);
    expect(fired).toEqual(['t_ooo:0:0', 't_ooo:1:0', 't_ooo:2:0', 't_ooo:3:0', 't_ooo:4:0']);
  });

  it('seamlessly transitions to next step even if the first word of the next step was skipped or altered', () => {
    const fired: string[] = [];
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(
      (op) => fired.push(op.opId),
      (step) => completed.push(step.stepIndex)
    );
    m.beginTurn('t_seamless');
    m.enqueueStep({
      turnId: 't_seamless',
      generation: 1,
      stepIndex: 0,
      spokenText: 'look at side ab in triangle',
      words: ['look', 'at', 'side', 'ab', 'in', 'triangle'],
      ops: [{ opId: 't_seamless:0:0', kind: 'WRITE' as const, atWord: 3, text: 'w1' }],
    });
    m.enqueueStep({
      turnId: 't_seamless',
      generation: 1,
      stepIndex: 1,
      spokenText: 'now follow side bc to apply bpt',
      words: ['now', 'follow', 'side', 'bc', 'to', 'apply', 'bpt'],
      ops: [{ opId: 't_seamless:1:0', kind: 'WRITE' as const, atWord: 4, text: 'w2' }],
    });

    // Step 0 plays partial words: 'look at side' -> triggers w1
    m.onIncomingTranscript('look at side');
    expect(fired).toEqual(['t_seamless:0:0']);
    expect(completed).toEqual([]);

    // Now speaker begins step 1, but skips 'now' and says 'follow side bc'!
    m.onIncomingTranscript('follow side bc');
    // Step 0 should have completed immediately without stalling!
    expect(completed).toContain(0);
    // Step 1's op atWord 4 (side bc) should fire!
    expect(fired).toContain('t_seamless:1:0');
  });

  it('matches exact names and synonyms only', () => {
    expect(wordsMatch('ab', 'ab')).toBe(true);
    expect(wordsMatch('ab', 'ba')).toBe(false);
    expect(wordsMatch('ca', 'ac')).toBe(false);
    expect(wordsMatch('ab', 'a')).toBe(false);
    expect(wordsMatch('ab', 'b')).toBe(false);
    expect(wordsMatch('bpt', 'ratio')).toBe(true);
    expect(wordsMatch('bpt', 'theorem')).toBe(true);
    expect(wordsMatch('delta', 'triangle')).toBe(true);
  });
});

describe('TranscriptSyncMatcher stall guard', () => {
  const mkStep = (turnId: string, generation: number, stepIndex: number, text: string) => ({
    turnId, generation, stepIndex, spokenText: text,
    words: normalizeWord(text),
    ops: [{ opId: `${turnId}:${stepIndex}:0`, kind: 'WRITE' as const, atWord: 99, text: 'w' }],
  });

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('no_stall_while_silent', () => {
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three four five six'));
    m.onIncomingTranscript('one two');
    m.setSpeaking(false);
    vi.advanceTimersByTime(10000);
    expect(completed).toEqual([]);
  });

  it('stall_only_while_speaking', () => {
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three four five six'));
    m.setSpeaking(true);
    vi.advanceTimersByTime(2600);
    expect(completed).toEqual([0]);
  });

  it('stall_completion_marks_step_started', () => {
    // A step advancing purely by the stall fallback (silent audio / dropped transcript)
    // must still report started so the reveal clock can show the base figure — but never
    // before the step is genuinely stalled.
    const started: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, () => {}, (s) => started.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three four five six'));
    m.setSpeaking(true);
    expect(started).toEqual([]);                      // silent so far: not started
    vi.advanceTimersByTime(STALL_MS_SPEAKING + 100);  // stall fires
    expect(started).toEqual([0]);
  });

  it('hold_freezes_stall', () => {
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three four five six'));
    m.setSpeaking(true);
    m.hold();
    vi.advanceTimersByTime(10000);
    expect(completed).toEqual([]);
    m.release();
    vi.advanceTimersByTime(2600);
    expect(completed).toEqual([0]);
  });

  it('suspend_ignores_words', () => {
    const completed: number[] = [];
    const m = new TranscriptSyncMatcher(() => {}, (s) => completed.push(s.stepIndex));
    m.beginTurn('t');
    m.enqueueStep(mkStep('t', 1, 0, 'one two three four five six'));
    m.setSpeaking(true);
    m.suspend();
    m.onIncomingTranscript('one two three four five six');
    expect(completed).toEqual([]);
    vi.advanceTimersByTime(10000);
    expect(completed).toEqual([]);
    m.resume();
    m.onIncomingTranscript('one two three four five six');
    expect(completed).toEqual([0]);
  });
});

describe('TranscriptWordFeeder', () => {
  it('feeds each word of a cumulative segment exactly once and holds back partial words', () => {
    const words: string[] = [];
    const f = new TranscriptWordFeeder((w) => words.push(w));
    f.pushCumulative('s1', 'so AC is thir', false);
    expect(words).toEqual(['so', 'ac', 'is']);
    f.pushCumulative('s1', 'so AC is thirteen', false);
    f.pushCumulative('s1', 'so AC is thirteen cm.', true);
    expect(words).toEqual(['so', 'ac', 'is', 'thirteen', 'cm']);
  });

  it('assembles text-stream deltas across chunk boundaries', () => {
    const words: string[] = [];
    const f = new TranscriptWordFeeder((w) => words.push(w));
    f.pushDelta('x', 'side-le', false);
    f.pushDelta('x', 'ngth is 3.', false);
    f.pushDelta('x', '5', false);
    f.pushDelta('x', ' ', true);
    expect(words).toEqual(['side', 'length', 'is', '3', '5']);
  });
});

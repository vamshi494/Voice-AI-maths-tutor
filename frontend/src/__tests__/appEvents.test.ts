// frontend/src/__tests__/appEvents.test.ts
// The extracted App event router, tested without a browser room.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EpochDedupBook } from '../App';
import { routeTutorEvent, RouterDeps } from '../app/eventRouter';
import { continueLesson, changeSpeed, callRpcWithRetry, setHold, uploadPhoto } from '../app/actions';
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TranscriptSyncMatcher, TranscriptWordFeeder, STALL_MS_SPEAKING } from '../whiteboard/transcriptSync';

function makeDeps() {
  const refs = { currentTurnId: { current: '' }, currentGeneration: { current: 0 } };
  const executor = {
    setGeneration: vi.fn(),
    clearBoard: vi.fn(),
    pageRestore: vi.fn(),
    commitDiagram: vi.fn(),
    commitPage: vi.fn(),
    revealAll: vi.fn(),
    executeOp: vi.fn(),
    clearFocus: vi.fn(),
    reportFinal: vi.fn(),
  } as unknown as CommandExecutor;
  const matcher = {
    beginTurn: vi.fn(),
    enqueueStep: vi.fn(),
    replayFromStep: vi.fn(),
    turnCancelled: vi.fn(),
    flushRemaining: vi.fn(),
    suspend: vi.fn(),
    resume: vi.fn(),
  } as unknown as TranscriptSyncMatcher;
  const layout = { reset: vi.fn(), setLayoutMode: vi.fn() } as unknown as BoardLayout;
  const feeder = { reset: vi.fn() } as unknown as TranscriptWordFeeder;
  const ui = {
    setThinking: vi.fn(),
    setLocked: vi.fn(),
    setHasDrawn: vi.fn(),
    setCanContinue: vi.fn(),
    showNotice: vi.fn(),
    setDraft: vi.fn(),
    showSessionEnded: vi.fn(),
    setLessonPlan: vi.fn(),
    setCurrentPage: vi.fn(),
  };
  const deps: RouterDeps = { executor, matcher, layout, feeder, refs, ui };
  return { refs, executor, matcher, layout, feeder, ui, deps };
}

describe('routeTutorEvent', () => {
  it('stale_generation_dropped', () => {
    const { deps, refs, matcher, executor } = makeDeps();
    refs.currentGeneration.current = 5;
    routeTutorEvent(
      { type: 'step', generation: 4, step: { turnId: 't1', generation: 4, stepIndex: 0, spokenText: 'x', words: ['x'], ops: [] } },
      deps,
    );
    expect(matcher.enqueueStep).not.toHaveBeenCalled();
    expect(executor.executeOp).not.toHaveBeenCalled();
    expect(executor.setGeneration).not.toHaveBeenCalled();
  });

  it('turn_started_new_page_clears_board', () => {
    const { deps, refs, matcher, executor, layout, feeder, ui } = makeDeps();
    routeTutorEvent(
      { type: 'turn_started', generation: 2, turnId: 't2', kind: 'lesson', pageId: 'p2', newPage: true },
      deps,
    );
    expect(refs.currentTurnId.current).toBe('t2');
    expect(refs.currentGeneration.current).toBe(2);
    expect(executor.setGeneration).toHaveBeenCalledWith(2);
    expect(matcher.beginTurn).toHaveBeenCalledWith('t2');
    expect(feeder.reset).toHaveBeenCalled();
    expect(executor.clearBoard).toHaveBeenCalledWith(true);
    expect(layout.reset).toHaveBeenCalledWith('p2');
    expect(ui.setHasDrawn).toHaveBeenCalledWith(false);
  });

  it('step_of_other_turn_dropped', () => {
    const { deps, refs, matcher } = makeDeps();
    refs.currentTurnId.current = 't_current';
    refs.currentGeneration.current = 1;
    routeTutorEvent(
      { type: 'step', generation: 1, step: { turnId: 't_other', generation: 1, stepIndex: 0, spokenText: 'a', words: ['a'], ops: [] } },
      deps,
    );
    expect(matcher.enqueueStep).not.toHaveBeenCalled();
    routeTutorEvent(
      { type: 'step', generation: 1, step: { turnId: 't_current', generation: 1, stepIndex: 1, spokenText: 'b', words: ['b'], ops: [] } },
      deps,
    );
    expect(matcher.enqueueStep).toHaveBeenCalledTimes(1);
  });

  it('turn_ended_flushes', () => {
    const { deps, matcher } = makeDeps();
    routeTutorEvent(
      { type: 'turn_ended', generation: 1, turnId: 't1', status: 'complete', visualStatus: 'validated' },
      deps,
    );
    expect(matcher.flushRemaining).toHaveBeenCalledWith('t1');
  });

  it('turn_ended_partial_does_not_flush', () => {
    const { deps, matcher } = makeDeps();
    routeTutorEvent(
      { type: 'turn_ended', generation: 1, turnId: 't1', status: 'partial', visualStatus: 'text_only' },
      deps,
    );
    expect(matcher.flushRemaining).not.toHaveBeenCalled();
    expect(matcher.turnCancelled).not.toHaveBeenCalled();
  });

  it('page_only_turn_end_does_not_flush', () => {
    // A chapter page boundary must not dump the page's un-heard steps; the next page's
    // steps arrive on their own.
    const { deps, matcher } = makeDeps();
    routeTutorEvent(
      { type: 'turn_ended', generation: 1, turnId: 't1', status: 'complete',
        visualStatus: 'validated', pageOnly: true },
      deps,
    );
    expect(matcher.flushRemaining).not.toHaveBeenCalled();
  });

  it('snapshot_restores_last_subpage', () => {
    const { deps, executor } = makeDeps();
    routeTutorEvent(
      {
        type: 'board_snapshot',
        generation: 3,
        current: {
          pageId: 'root',
          subPages: [
            { subId: 'root', ops: [{ opId: 't1:0:0', kind: 'WRITE', atWord: 0, text: 'old', rowId: 'w1' }] },
            { subId: 'root_p2', ops: [{ opId: 't1:1:0', kind: 'WRITE', atWord: 0, text: 'new', rowId: 'w2' }] },
          ],
        },
        stack: [],
        pendingSteps: [],
        canContinueLesson: false,
      },
      deps,
    );
    expect(executor.pageRestore).toHaveBeenCalledWith(
      'root_p2', null, [expect.objectContaining({ opId: 't1:1:0' })], 3,
    );
  });

  it('snapshot_restores_active_turn', () => {
    const { deps, refs, matcher, ui } = makeDeps();
    routeTutorEvent(
      {
        type: 'board_snapshot',
        generation: 4,
        current: { pageId: 'p1', subPages: [{ subId: 'p1', ops: [] }] },
        stack: [],
        canContinueLesson: true,
        activeTurn: { type: 'turn_started', generation: 4, turnId: 't9', kind: 'lesson', pageId: 'p1', newPage: false },
        pendingSteps: [{ turnId: 't9', generation: 4, stepIndex: 0, spokenText: 'x', words: ['x'], ops: [] }],
      },
      deps,
    );
    expect(refs.currentTurnId.current).toBe('t9');
    expect(matcher.beginTurn).toHaveBeenCalledWith('t9');
    expect(matcher.enqueueStep).toHaveBeenCalledTimes(1);
    expect(ui.setCanContinue).toHaveBeenCalledWith(true);
  });
});

describe('RPC acks', () => {
  const makeUi = (currentSpeed: 0.8 | 1.0 | 1.2 = 1.0) => ({
    unblockAudio: vi.fn(),
    setThinking: vi.fn(),
    setCanContinue: vi.fn(),
    setCurrentSpeed: vi.fn(),
    getCurrentSpeed: () => currentSpeed,
    setOcrProcessing: vi.fn(),
    setDraft: vi.fn(),
    showNotice: vi.fn(),
  });

  it('continue_rejected_clears_spinner', async () => {
    const ui = makeUi();
    const rpc = vi.fn().mockResolvedValue({ ok: false, reason: 'not_now' });
    await continueLesson(rpc, ui);
    expect(ui.setThinking).toHaveBeenCalledWith(true);
    expect(ui.setThinking).toHaveBeenLastCalledWith(false);
    expect(ui.setCanContinue).toHaveBeenCalledWith(false);
    expect(ui.showNotice).toHaveBeenCalledWith('Let me finish this first.');
  });

  it('speed_rejected_reverts', async () => {
    const ui = makeUi(1.0);
    const rpc = vi.fn().mockResolvedValue({ ok: false, reason: 'speed_unsupported' });
    await changeSpeed(rpc, ui, 1.2);
    expect(ui.setCurrentSpeed).toHaveBeenNthCalledWith(1, 1.2);
    expect(ui.setCurrentSpeed).toHaveBeenLastCalledWith(1.0);
    expect(ui.showNotice).toHaveBeenCalledWith("Speed can't be changed for this voice.");
  });

  it('rpc_retried_once_on_throw', async () => {
    vi.useFakeTimers();
    const rpc = vi.fn()
      .mockRejectedValueOnce(new Error('timeout'))
      .mockResolvedValueOnce({ ok: true });
    const promise = callRpcWithRetry(rpc, 'continue_lesson');
    await vi.advanceTimersByTimeAsync(500);
    await expect(promise).resolves.toEqual({ ok: true });
    expect(rpc).toHaveBeenCalledTimes(2);

    // ok:false is an answer, never retried
    const noRetry = vi.fn().mockResolvedValue({ ok: false, reason: 'not_now' });
    await callRpcWithRetry(noRetry, 'continue_lesson');
    expect(noRetry).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});

describe('asides', () => {
  it('aside_suspends_matcher', () => {
    const { deps } = makeDeps();
    const matcher = new TranscriptSyncMatcher(() => {});
    deps.matcher = matcher;
    matcher.beginTurn('t1');
    matcher.enqueueStep({
      turnId: 't1', generation: 1, stepIndex: 0, spokenText: 'one two',
      words: ['one', 'two'], ops: [],
    });
    matcher.setSpeaking(true);

    routeTutorEvent({ type: 'aside', generation: 1, phase: 'start', kind: 'greeting' }, deps);
    matcher.onIncomingTranscript('one two');      // ignored inside the aside
    expect(matcher.pointer.step).toBe(0);

    routeTutorEvent({ type: 'aside', generation: 1, phase: 'end', kind: 'greeting' }, deps);
    matcher.onIncomingTranscript('one two');
    expect(matcher.pointer.step).toBe(1);
  });

  it('stale_aside_end_not_dropped', () => {
    const { deps, refs, matcher } = makeDeps();
    refs.currentGeneration.current = 5;
    routeTutorEvent({ type: 'aside', generation: 4, phase: 'end', kind: 'bridge' }, deps);
    expect(matcher.resume).toHaveBeenCalled();
  });
});

describe('epoch de-dup', () => {
  it('new_epoch_not_dropped', () => {
    const book = new EpochDedupBook();
    expect(book.duplicate(57, 'aaaa1111')).toBe(false);
    expect(book.duplicate(57, 'aaaa1111')).toBe(true);    // duplicate within the epoch
    expect(book.duplicate(1, 'bbbb2222')).toBe(false);    // new epoch clears the book
    expect(book.duplicate(2, 'bbbb2222')).toBe(false);
    expect(book.duplicate(1, 'bbbb2222')).toBe(true);
  });
});

describe('photo upload', () => {
  it('photo_http_failure_shows_error', async () => {
    const publish = vi.fn();
    const fetchFn = vi.fn().mockRejectedValue(new Error('offline'));
    const result = await uploadPhoto(fetchFn, new Blob(['x']), publish);
    expect(result?.success).toBe(false);
    expect(result?.message).toBe('Upload failed. Please check your connection and try again.');
    expect(publish).not.toHaveBeenCalled();
  });
});

describe('session ended', () => {
  it('session_ended_shows_modal', () => {
    const { deps, ui } = makeDeps();
    deps.refs.currentGeneration.current = 9;   // older generation: GEN_EXEMPT must still deliver
    routeTutorEvent({ type: 'session_ended', generation: 8, reason: 'opened_elsewhere' }, deps);
    expect(ui.showSessionEnded).toHaveBeenCalledWith('opened_elsewhere');
    expect(ui.setThinking).toHaveBeenCalledWith(false);
    expect(ui.setLocked).toHaveBeenCalledWith(false);
  });
});

describe('marker hold', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('marker_hold_blocks_stall', () => {
    const { deps, executor } = makeDeps();
    const matcher = new TranscriptSyncMatcher((op) => (executor.executeOp as any)(op, 1, 't1'));
    deps.matcher = matcher;

    routeTutorEvent(
      { type: 'turn_started', generation: 1, turnId: 't1', kind: 'lesson', pageId: 'p1', newPage: true },
      deps,
    );
    routeTutorEvent(
      {
        type: 'step',
        generation: 1,
        step: {
          turnId: 't1',
          generation: 1,
          stepIndex: 0,
          spokenText: 'one two three four five six',
          words: ['one', 'two', 'three', 'four', 'five', 'six'],
          ops: [{ opId: 't1:0:0', kind: 'WRITE', atWord: 5, text: 'w' }],
        },
      },
      deps,
    );
    matcher.setSpeaking(true);
    matcher.hold();                       // App.setMarkerArmed(true)
    vi.advanceTimersByTime(STALL_MS_SPEAKING + 1000);
    expect(executor.executeOp).not.toHaveBeenCalled();

    matcher.release();                    // App.setMarkerArmed(false)
    vi.advanceTimersByTime(STALL_MS_SPEAKING + 100);
    expect(executor.executeOp).toHaveBeenCalledWith(expect.objectContaining({ opId: 't1:0:0' }), 1, 't1');
  });
});

describe('hold reasons', () => {
  it('clear_marks_keeps_user_pause', async () => {
    const registry = new Set<string>();
    const matcher = { hold: vi.fn(), release: vi.fn() };
    const setIsPaused = vi.fn();
    const rpc = vi.fn().mockResolvedValue({ ok: true });
    const deps = { rpc, registry, matcher, setIsPaused };

    await setHold(deps, 'user_pause', true);
    await setHold(deps, 'marker', true);
    expect(matcher.hold).toHaveBeenCalledTimes(1);
    expect(rpc).toHaveBeenLastCalledWith('hold', { reason: 'marker' });

    await setHold(deps, 'marker', false);                 // Clear marks
    expect(registry.has('user_pause')).toBe(true);
    expect(matcher.release).not.toHaveBeenCalled();
    expect(setIsPaused).toHaveBeenLastCalledWith(true);

    await setHold(deps, 'user_pause', false);
    expect(matcher.release).toHaveBeenCalledTimes(1);
    expect(setIsPaused).toHaveBeenLastCalledWith(false);
    expect(rpc).toHaveBeenLastCalledWith('release', { reason: 'user_pause' });
  });
});


describe('routeTutorEvent page_commit / reveal triggers', () => {
  it('page_commit_reference_stages_not_instant', () => {
    const { deps, refs, executor } = makeDeps();
    refs.currentTurnId.current = 't1';
    const evt = { type: 'page_commit', generation: 1, turnId: 't1', pageId: 'p1', commitId: 'c1',
                  workRect: { x: 40, y: 72, width: 340, height: 608 }, blocks: [], reference: true };
    routeTutorEvent(evt, deps);
    // A reference figure reveals at stage time via the clock — never via instant=true.
    expect(executor.commitPage).toHaveBeenCalledWith(evt, false);
  });

  it('turn_ended_reveals_cancelled_does_not', () => {
    const { deps, refs, executor } = makeDeps();
    refs.currentTurnId.current = 't1';
    routeTutorEvent({ type: 'turn_ended', generation: 1, turnId: 't1', status: 'complete', visualStatus: 'validated' }, deps);
    expect(executor.revealAll).toHaveBeenCalledTimes(1);

    const { deps: deps2, refs: refs2, executor: executor2 } = makeDeps();
    refs2.currentTurnId.current = 't1';
    routeTutorEvent({ type: 'turn_cancelled', generation: 1, turnId: 't1', keepBoard: true }, deps2);
    expect(executor2.revealAll).not.toHaveBeenCalled();
  });
});

describe('lesson plan chips', () => {
  const pages = [
    { pageId: 'L_1a2b_p0', index: 0, title: 'What BPT says' },
    { pageId: 'L_1a2b_p1', index: 1, title: 'Proof' },
  ];

  it('lesson_plan_shows_progress', () => {
    const { deps, ui } = makeDeps();
    routeTutorEvent(
      { type: 'lesson_plan', generation: 2, lessonId: 'L_1a2b', title: 'BPT', pages },
      deps,
    );
    expect(ui.setLessonPlan).toHaveBeenCalledWith(pages);

    routeTutorEvent(
      { type: 'turn_started', generation: 2, turnId: 't1', kind: 'lesson',
        pageId: 'L_1a2b_p1', newPage: false, pageIndex: 1, pageTitle: 'Proof' },
      deps,
    );
    expect(ui.setCurrentPage).toHaveBeenCalledWith(1);
  });

  it('lesson_plan_stale_generation_dropped', () => {
    // A superseded lesson's plan must never update the chips.
    const { deps, ui, refs } = makeDeps();
    refs.currentGeneration.current = 5;
    routeTutorEvent(
      { type: 'lesson_plan', generation: 4, lessonId: 'L_old', title: 'old', pages },
      deps,
    );
    expect(ui.setLessonPlan).not.toHaveBeenCalled();
  });
});

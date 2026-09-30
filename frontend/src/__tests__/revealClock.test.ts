// frontend/src/__tests__/revealClock.test.ts
// Figure groups reveal when the speech reaches them. The base
// group waits for the first heard word of the turn; a reveal group reveals at the
// start of the first step that FOCUSes it; unrevealed groups reveal on the last step or on
// revealAll (turn end); reference blocks reveal everything at stage time.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Konva from 'konva';
import gsap from 'gsap';
import { RevealClock, StagedGroup } from '../whiteboard/revealClock';
import { BoardOp, Step } from '../types/events';

const group = () => new Konva.Group({ opacity: 0 });

/** gsap's ticker does not run under vitest fake timers; force every active tween to its end. */
const settle = () => gsap.globalTimeline.progress(1);

const step = (ops: BoardOp[], over: Partial<Step> = {}): Step => ({
  turnId: 't1',
  generation: 1,
  stepIndex: 0,
  spokenText: 'x',
  words: ['x'],
  ops,
  ...over,
});

const focus = (entityId: string): BoardOp => ({ opId: 't:0:0', kind: 'FOCUS', atWord: 0, entityId });

describe('RevealClock', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const stageBasic = (clock: RevealClock, reference = false) => {
    const base = group();
    const rgA = group();
    const groups: StagedGroup[] = [
      { targetId: 'base', nodes: base },
      { targetId: 'rg_a', nodes: rgA },
    ];
    clock.stage('fig1', groups, reference);
    return { base, rgA };
  };

  it('base_figure_waits_for_first_word', () => {
    const clock = new RevealClock();
    const { base, rgA } = stageBasic(clock);
    expect(base.opacity()).toBe(0);
    expect(rgA.opacity()).toBe(0);

    clock.onStepStarted(step([]), false);
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgA.opacity()).toBe(0);   // nothing FOCUSed it yet
  });

  it('group_reveals_on_focus_step', () => {
    const clock = new RevealClock();
    const { base, rgA } = stageBasic(clock);
    const rgB = group();
    clock.stage('fig1', [{ targetId: 'base', nodes: base }, { targetId: 'rg_a', nodes: rgA },
                          { targetId: 'rg_b', nodes: rgB }], false);

    clock.onStepStarted(step([focus('rg_a')]), false);
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgA.opacity()).toBe(1);
    expect(rgB.opacity()).toBe(0);
  });

  it('group_reveals_when_member_entity_focused', () => {
    const clock = new RevealClock();
    const base = group();
    const rgParallel = group();
    clock.stage('fig1', [
      { targetId: 'base', nodes: base },
      { targetId: 'd1_group_parallel', nodes: rgParallel, entityIds: new Set(['d1_point_D', 'd1_seg_DE']) },
    ], false);

    // Tutor focuses point D; rgParallel should reveal even though targetId d1_group_parallel was not explicitly used
    clock.onStepStarted(step([focus('d1_point_D')]), false);
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgParallel.opacity()).toBe(1);
  });

  it('unreferenced_groups_reveal_on_last_step', () => {
    const clock = new RevealClock();
    const { base, rgA } = stageBasic(clock);
    clock.onStepStarted(step([]), false);
    clock.onStepStarted(step([], { stepIndex: 1 }), true);   // isLast
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgA.opacity()).toBe(1);

    // revealAll covers a turn that ended without an isLast step.
    const clock2 = new RevealClock();
    const { base: b2, rgA: r2 } = stageBasic(clock2);
    clock2.revealAll();
    settle();
    expect(b2.opacity()).toBe(1);
    expect(r2.opacity()).toBe(1);
  });

  it('reference_and_restore_reveal_all', () => {
    const clock = new RevealClock();
    const { base, rgA } = stageBasic(clock, true);
    expect(base.opacity()).toBe(0);   // the tween starts at opacity 0
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgA.opacity()).toBe(1);

    // reset() forgets staged groups: a stale reveal must never touch a new board.
    const clock2 = new RevealClock();
    const { base: b2 } = stageBasic(clock2);
    clock2.reset();
    clock2.onStepStarted(step([]), false);
    settle();
    expect(b2.opacity()).toBe(0);
  });

  it('stage_after_turn_started_reveals_base', () => {
    // A late page_commit (arriving after the turn's first step started) must still show
    // the base figure — otherwise it stays hidden until turn end.
    const clock = new RevealClock();
    clock.onStepStarted(step([]), false);   // turn already speaking; nothing staged yet
    const { base, rgA } = stageBasic(clock);
    settle();
    expect(base.opacity()).toBe(1);
    expect(rgA.opacity()).toBe(0);          // reveal groups still wait for their FOCUS step
  });

  it('reset_kills_inflight_reveal_tweens', () => {
    // reset must kill in-flight fade tweens so a cleared board never keeps animating.
    const clock = new RevealClock();
    const { base } = stageBasic(clock);
    clock.onStepStarted(step([]), false);   // base fade (0.4 s) now running
    expect(gsap.getTweensOf(base).length).toBe(1);
    clock.reset();
    expect(gsap.getTweensOf(base).length).toBe(0);
    settle();
    expect(base.opacity()).toBe(0);         // the killed tween never completes
  });
});

// frontend/src/__tests__/audioHealth.test.ts
// Autoplay-blocked holds and releases.
import { describe, it, expect, vi } from 'vitest';
import { AudioHealth } from '../audio/audioHealth';

class FakeRoom {
  public canPlaybackAudio: boolean;
  public listeners: Array<() => void> = [];
  constructor(canPlayback: boolean) {
    this.canPlaybackAudio = canPlayback;
  }
  on(_event: string, listener: () => void) {
    this.listeners.push(listener);
  }
  async startAudio() {
    this.canPlaybackAudio = true;
  }
  public fireStatus() {
    for (const l of this.listeners) l();
  }
}

function make(canPlayback: boolean) {
  const room = new FakeRoom(canPlayback);
  const rpc = vi.fn().mockResolvedValue({ ok: true });
  const matcher = { hold: vi.fn(), release: vi.fn() };
  const setAudioMode = vi.fn();
  const health = new AudioHealth(room as any, rpc, matcher, { setAudioMode });
  return { room, rpc, matcher, setAudioMode, health };
}

describe('AudioHealth', () => {
  it('blocked_sends_hold', () => {
    const { rpc, matcher, setAudioMode } = make(false);
    expect(rpc).toHaveBeenCalledWith('hold', { reason: 'audio_blocked' });
    expect(matcher.hold).toHaveBeenCalledTimes(1);
    expect(setAudioMode).toHaveBeenLastCalledWith('blocked');
  });

  it('blocked_mid_turn_holds', () => {
    const { room, rpc, matcher, setAudioMode } = make(true);
    expect(rpc).not.toHaveBeenCalled();

    room.canPlaybackAudio = false;
    room.fireStatus();

    expect(rpc).toHaveBeenCalledWith('hold', { reason: 'audio_blocked' });
    expect(matcher.hold).toHaveBeenCalledTimes(1);
    expect(setAudioMode).toHaveBeenLastCalledWith('blocked');
  });

  it('unblock_releases', async () => {
    const { rpc, matcher, setAudioMode, health } = make(false);

    await health.unblock();

    expect(rpc).toHaveBeenLastCalledWith('release', { reason: 'audio_blocked' });
    expect(matcher.release).toHaveBeenCalledTimes(1);
    expect(setAudioMode).toHaveBeenLastCalledWith('voice');
  });
});

import { CaptionClock } from '../audio/captionClock';
import { TranscriptSyncMatcher } from '../whiteboard/transcriptSync';
import { routeTutorEvent, RouterDeps } from '../app/eventRouter';

describe('CaptionClock', () => {
  it('caption_clock_paces_steps', () => {
    vi.useFakeTimers();
    const completed: Array<[number, string]> = [];
    const matcher = new TranscriptSyncMatcher(() => {}, (s, by) => completed.push([s.stepIndex, by]));
    matcher.beginTurn('t');
    const step = {
      turnId: 't', generation: 1, stepIndex: 0, spokenText: 'one two three',
      words: ['one', 'two', 'three'], ops: [],
    };
    matcher.enqueueStep(step);

    const clock = new CaptionClock(matcher, 2.6);
    clock.start(step, 1.0);
    vi.advanceTimersByTime((1000 / 2.6) * 3 + 50);

    expect(completed).toEqual([[0, 'caption']]);
    expect(clock.running).toBe(false);
    vi.useRealTimers();
  });

  it('captions_no_flush_dump', () => {
    const matcher = {
      beginTurn: vi.fn(), enqueueStep: vi.fn(), flushRemaining: vi.fn(),
      turnCancelled: vi.fn(), suspend: vi.fn(), resume: vi.fn(), replayFromStep: vi.fn(),
    };
    const executor = {
      setGeneration: vi.fn(), clearBoard: vi.fn(), pageRestore: vi.fn(), commitDiagram: vi.fn(),
      executeOp: vi.fn(), clearFocus: vi.fn(), reportFinal: vi.fn(), revealAll: vi.fn(),
    };
    const ui = {
      setThinking: vi.fn(), setLocked: vi.fn(), setHasDrawn: vi.fn(), setCanContinue: vi.fn(),
      showNotice: vi.fn(), setDraft: vi.fn(), setAudioMode: vi.fn(), onCaptionStep: vi.fn(),
    };
    const deps = {
      executor, matcher, layout: null, feeder: null,
      refs: { currentTurnId: { current: 't1' }, currentGeneration: { current: 1 } },
      getAudioMode: () => 'captions' as const,
      ui,
    } as unknown as RouterDeps;

    routeTutorEvent(
      { type: 'turn_ended', generation: 1, turnId: 't1', status: 'complete', visualStatus: 'validated' },
      deps,
    );

    expect(matcher.flushRemaining).not.toHaveBeenCalled();
    expect(executor.reportFinal).toHaveBeenCalled();
  });
});

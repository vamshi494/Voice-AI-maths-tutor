// frontend/src/audio/captionClock.ts
// In captions mode the client's caption clock is the audio clock.
import { Step } from '../types/events';
import { TranscriptSyncMatcher } from '../whiteboard/transcriptSync';

export const CAPTION_WPS = 2.6;

export class CaptionClock {
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private matcher: TranscriptSyncMatcher, private wps: number = CAPTION_WPS) {}

  public get running(): boolean {
    return this.timer !== null;
  }

  /** Feed the step's words one by one at reading speed; completions report `caption`. */
  public start(step: Step, speed: number): void {
    this.stop();
    const words = Array.isArray(step.words) ? step.words : [];
    const interval = 1000 / (this.wps * Math.max(0.5, speed));
    let i = 0;
    const tick = () => {
      if (i >= words.length) {
        this.timer = null;
        return;
      }
      this.matcher.feedCaptionWord(words[i]);
      i += 1;
      this.timer = setTimeout(tick, interval);
    };
    tick();
  }

  public stop(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

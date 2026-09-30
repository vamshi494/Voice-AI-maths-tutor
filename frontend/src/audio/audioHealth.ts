// frontend/src/audio/audioHealth.ts
// Autoplay blocking holds the tutor until the student taps.
import { RoomEvent } from 'livekit-client';

export type AudioMode = 'voice' | 'blocked' | 'captions';

export interface AudioRoom {
  canPlaybackAudio: boolean;
  startAudio(): Promise<void>;
  on(event: string, listener: (...args: any[]) => void): unknown;
}

export interface RpcAckLike {
  ok: boolean;
}

export interface AudioMatcher {
  hold(): void;
  release(): void;
}

export interface AudioHealthUi {
  setAudioMode(mode: AudioMode): void;
}

export class AudioHealth {
  private blocked = false;

  constructor(
    private room: AudioRoom,
    private rpc: (method: string, payload: any) => Promise<RpcAckLike | null>,
    private matcher: AudioMatcher,
    private ui: AudioHealthUi,
  ) {
    this.syncBlocked();
    this.room.on(RoomEvent.AudioPlaybackStatusChanged, () => this.syncBlocked());
  }

  private syncBlocked(): void {
    if (!this.room.canPlaybackAudio) {
      if (this.blocked) return;
      this.blocked = true;
      this.ui.setAudioMode('blocked');
      this.matcher.hold();
      void this.rpc('hold', { reason: 'audio_blocked' });
    } else if (this.blocked) {
      this.blocked = false;
      this.ui.setAudioMode('voice');
      this.matcher.release();
      void this.rpc('release', { reason: 'audio_blocked' });
    }
  }

  /** Server audio status: captions wins; a voice status never clears a local block. */
  public onServerAudioStatus(mode: 'voice' | 'captions'): void {
    if (mode === 'captions') {
      this.ui.setAudioMode('captions');
      return;
    }
    if (!this.blocked) this.ui.setAudioMode('voice');
  }

  /** User gesture: start audio, then release the hold if playback is possible now. */
  public async unblock(): Promise<void> {
    await this.room.startAudio();
    this.syncBlocked();
  }
}

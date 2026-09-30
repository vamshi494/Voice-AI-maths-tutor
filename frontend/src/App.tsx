// frontend/src/App.tsx
import React, { useState, useRef, useCallback, useEffect } from 'react';
import { Room, RoomEvent, Track, RemoteTrack, DataPacket_Kind, RemoteParticipant, Participant } from 'livekit-client';
import { Whiteboard } from './whiteboard/Whiteboard';
import { InputBar } from './components/InputBar';
import { AudioWaveform } from './components/AudioWaveform';
import { PhotoUploadModal } from './components/PhotoUploadModal';
import { CommandExecutor } from './whiteboard/commandExecutor';
import { BoardLayout } from './whiteboard/boardLayout';
import { DoubtMark, PageHeader, RpcAck, RpcHoldReason, Step, TutorWireEvent } from './types/events';
import { TranscriptSyncMatcher, TranscriptWordFeeder } from './whiteboard/transcriptSync';
import { isTutorTranscriptSender } from './app/transcriptSource';
import { EventSequencer } from './whiteboard/eventSequencer';
import { AudioHealth, AudioMode } from './audio/audioHealth';
import { CaptionClock } from './audio/captionClock';
import { CaptionStrip } from './components/CaptionStrip';
import { LessonProgress } from './components/LessonProgress';
import { NotesDrawer } from './components/NotesDrawer';
import { routeTutorEvent } from './app/eventRouter';
import { continueLesson, changeSpeed, setHold, uploadPhoto } from './app/actions';
import { AlertCircle, Wifi, WifiOff, Volume2, VolumeX, Sparkles, PlusCircle, RefreshCw, User, LogOut } from 'lucide-react';
import { requestNewBoard, takeNewBoardRequest } from './auth/session';

import { API_BASE, LIVEKIT_URL } from './app/endpoints';

/** Event de-duplication book keyed by epoch: a new epoch clears the
 *  seen set, so a restarted agent's seq-1 events are not mistaken for duplicates. */
export class EpochDedupBook {
  private epoch: string | null = null;
  private seen = new Set<number>();

  /** True when this event is a duplicate inside its epoch. */
  public duplicate(seq: number | undefined, epoch?: string | null): boolean {
    if (typeof seq !== 'number') return false;      // legacy event without a book
    const ep = epoch ?? null;
    if (this.epoch !== ep) {
      this.epoch = ep;
      this.seen.clear();
    }
    if (this.seen.has(seq)) return true;
    this.seen.add(seq);
    if (this.seen.size > 2000) this.seen = new Set(Array.from(this.seen).slice(-1000));
    return false;
  }
}

const STARTER_TOPICS = [
  {
    icon: '📐',
    title: 'Similar Triangles (Class 10)',
    prompt: 'Can you explain the AAA similarity criterion for two triangles with diagrams and proof?',
    desc: 'Criteria for similarity & side-by-side triangle proofs',
  },
  {
    icon: '⭕',
    title: 'Circles & Tangents (Class 10)',
    prompt: 'Prove that the tangent at any point of a circle is perpendicular to the radius through the point of contact.',
    desc: 'Theorem 10.1 geometry proof with figure',
  },
  {
    icon: '📊',
    title: 'Quadratic Equations (Class 10)',
    prompt: 'Solve 2x^2 - 5x + 3 = 0 using the quadratic formula and show step-by-step working.',
    desc: 'Discriminant, roots, and structured column derivation',
  },
  {
    icon: '📏',
    title: 'Surface Areas & Volumes (Class 9/10)',
    prompt: 'Find the total surface area and volume of a cylinder of radius 7 cm and height 10 cm.',
    desc: 'Formula breakdown with step-by-step substitution',
  },
];

export interface AppProps {
  userName?: string;
  onLogout?(): void;
}

export const App: React.FC<AppProps> = ({ userName = '', onLogout }) => {
  const [isLocked, setIsLocked] = useState(false);
  const [hasMarks, setHasMarks] = useState(false);
  const [currentSpeed, setCurrentSpeed] = useState<0.8 | 1.0 | 1.2>(1.0);
  const [isPaused, setIsPaused] = useState(false);
  const [canContinueLesson, setCanContinueLesson] = useState(false);
  const [isPhotoModalOpen, setIsPhotoModalOpen] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'error' | 'disconnected'>('connecting');
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draftText, setDraftText] = useState<string | null>(null);
  const [hasDrawnContent, setHasDrawnContent] = useState<boolean>(false);
  const [isThinking, setIsThinking] = useState<boolean>(false);
  const [isOcrProcessing, setIsOcrProcessing] = useState(false);
  const [isAudioBlocked, setIsAudioBlocked] = useState<boolean>(false);
  const [audioMode, setAudioMode] = useState<AudioMode>('voice');
  const [captionText, setCaptionText] = useState<string | null>(null);
  const [sessionEndedReason, setSessionEndedReason] = useState<string | null>(null);
  const [lessonPages, setLessonPages] = useState<PageHeader[]>([]);
  const [lessonPageIndex, setLessonPageIndex] = useState<number | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const [activeBoardId, setActiveBoardId] = useState<string>('');
  // Student mic preference survives reconnects AND reloads (LiveKit re-publishes tracks on a
  // full reconnection; we re-apply the preference instead of trusting the default "on").
  const [isMicMuted, setIsMicMuted] = useState<boolean>(() => {
    try {
      return localStorage.getItem('tutor.micMuted') === '1';
    } catch {
      return false;
    }
  });
  const micMutedRef = useRef(isMicMuted);
  const [clearMarksSignal, setClearMarksSignal] = useState(0);
  const holdsRef = useRef<Set<string>>(new Set());
  const feederRef = useRef<TranscriptWordFeeder | null>(null);
  const sequencerRef = useRef<EventSequencer | null>(null);
  const streamTranscriptsRef = useRef(false);
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const roomRef = useRef<Room | null>(null);
  const executorRef = useRef<CommandExecutor | null>(null);
  const layoutRef = useRef<BoardLayout | null>(null);
  const marksRef = useRef<DoubtMark[]>([]);
  const audioHealthRef = useRef<AudioHealth | null>(null);
  const audioModeRef = useRef<'voice' | 'captions'>('voice');
  const captionClockRef = useRef<CaptionClock | null>(null);
  const captionQueueRef = useRef<Step[]>([]);
  const startNextCaptionRef = useRef<() => void>(() => {});
  const currentSpeedRef = useRef<0.8 | 1.0 | 1.2>(1.0);
  const syncMatcherRef = useRef<TranscriptSyncMatcher | null>(null);
  const currentTurnIdRef = useRef<string>('');
  const currentGenerationRef = useRef<number>(0);

  const handleExecutorReady = useCallback((executor: CommandExecutor, layout: BoardLayout) => {
    executorRef.current = executor;
    layoutRef.current = layout;
    sequencerRef.current?.setReady(true);      // nothing is delivered before the board is ready
    // Ops fire with THEIR step's generation, so a superseded turn can never draw on the board.
    // The matcher reports started/completed for the cumulative step_progress stream.
    syncMatcherRef.current = new TranscriptSyncMatcher(
      (op, generation, turnId) => executor.executeOp(op, generation, turnId),
      (step, by) => {
        executor.reportCompleted(step, by);
        if (audioModeRef.current === 'captions' && by === 'caption') {
          startNextCaptionRef.current();      // step k+1 starts when k completes
        }
      },
      (step) => {
        executor.reportStarted(step);
        if (audioModeRef.current === 'captions') setCaptionText(step.spokenText);
      },
    );
    captionClockRef.current = new CaptionClock(syncMatcherRef.current);
    feederRef.current = new TranscriptWordFeeder((w) => {
      syncMatcherRef.current?.onIncomingTranscript(w);
      // The chalk moves at the tutor's measured speaking tempo.
      if (syncMatcherRef.current) executor.setMsPerWord(syncMatcherRef.current.msPerWord);
    });
    // Autoplay blocking holds the tutor until the student taps the banner.
    const room = roomRef.current;
    if (room && syncMatcherRef.current) {
      audioHealthRef.current = new AudioHealth(
        room as any, callAgentRpc, syncMatcherRef.current, { setAudioMode: applyAudioMode },
      );
    }
  }, []);

  const showNotice = useCallback((message: string) => {
    setNotice(message);
    if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current);
    noticeTimerRef.current = setTimeout(() => setNotice(null), 6000);
  }, []);

  // Stable identity: an inline callback here changes on every render, and an unstable
  // reference can rebuild the CommandExecutor (killing all [FOCUS] highlights). Keep this
  // memoized so the callback identity stays stable across renders.
  const handleSendReport = useCallback((report: any) => {
    const room = roomRef.current;
    if (room && room.localParticipant) {
      const bytes = new TextEncoder().encode(JSON.stringify(report));
      room.localParticipant.publishData(bytes, { topic: 'tutor.report', reliable: true });
    }
  }, []);

  const applyAudioMode = useCallback((mode: AudioMode) => {
    audioModeRef.current = mode === 'captions' ? 'captions' : 'voice';
    setAudioMode(mode);
    setIsAudioBlocked(mode === 'blocked');
    if (mode !== 'captions') {
      captionClockRef.current?.stop();
      captionQueueRef.current = [];
      setCaptionText(null);
    }
  }, []);

  const startNextCaption = useCallback(() => {
    const next = captionQueueRef.current.shift();
    if (!next) {
      captionClockRef.current?.stop();
      setCaptionText(null);
      return;
    }
    setCaptionText(next.spokenText);
    captionClockRef.current?.start(next, currentSpeedRef.current);
  }, []);
  startNextCaptionRef.current = startNextCaption;

  const handleCaptionStep = useCallback((step: Step) => {
    captionQueueRef.current.push(step);
    if (!captionClockRef.current?.running) startNextCaptionRef.current();
  }, []);

  const unblockAudio = useCallback(() => {
    const health = audioHealthRef.current;
    if (health) {
      health.unblock().catch(() => {});
      return;
    }
    if (roomRef.current) {
      roomRef.current.startAudio().then(() => {
        setIsAudioBlocked(false);
      }).catch(() => {});
    }
  }, []);

  // One sequencer per session orders and de-duplicates every tutor.events payload.
  // Whiteboard (a child) mounts before App's effects, so its executor-ready callback may have
  // already fired while sequencerRef was null: adopt that readiness here (and vice versa in
  // handleExecutorReady), otherwise every event is held forever and the hold timeout floods
  // resync requests while the board stays empty.
  useEffect(() => {
    const seq = EventSequencer.create(
      (evt) => handleTutorEventRef.current(evt),
      (epoch, lastSeq, reason, wantSnapshot) => handleSendReport({
        type: 'resync_request', epoch, lastSeq, reason, wantSnapshot: wantSnapshot ?? false,
      }),
      Boolean(executorRef.current),
    );
    sequencerRef.current = seq;
    return () => {
      if (sequencerRef.current === seq) sequencerRef.current = null;
    };
  }, [handleSendReport]);

  /** Parse one tutor.events payload and hand it to the sequencer (order + de-dup). */
  const ingestTutorPayload = (text: string) => {
    let evt: TutorWireEvent;
    try {
      evt = JSON.parse(text) as TutorWireEvent;
    } catch (err) {
      console.error('[tutor.events] unparseable payload', err);
      return;
    }
    sequencerRef.current?.ingest(evt);
  };

  // Connect to LiveKit Room on mount
  useEffect(() => {
    let isMounted = true;
    const room = new Room({
      adaptiveStream: true,
      dynacast: true,
    });
    roomRef.current = room;

    async function initConnection() {
      try {
        setConnectionStatus('connecting');
        setConnectionError(null);

        // 1. Fetch the token. Name login: the server maps the name to a stable user and that
        //    user's latest board (or a fresh one after "New board"), so the worker rehydrates
        //    the student's memory, paused lesson and pages.
        const newBoard = takeNewBoardRequest();
        const tokenRes = await fetch(`${API_BASE}/token`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(userName ? { name: userName, new_board: newBoard } : {}),
        });

        if (!tokenRes.ok) {
          throw new Error(`Failed to fetch room token (Status ${tokenRes.status})`);
        }

        const { token, board_id } = await tokenRes.json();
        if (!isMounted) return;

        if (board_id) setActiveBoardId(board_id);

        // 2. Setup event listeners
        room.on(RoomEvent.Connected, () => {
          if (!isMounted) return;
          console.log('[LiveKit] Connected to room:', room.name);
          setConnectionStatus('connected');
        });

        room.on(RoomEvent.Reconnected, () => {
          if (!isMounted) return;
          console.log('[LiveKit] Reconnected to room:', room.name);
          setConnectionStatus('connected');
          applyMicPreference(room);
          sequencerRef.current?.onReconnected();  // resend + replay from the heard cursor
        });

        room.on(RoomEvent.Reconnecting, () => {
          if (!isMounted) return;
          console.log('[LiveKit] Reconnecting to room...');
          setConnectionStatus('connecting');
        });

        room.on(RoomEvent.Disconnected, () => {
          if (!isMounted) return;
          console.log('[LiveKit] Disconnected from room');
          setConnectionStatus('disconnected');
          syncMatcherRef.current?.turnCancelled();
        });

        // tutor.events: small events arrive as data packets, large ones (page_restore, big
        // figures) as a text stream -- the server picks ONE path per event by size. Listening
        // over ~14 KiB never arrive on DataReceived, so both paths are registered.
        room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p?: RemoteParticipant, _k?: DataPacket_Kind, topic?: string) => {
          if (topic !== 'tutor.events') return;
          ingestTutorPayload(new TextDecoder().decode(payload));
        });
        room.registerTextStreamHandler('tutor.events', async (reader) => {
          try {
            ingestTutorPayload(await reader.readAll());
          } catch (err) {
            console.error('[tutor.events] stream read failed', err);
          }
        });

        // Tutor transcript -> word matcher. Prefer the synchronized text-stream transcript
        // (lk.transcription); fall back to legacy TranscriptionReceived segments. Both deliver
        // growing text, so only NEW complete words are fed.
        const isAgentParticipant = (p?: Participant | null) => {
          if (!p) return true;
          return Boolean((p as any).isAgent || p.identity?.includes('agent'));
        };

        room.registerTextStreamHandler('lk.transcription', async (reader, participantInfo) => {
          // The student's own STT arrives on this topic too, sent with the STUDENT's
          // identity (never in remoteParticipants) — it must not drive the tutor-word matcher.
          const from = room.remoteParticipants.get(participantInfo.identity);
          if (!isTutorTranscriptSender(participantInfo.identity, room.localParticipant?.identity, from)) return;
          streamTranscriptsRef.current = true;
          const id = reader.info.id;
          try {
            for await (const chunk of reader) feederRef.current?.pushDelta(id, chunk, false);
            feederRef.current?.pushDelta(id, ' ', true);
          } catch (err) {
            console.warn('[lk.transcription] stream ended abnormally', err);
          }
        });
        room.on(RoomEvent.TranscriptionReceived, (segments: any[], participant?: Participant) => {
          if (streamTranscriptsRef.current) return;
          if (participant && (participant.identity === room.localParticipant?.identity
                              || !isAgentParticipant(participant))) return;
          for (const seg of segments) {
            if (seg?.text) feederRef.current?.pushCumulative(String(seg.id), String(seg.text), Boolean(seg.final));
          }
        });

        // Handle incoming audio tracks so student can hear the tutor
        room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
          if (track.kind === Track.Kind.Audio) {
            const audioElement = track.attach();
            audioElement.id = `audio-${track.sid}`;
            document.body.appendChild(audioElement);
            console.log('[LiveKit] Attached remote audio track to DOM');
          }
        });

        room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
          track.detach().forEach((el) => el.remove());
        });

        // Track speaking state
        room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
          const agentSpeaking = speakers.some((sp) => sp.isAgent || sp.identity.includes('agent'));
          setIsSpeaking(agentSpeaking);
          syncMatcherRef.current?.setSpeaking(agentSpeaking);
        });

        // 3. Connect to LiveKit server
        console.log('[LiveKit] Connecting to:', LIVEKIT_URL);
        await room.connect(LIVEKIT_URL, token);
        if (!isMounted) return;
        setConnectionStatus('connected');

        // Unblock browser autoplay restrictions
        room.startAudio().then(() => {
          setIsAudioBlocked(false);
        }).catch((err) => {
          console.warn('[LiveKit] Audio autoplay pending user gesture:', err);
          setIsAudioBlocked(true);
        });

        // Student microphone: honour the saved mute preference.
        await applyMicPreference(room);
      } catch (err: any) {
        if (!isMounted) return;
        console.error('[LiveKit] Connection error:', err);
        setConnectionStatus('error');
        setConnectionError(err.message || 'Failed to connect');
      }
    }

    initConnection();

    return () => {
      isMounted = false;
      room.disconnect();
    };
  }, []);

  async function applyMicPreference(room: Room) {
    try {
      await room.localParticipant.setMicrophoneEnabled(!micMutedRef.current);
    } catch (micErr) {
      console.warn('[LiveKit] Microphone unavailable:', micErr);
      micMutedRef.current = true;
      setIsMicMuted(true);
      showNotice('Microphone is unavailable. You can still type your questions.');
    }
  }

  const handleToggleMic = async () => {
    const next = !micMutedRef.current;
    micMutedRef.current = next;
    setIsMicMuted(next);
    try {
      localStorage.setItem('tutor.micMuted', next ? '1' : '0');
    } catch {
      /* storage unavailable: preference lasts for this tab */
    }
    const room = roomRef.current;
    if (room && room.state === 'connected') await applyMicPreference(room);
  };

  // Wire-event handler. Kept in a ref so the listeners registered once at mount always call
  // the latest closure instead of the first render's handleSendMessage.
  const handleTutorEvent = (rawEvt: any) => {
    const executor = executorRef.current;
    if (!executor) return;
    routeTutorEvent(rawEvt, {
      executor,
      matcher: syncMatcherRef.current,
      layout: layoutRef.current,
      feeder: feederRef.current,
      refs: { currentTurnId: currentTurnIdRef, currentGeneration: currentGenerationRef },
      getAudioMode: () => audioModeRef.current,
      ui: {
        setThinking: setIsThinking,
        setLocked: setIsLocked,
        setHasDrawn: setHasDrawnContent,
        setCanContinue: setCanContinueLesson,
        showNotice,
        setDraft: setDraftText,
        setAudioMode: applyAudioMode,
        onCaptionStep: handleCaptionStep,
        showSessionEnded: (reason: string) => setSessionEndedReason(reason),
        setLessonPlan: (pages: PageHeader[]) => setLessonPages(pages),
        setCurrentPage: (index: number | null) => setLessonPageIndex(index),
      },
    });
  };
  const handleTutorEventRef = useRef(handleTutorEvent);
  handleTutorEventRef.current = handleTutorEvent;

  // Helper to call backend Agent RPC. Returns the parsed RpcAck; throws on transport error
  // so callRpcWithRetry can retry once.
  const callAgentRpc = async (method: string, payload: any = {}): Promise<RpcAck | null> => {
    const room = roomRef.current;
    if (!room || !room.localParticipant) {
      console.warn('[RPC] Cannot call RPC: Room not connected');
      return null;
    }

    // Find agent participant
    let agent = Array.from(room.remoteParticipants.values()).find(
      (p) => (p as any).isAgent || p.identity.includes('agent')
    );

    if (!agent && room.remoteParticipants.size > 0) {
      agent = Array.from(room.remoteParticipants.values())[0];
    }

    if (!agent) {
      console.warn('[RPC] No remote agent participant found in room yet. Remote participants:', Array.from(room.remoteParticipants.keys()));
      setIsLocked(false);
      return null;
    }

    try {
      const response = await room.localParticipant.performRpc({
        destinationIdentity: agent.identity,
        method,
        payload: JSON.stringify(payload),
      });
      console.log(`[RPC ${method}] Sent to ${agent.identity}`);
      if (!response) return null;
      try {
        return JSON.parse(response) as RpcAck;
      } catch {
        console.warn(`[RPC ${method}] unparseable ack`, response);
        return null;
      }
    } catch (err) {
      console.error(`[RPC ${method}] Error:`, err);
      setIsLocked(false);
      throw err;
    }
  };

  const handleNewBoard = () => {
    requestNewBoard();
    window.location.reload();
  };

  const handleSendMessage = async (text: string, intent: 'new' | 'auto' | 'topic' = 'auto') => {
    unblockAudio();
    setIsThinking(true);
    setIsLocked(true); // Lock until agent begins answering
    setDraftText(null);
    void applyHold('marker', false);          // a doubt/question releases the marker only
    if (marksRef.current.length > 0) {
      await callAgentRpc('submit_doubt', {
        typedText: text,
        marks: marksRef.current,
      });
      marksRef.current = [];
      setHasMarks(false);
      setClearMarksSignal((n) => n + 1);
    } else {
      // Starter topics start a new lesson; the input bar sends 'auto'.
      await callAgentRpc('submit_question', { text, intent });
    }
  };

  const handleExplainMarks = async () => {
    unblockAudio();
    setIsThinking(true);
    setIsLocked(true);
    void applyHold('marker', false);
    await callAgentRpc('submit_doubt', {
      typedText: '',
      marks: marksRef.current,
    });
    marksRef.current = [];
    setHasMarks(false);
  };

  const handleSubmitDoubtMarks = (marks: DoubtMark[]) => {
    marksRef.current = marks;
    setHasMarks(marks.length > 0);
    const room = roomRef.current;
    if (room && room.localParticipant) {
      const payload = {
        type: 'pointer_focus',
        marks,
        target:
          marks.length > 0
            ? {
              id: marks[0].entityId || marks[0].rowId,
              kind: marks[0].targetKind,
              text: marks[0].text,
            }
            : null,
      };
      const bytes = new TextEncoder().encode(JSON.stringify(payload));
      room.localParticipant.publishData(bytes, { topic: 'tutor.report', reliable: true });
    }
  };

  /** Holds: marker (first stroke), user_pause (Pause button / Space) and
   *  audio_blocked are independent; the tutor stays paused until the last one goes. */
  const applyHold = async (reason: RpcHoldReason, armed: boolean) => {
    await setHold(
      { rpc: callAgentRpc, registry: holdsRef.current, matcher: syncMatcherRef.current, setIsPaused },
      reason,
      armed,
    );
  };

  const handleTogglePause = () => {
    void applyHold('user_pause', !holdsRef.current.has('user_pause'));
  };

  const handleStrokeStart = () => {
    void applyHold('marker', true);
  };

  const handleMarksCleared = () => {
    marksRef.current = [];
    setHasMarks(false);
    void applyHold('marker', false);          // keeps user_pause / audio_blocked
  };

  // Space toggles the explicit user pause unless the student is typing.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'Space') return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      e.preventDefault();
      handleTogglePause();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const actionUi = {
    unblockAudio,
    setThinking: setIsThinking,
    setCanContinue: setCanContinueLesson,
    setCurrentSpeed,
    getCurrentSpeed: () => currentSpeed,
    setOcrProcessing: setIsOcrProcessing,
    setDraft: setDraftText,
    showNotice,
  };

  const handleChangeSpeed = (speed: number) => {
    currentSpeedRef.current = (speed === 0.8 || speed === 1.2 ? speed : 1.0) as 0.8 | 1.0 | 1.2;
    void changeSpeed(callAgentRpc, actionUi, speed);
  };

  const handleContinueLesson = async () => {
    await continueLesson(callAgentRpc, actionUi);
  };

  const handleEndClass = async () => {
    // The server says goodbye and then closes the room. Disconnecting immediately would cut
    // the goodbye off; disconnect ourselves only if the server has not within 10 s.
    await callAgentRpc('end_session');
    const room = roomRef.current;
    setTimeout(() => {
      if (room && room.state !== 'disconnected') room.disconnect();
    }, 10000);
  };

  const handleUploadPhoto = async (blob: Blob): Promise<{ success: boolean; message?: string } | void> => {
    setIsOcrProcessing(true);
    try {
      const result = await uploadPhoto(fetch, blob);
      if (result?.success && result.text) {
        setDraftText(result.text);            // confirm/edit before asking
      }
      return result;
    } finally {
      setIsOcrProcessing(false);
    }
  };

  return (
    <div
      className="whiteboard-container"
      onClick={() => {
        roomRef.current?.startAudio().catch(() => { });
      }}
    >
      {/* Top Header Bar */}
      <header
        style={{
          padding: '12px 24px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          zIndex: 30,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <h1 style={{ fontSize: 18, fontWeight: 700, color: '#fef3c7', fontFamily: 'var(--font-display)' }}>
            AI Math Tutor
          </h1>
          <span style={{ fontSize: 12, color: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.1)', padding: '2px 8px', borderRadius: 12 }}>
            CBSE 6–10
          </span>

          {/* Connection Indicator */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 8, fontSize: 12 }}>
            {connectionStatus === 'connected' ? (
              <span style={{ color: '#10b981', display: 'flex', alignItems: 'center', gap: 4 }}>
                <Wifi size={14} /> Live
              </span>
            ) : connectionStatus === 'connecting' ? (
              <span style={{ color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 4 }}>
                Connecting...
              </span>
            ) : (
              <span style={{ color: '#ef4444', display: 'flex', alignItems: 'center', gap: 4 }}>
                <WifiOff size={14} /> Offline
              </span>
            )}
          </div>

          {/* Signed-in student (name login) */}
          {userName && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 10 }}>
              <span
                data-testid="user-name"
                style={{ fontSize: 12, color: '#fef3c7', backgroundColor: 'rgba(255,255,255,0.06)', padding: '2px 10px', borderRadius: 12, display: 'flex', alignItems: 'center', gap: 4 }}
              >
                <User size={12} /> {userName}
              </span>
              {onLogout && (
                <button
                  onClick={onLogout}
                  title="Sign out"
                  style={{ background: 'none', border: '1px solid rgba(255,255,255,0.15)', color: '#94a3b8', padding: '2px 8px', borderRadius: 6, fontSize: 11, display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}
                >
                  <LogOut size={12} /> Sign out
                </button>
              )}
            </div>
          )}

          {/* Active Board Indicator and Reset */}
          {activeBoardId && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 10 }}>
              <button
                onClick={handleNewBoard}
                title="Start a new blank board session"
                style={{
                  background: 'none',
                  border: '1px solid rgba(255,255,255,0.15)',
                  color: '#94a3b8',
                  padding: '2px 8px',
                  borderRadius: 6,
                  fontSize: 11,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  cursor: 'pointer',
                  transition: 'all 0.15s ease',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = '#f59e0b';
                  e.currentTarget.style.color = '#fef3c7';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = 'rgba(255,255,255,0.15)';
                  e.currentTarget.style.color = '#94a3b8';
                }}
              >
                <PlusCircle size={12} /> New Board
              </button>
            </div>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button
            onClick={() => setNotesOpen((v) => !v)}
            title="Lesson notes"
            disabled={connectionStatus !== 'connected'}
            style={{
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.15)',
              color: '#cbd5e1',
              padding: '4px 10px',
              borderRadius: 6,
              fontSize: 12,
              cursor: connectionStatus === 'connected' ? 'pointer' : 'not-allowed',
            }}
          >
            Notes
          </button>
          <AudioWaveform isActive={connectionStatus === 'connected'} isSpeaking={isSpeaking} tutorName="Vamshi" />
        </div>
      </header>

      {/* Connection Error Banner */}
      {connectionError && (
        <div
          style={{
            backgroundColor: 'rgba(239, 68, 68, 0.2)',
            border: '1px solid rgba(239, 68, 68, 0.4)',
            color: '#fca5a5',
            padding: '8px 16px',
            margin: '0 24px 8px 24px',
            borderRadius: 8,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 13,
            zIndex: 40,
          }}
        >
          <AlertCircle size={16} />
          <span>Connection error: {connectionError}. Make sure FastAPI (port 8000) and LiveKit (port 7880) are running.</span>
        </div>
      )}

      {/* Audio Autoplay Blocked Banner */}
      {isAudioBlocked && (
        <div
          onClick={unblockAudio}
          role="button"
          tabIndex={0}
          style={{
            backgroundColor: 'rgba(245, 158, 11, 0.2)',
            border: '1px solid rgba(245, 158, 11, 0.6)',
            color: '#fef3c7',
            padding: '8px 16px',
            margin: '0 24px 8px 24px',
            borderRadius: 8,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: 13,
            cursor: 'pointer',
            zIndex: 45,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <VolumeX size={16} color="#f59e0b" />
            <span><strong>Audio is muted by browser:</strong> Tap here to enable Teacher Vamshi's voice explanation.</span>
          </div>
          <span style={{ fontSize: 11, textDecoration: 'underline', color: '#fcd34d' }}>Enable Sound</span>
        </div>
      )}

      {notice && (
        <div
          role="status"
          style={{
            backgroundColor: 'rgba(245, 158, 11, 0.15)',
            border: '1px solid rgba(245, 158, 11, 0.4)',
            color: '#fde68a',
            padding: '8px 16px',
            margin: '0 24px 8px 24px',
            borderRadius: 8,
            fontSize: 13,
            zIndex: 40,
          }}
        >
          {notice}
        </div>
      )}

      {/* Main Whiteboard Area with Overlays */}
      <div style={{ position: 'relative', flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        <LessonProgress pages={lessonPages} currentIndex={lessonPageIndex} />
        <Whiteboard
          onExecutorReady={handleExecutorReady}
          onSubmitDoubtMarks={handleSubmitDoubtMarks}
          onExplainMarks={handleExplainMarks}
          onStrokeStart={handleStrokeStart}
          onMarksCleared={handleMarksCleared}
          clearMarksSignal={clearMarksSignal}
          canMark={!isLocked}
          onSendReport={handleSendReport}
        />

        {/* Floating Thinking / Preparing Board Indicator */}
        {isThinking && (
          <div
            style={{
              position: 'absolute',
              top: 20,
              left: '50%',
              transform: 'translateX(-50%)',
              backgroundColor: 'rgba(15, 23, 42, 0.92)',
              backdropFilter: 'blur(12px)',
              border: '1px solid rgba(245, 158, 11, 0.5)',
              color: '#fef3c7',
              padding: '10px 22px',
              borderRadius: 24,
              fontSize: 14,
              fontWeight: 500,
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              boxShadow: '0 8px 30px rgba(0, 0, 0, 0.6)',
              zIndex: 35,
            }}
          >
            <Sparkles size={16} color="#f59e0b" />
            <span>Teacher Vamshi is thinking & preparing the board...</span>
          </div>
        )}

        {(isAudioBlocked || audioMode !== 'voice') && (
          <CaptionStrip text={captionText} />
        )}

        {/* Classroom Welcome & Starter Topics Overlay on Empty Board */}
        {!hasDrawnContent && !isThinking && (
          <div
            style={{
              position: 'absolute',
              top: '50%',
              left: '50%',
              transform: 'translate(-50%, -50%)',
              width: '90%',
              maxWidth: 760,
              backgroundColor: 'rgba(15, 23, 19, 0.92)',
              backdropFilter: 'blur(16px)',
              border: '1px solid rgba(245, 158, 11, 0.3)',
              borderRadius: 16,
              padding: '28px 32px',
              textAlign: 'center',
              boxShadow: '0 20px 50px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255, 255, 255, 0.05)',
              zIndex: 25,
            }}
          >
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '4px 12px', borderRadius: 20, backgroundColor: 'rgba(245, 158, 11, 0.15)', color: '#f59e0b', fontSize: 13, fontWeight: 600, marginBottom: 12 }}>
              <Sparkles size={14} />
              CBSE Class 6–10 Interactive Classroom
            </div>

            <h2 style={{ fontSize: 22, fontWeight: 700, color: '#fef3c7', marginBottom: 8, fontFamily: 'var(--font-display)' }}>
              Teacher Vamshi is ready on the Whiteboard
            </h2>
            <p style={{ fontSize: 14, color: '#cbd5e1', maxWidth: 540, margin: '0 auto 20px auto', lineHeight: 1.5 }}>
              Ask any math question by voice, text, or photo upload below. Or tap a starter topic to see a complete live lesson with geometry diagrams and chalk derivation:
            </p>

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
                gap: 12,
                textAlign: 'left',
              }}
            >
              {STARTER_TOPICS.map((item, idx) => (
                <button
                  key={idx}
                  onClick={() => handleSendMessage(item.prompt, 'new')}
                  disabled={isLocked || connectionStatus !== 'connected'}
                  style={{
                    backgroundColor: 'rgba(255, 255, 255, 0.04)',
                    border: '1px solid rgba(255, 255, 255, 0.08)',
                    borderRadius: 12,
                    padding: '14px 16px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                    cursor: isLocked ? 'not-allowed' : 'pointer',
                    transition: 'all 0.2s ease',
                    color: 'inherit',
                  }}
                  onMouseEnter={(e) => {
                    if (!isLocked) {
                      e.currentTarget.style.backgroundColor = 'rgba(245, 158, 11, 0.12)';
                      e.currentTarget.style.borderColor = 'rgba(245, 158, 11, 0.4)';
                      e.currentTarget.style.transform = 'translateY(-2px)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor = 'rgba(255, 255, 255, 0.04)';
                    e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.08)';
                    e.currentTarget.style.transform = 'translateY(0)';
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 18 }}>{item.icon}</span>
                    <span style={{ fontSize: 14, fontWeight: 600, color: '#fef3c7' }}>{item.title}</span>
                  </div>
                  <span style={{ fontSize: 12, color: '#94a3b8', lineHeight: 1.4 }}>{item.desc}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Bottom Floating Input and Controls */}
      <InputBar
        isLocked={isLocked || connectionStatus !== 'connected'}
        hasMarks={hasMarks}
        canContinueLesson={canContinueLesson}
        currentSpeed={currentSpeed}
        onSendMessage={handleSendMessage}
        onExplainMarks={handleExplainMarks}
        onTogglePause={handleTogglePause}
        isPaused={isPaused}
        onChangeSpeed={handleChangeSpeed}
        onContinueLesson={handleContinueLesson}
        onEndClass={handleEndClass}
        onOpenPhotoModal={() => setIsPhotoModalOpen(true)}
        isMicMuted={isMicMuted}
        onToggleMic={handleToggleMic}
        draftText={draftText}
        onDraftConsumed={() => setDraftText(null)}
      />

      {/* Photo Upload Modal */}
      <PhotoUploadModal
        isOpen={isPhotoModalOpen}
        onClose={() => setIsPhotoModalOpen(false)}
        onUploadImage={handleUploadPhoto}
      />

      {/* Notes drawer: read-only page list + preview, never touches the live board. */}
      <NotesDrawer
        open={notesOpen}
        onClose={() => setNotesOpen(false)}
        boardId={activeBoardId}
        pages={lessonPages}
        apiBase={API_BASE}
      />

      {/* Blocking session-ended message: a newer tab owns this board. */}
      {sessionEndedReason && (
        <div
          role="alertdialog"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 100,
            backgroundColor: 'rgba(2, 6, 23, 0.88)',
            backdropFilter: 'blur(6px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            style={{
              backgroundColor: '#0f172a',
              border: '1px solid rgba(245, 158, 11, 0.4)',
              borderRadius: 16,
              padding: '28px 32px',
              maxWidth: 420,
              textAlign: 'center',
              color: '#fef3c7',
              boxShadow: '0 20px 60px rgba(0, 0, 0, 0.7)',
            }}
          >
            <div style={{ fontSize: 32, marginBottom: 8 }}>🖥️</div>
            <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
              {sessionEndedReason === 'opened_elsewhere'
                ? 'This board is open in another tab.'
                : 'This session has ended.'}
            </div>
            <div style={{ fontSize: 13, color: '#94a3b8', lineHeight: 1.5 }}>
              Only one tab can write to a board at a time. Continue there, or open a new board here.
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
export default App;

// frontend/src/whiteboard/transcriptSync.ts
// Speech sync: matches the tutor's spoken words to the planned steps.

import { BoardOp, Step } from '../types/events';

/** Word normalization. MUST stay identical to backend app/tutor/stream_parser.normalize_words. */
export function normalizeWord(text: string): string[] {
  let s = text.toLowerCase();
  s = s.replace(/<[^>]+>/g, '');          // strip SSML
  s = s.replace(/’/g, "'");
  s = s.replace(/[^a-z0-9']/g, ' ');      // replace (never delete) everything else
  return s.split(/\s+/).filter(Boolean);
}

export function normalizeOp(raw: any): BoardOp {
  return {
    ...raw,
    opId: raw.opId || raw.op_id || '',
    kind: raw.kind,
    atWord: raw.atWord ?? raw.at_word ?? 0,
    text: raw.text,
    rowId: raw.rowId || raw.row_id,
    durationMs: raw.durationMs ?? raw.duration_ms,
    entityId: raw.entityId || raw.entity_id,
    focusMode: raw.focusMode || raw.focus_mode,
    emphasizeRowId: raw.emphasizeRowId || raw.emphasize_row_id,
    pageTitle: raw.pageTitle || raw.page_title,
  };
}

export function normalizeStep(raw: any): Step {
  const rawOps = Array.isArray(raw?.ops) ? raw.ops : [];
  return {
    turnId: raw?.turnId || raw?.turn_id || '',
    generation: raw?.generation ?? 0,
    stepIndex: raw?.stepIndex ?? raw?.step_index ?? 0,
    spokenText: raw?.spokenText || raw?.spoken_text || '',
    words: Array.isArray(raw?.words) ? raw.words : [],
    ops: rawOps.map(normalizeOp),
    sourceStepIndex: raw?.sourceStepIndex ?? raw?.source_step_index,
    isLast: raw?.isLast ?? raw?.is_last,
  };
}

export interface QueuedStep {
  step: Step;
  unfiredOps: BoardOp[];
  started: boolean;
  completed: boolean;
}

/** Fired with the op AND the generation of the step it belongs to, so the executor can drop
 *  ops of a superseded turn. */
export type FireOp = (op: BoardOp, generation: number, turnId: string) => void;
export type CompletedBy = 'words' | 'stall' | 'flush' | 'caption';
export type StepComplete = (step: Step, by: CompletedBy) => void;
export type StepStarted = (step: Step) => void;

export const STALL_MS_SPEAKING = 2500;
export const MATCHER_LOOKAHEAD = 4;
export const EMA_INITIAL_MS = 300;
export const EMA_ALPHA = 0.2;
export const EMA_MIN_MS = 80;
export const EMA_MAX_MS = 1500;

const DIGIT_WORDS: Record<string, string> = {
  '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
  '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine', '10': 'ten',
};

const MATH_SYNONYMS: Record<string, string[]> = {
  'aaa': ['triple', 'a', 'three'],
  'sss': ['triple', 's', 'three'],
  'sas': ['s', 'side'],
  'asa': ['a', 'angle'],
  'rhs': ['right', 'hypotenuse'],
  'bpt': ['basic', 'proportionality', 'theorem', 'ratio'],
  '180': ['one', 'hundred', 'eighty'],
  '360': ['three', 'sixty'],
  '90': ['ninety'],
  'delta': ['triangle', 'tri'],
  'triangle': ['tri', 'delta'],
  'parallel': ['parallels', 'parallelism'],
  'parallels': ['parallel'],
  'congruent': ['congruency', 'equal'],
  'similar': ['similarity'],
};

/** Stems after dropping one plural / -d / -ed ending: triangle->triangle, squared->square. */
const suffixStems = (w: string): string[] => {
  const out = [w];
  if (w.endsWith('es') && w.length > 3) out.push(w.slice(0, -2));
  if (w.endsWith('s') && w.length > 2) out.push(w.slice(0, -1));
  if (w.endsWith('ed') && w.length > 3) out.push(w.slice(0, -2));
  if (w.endsWith('d') && w.length > 2) out.push(w.slice(0, -1));
  return out.filter((s) => s.length >= 3);
};

export function wordsMatch(expected: string | undefined, incoming: string): boolean {
  if (!expected) return false;
  if (expected === incoming) return true;
  if (DIGIT_WORDS[expected] === incoming || DIGIT_WORDS[incoming] === expected) return true;
  if (MATH_SYNONYMS[expected]?.includes(incoming) || MATH_SYNONYMS[incoming]?.includes(expected)) return true;
  // Plural / -d / -ed suffix: triangle<->triangles, square<->squared.
  if (expected.length >= 3 && incoming.length >= 3) {
    const incomingStems = suffixStems(incoming);
    if (suffixStems(expected).some((s) => incomingStems.includes(s))) return true;
  }
  return false;
}

/** Span matching: checks if concatenating 2-4 consecutive expected words matches incoming (e.g. ['a','b','c'] -> 'abc') */
export function matchSpan(words: string[], startIndex: number, incoming: string): number {
  if (!incoming || incoming.length < 2) return 0;
  let combined = '';
  for (let k = 0; k < 4 && startIndex + k < words.length; k++) {
    combined += words[startIndex + k];
    if (combined === incoming) return k + 1; // Returns count of matched expected words
  }
  return 0;
}

export class TranscriptSyncMatcher {
  private queue: QueuedStep[] = [];
  private currentStepIndex = 0;
  private currentWordPointer = 0;
  private onFireOp: FireOp;
  private onStepComplete: StepComplete;
  private onStepStarted: StepStarted;
  private stallTimeoutId: ReturnType<typeof setTimeout> | null = null;
  private isSpeaking = false;
  private held = false;
  private suspended = false;
  private turnId = '';
  private pendingJump: { word: string; target: number } | null = null;
  private pendingNextStep: { word: string; stepIndex: number; offset: number } | null = null;
  private ema = EMA_INITIAL_MS;
  private lastWordAt: number | null = null;

  constructor(onFireOp: FireOp, onStepComplete: StepComplete = () => {}, onStepStarted: StepStarted = () => {}) {
    this.onFireOp = onFireOp;
    this.onStepComplete = onStepComplete;
    this.onStepStarted = onStepStarted;
  }

  /** Smoothed ms per spoken word; the executor uses it to pace WRITE animations. */
  public get msPerWord(): number {
    return this.ema;
  }

  private getCurrentStep(): QueuedStep | undefined {
    return this.queue.find((q) => q.step.stepIndex === this.currentStepIndex);
  }

  /** New turn: forget every queued step of the previous turn so a superseded turn's unmatched
   *  steps cannot be matched against the NEW turn's words. */
  public beginTurn(turnId: string): void {
    this.clearStallTimer();
    this.turnId = turnId;
    this.queue = [];
    this.currentStepIndex = 0;
    this.currentWordPointer = 0;
    this.pendingJump = null;
    this.pendingNextStep = null;
  }

  public get activeTurnId(): string {
    return this.turnId;
  }

  public enqueueStep(rawStep: Step): void {
    const step = normalizeStep(rawStep);
    if (this.turnId && step.turnId && step.turnId !== this.turnId) return;          // stale turn
    if (this.queue.some((q) => q.step.stepIndex === step.stepIndex)) return; // duplicate delivery
    this.queue.push({ step, unfiredOps: [...step.ops], started: false, completed: false });
    this.queue.sort((a, b) => a.step.stepIndex - b.step.stepIndex);
    this.scheduleStall();
  }

  public setSpeaking(speaking: boolean): void {
    this.isSpeaking = speaking;
    this.scheduleStall();
  }

  /** Marker / Pause: freeze the stall fallback until release. */
  public hold(): void {
    this.held = true;
    this.clearStallTimer();
  }

  public release(): void {
    this.held = false;
    this.scheduleStall();
  }

  /** Asides (greeting, redirect, bridge, …): words must not advance the matcher. */
  public suspend(): void {
    this.suspended = true;
    this.clearStallTimer();
  }

  public resume(): void {
    this.suspended = false;
    this.scheduleStall();
  }

  private clearStallTimer(): void {
    if (this.stallTimeoutId) {
      clearTimeout(this.stallTimeoutId);
      this.stallTimeoutId = null;
    }
  }

  /**
   * Stall fallback: ONLY while the tutor is audibly speaking and the matcher is neither held
   * (marker/pause) nor suspended (aside). Steps are published BEFORE their audio (by design),
   * so while a turn is completely unstarted, we don't force-draw early.
   */
  private scheduleStall(): void {
    if (!this.isSpeaking || this.held || this.suspended) {
      this.clearStallTimer();
      return;
    }
    this.clearStallTimer();
    const current = this.getCurrentStep();
    if (!current) return;

    this.stallTimeoutId = setTimeout(() => {
      const curr = this.getCurrentStep();
      if (!curr) return;
      console.warn(`[sync_force] step ${curr.step.stepIndex}: advancing via stall fallback`);
      this.completeCurrentStep('stall');
      this.scheduleStall();
    }, STALL_MS_SPEAKING);
  }

  /**
   * Turn ended: all speech has finished. Flush and complete all remaining steps immediately.
   */
  public flushRemaining(turnId?: string): void {
    if (turnId && this.turnId && turnId !== this.turnId) return;
    this.clearStallTimer();
    this.queue.sort((a, b) => a.step.stepIndex - b.step.stepIndex);
    for (const q of this.queue) {
      if (q.step.stepIndex >= this.currentStepIndex && !q.completed) {
        this.currentStepIndex = q.step.stepIndex;
        this.completeCurrentStep('flush');
      }
    }
  }

  /** Feed ONE normalized word, or a phrase (split with normalizeWord). */
  public onIncomingTranscript(rawText: string): void {
    if (this.suspended) return;
    for (const w of normalizeWord(rawText)) this.matchWord(w);
  }

  /** Caption clock words: same matching, but completions report `caption`. */
  public feedCaptionWord(w: string): void {
    if (this.suspended) return;
    this.matchWord(w, 'caption');
  }

  private markStarted(current: QueuedStep): void {
    if (!current.started) {
      current.started = true;
      this.onStepStarted(current.step);
    }
  }

  private matchWord(w: string, by: CompletedBy = 'words'): void {
    const current = this.getCurrentStep();
    if (!current) return;

    // Confirmation for a buffered in-step jump over more than one word.
    if (this.pendingJump) {
      const { word: jumpWord, target } = this.pendingJump;
      this.pendingJump = null;
      const spanCount = matchSpan(current.step.words, target + 1, w);
      if (wordsMatch(current.step.words[target + 1], w) || spanCount > 0) {
        this.markStarted(current);
        while (this.currentWordPointer < target) this.advanceWord(by);
        this.advanceWord(by);              // the jump target itself
        const count = spanCount > 0 ? spanCount : 1;
        for (let i = 0; i < count; i++) this.advanceWord(by); // the confirming word(s)
        this.scheduleStall();
        return;
      }
      // Not confirmed: drop the buffered word and try this word on its own.
    }

    const words = current.step.words;
    if (wordsMatch(words[this.currentWordPointer], w)) {
      this.markStarted(current);
      this.advanceWord(by);
      this.scheduleStall();
      return;
    }

    // Check span match at current pointer: N-to-1 letter collapse (e.g. ['a','b','c'] -> 'abc')
    const spanAtPtr = matchSpan(words, this.currentWordPointer, w);
    if (spanAtPtr > 0) {
      this.markStarted(current);
      for (let i = 0; i < spanAtPtr; i++) this.advanceWord(by);
      this.scheduleStall();
      return;
    }

    // In-step look-ahead (MATCHER_LOOKAHEAD); a jump over > 1 word needs the next word to
    // confirm. Words of length <= 2 never start a jump unless they match a multi-letter span.
    for (let offset = 1; offset <= MATCHER_LOOKAHEAD && this.currentWordPointer + offset < words.length; offset++) {
      const target = this.currentWordPointer + offset;
      const spanAtTarget = matchSpan(words, target, w);
      if (wordsMatch(words[target], w) || spanAtTarget > 0) {
        if (offset > 1) {
          if (w.length <= 2 && spanAtTarget === 0) return;
          this.pendingJump = { word: w, target: target + (spanAtTarget > 0 ? spanAtTarget - 1 : 0) };
          return;
        }
        this.markStarted(current);
        while (this.currentWordPointer < target) this.advanceWord(by);
        const count = spanAtTarget > 0 ? spanAtTarget : 1;
        for (let i = 0; i < count; i++) this.advanceWord(by);
        this.scheduleStall();
        return;
      }
    }

    const wordsRemaining = words.length - this.currentWordPointer;
    const currentSubstantiallyDone =
      this.currentWordPointer >= Math.floor(words.length * 0.4) || wordsRemaining <= 3;

    // Confirmation for a buffered next-step jump: two consecutive matches are required.
    if (this.pendingNextStep) {
      const p = this.pendingNextStep;
      const pStep = this.queue.find((q) => q.step.stepIndex === p.stepIndex);
      this.pendingNextStep = null;
      if (currentSubstantiallyDone && pStep && wordsMatch(pStep.step.words[p.offset + 1], w)) {
        while (this.currentStepIndex < p.stepIndex) this.completeCurrentStep(by);
        this.matchWord(p.word, by);
        this.matchWord(w, by);
        return;
      }
    }

    // Look into the next steps; buffer the first matching word until the next one confirms.
    if (currentSubstantiallyDone) {
      for (const stepOffset of [1, 2]) {
        const next = this.queue.find((q) => q.step.stepIndex === this.currentStepIndex + stepOffset);
        if (!next) continue;
        const limit = stepOffset === 1 ? 5 : 3;
        for (let offset = 0; offset < Math.min(limit, next.step.words.length); offset++) {
          if (wordsMatch(next.step.words[offset], w)) {
            this.pendingNextStep = { word: w, stepIndex: next.step.stepIndex, offset };
            return;
          }
        }
      }
    }
  }

  private fire(q: QueuedStep, op: BoardOp): void {
    this.onFireOp(op, q.step.generation, q.step.turnId);
  }

  private advanceWord(by: CompletedBy = 'words'): void {
    const current = this.getCurrentStep();
    if (!current) return;
    const now = Date.now();
    if (this.lastWordAt !== null) {
      const dt = now - this.lastWordAt;
      if (dt >= EMA_MIN_MS && dt <= EMA_MAX_MS) {
        this.ema = (1 - EMA_ALPHA) * this.ema + EMA_ALPHA * dt;
      }
    }
    this.lastWordAt = now;

    this.currentWordPointer += 1;
    const nWords = current.step.words.length;
    const toFire: BoardOp[] = [];
    current.unfiredOps = current.unfiredOps.filter((op) => {
      // The parser already retimes trailing ops; fire exactly where told.
      const at = op.atWord;
      if (this.currentWordPointer >= at) {
        toFire.push(op);
        return false;
      }
      return true;
    });
    for (const op of toFire) this.fire(current, op);
    if (this.currentWordPointer >= nWords) this.completeCurrentStep(by);
  }

  private completeCurrentStep(by: CompletedBy = 'words'): void {
    const current = this.getCurrentStep();
    if (!current) return;
    // A step that advances without a matched word (stall fallback, flush, next-step
    // look-ahead) is genuinely started: report it so the reveal clock can show the base
    // figure instead of leaving it hidden while the step's ink is already on the board.
    this.markStarted(current);
    while (current.unfiredOps.length > 0) this.fire(current, current.unfiredOps.shift()!);
    if (!current.completed) {
      current.completed = true;
      this.onStepComplete(current.step, by);
    }
    this.currentStepIndex += 1;
    this.currentWordPointer = 0;
  }

  /** Server replay (backchannel / redirect / marker resume). Ops are idempotent in the
   *  executor, so re-arming already drawn ops never duplicates ink. */
  public replayFromStep(turnId: string, stepIndex: number): void {
    if (turnId !== this.turnId) return;
    this.currentStepIndex = stepIndex;
    this.currentWordPointer = 0;
    this.pendingJump = null;
    this.pendingNextStep = null;
    for (const q of this.queue) {
      if (q.step.stepIndex >= stepIndex) {
        q.unfiredOps = [...q.step.ops];
        q.started = false;
        q.completed = false;
      }
    }
    this.scheduleStall();
  }

  public turnCancelled(turnId?: string): void {
    if (turnId && this.turnId && turnId !== this.turnId) return;
    this.clearStallTimer();
    this.queue = [];
    this.currentStepIndex = 0;
    this.currentWordPointer = 0;
    this.pendingJump = null;
    this.pendingNextStep = null;
  }

  /** Test/debug view. */
  public get pointer(): { step: number; word: number; queued: number } {
    return { step: this.currentStepIndex, word: this.currentWordPointer, queued: this.queue.length };
  }
}

/**
 * Converts agent transcription into NEW words exactly once.
 *
 * LiveKit delivers the tutor's transcript either as cumulative segments (RoomEvent.
 * TranscriptionReceived: the SAME segment id arrives repeatedly with ever-longer text) or as
 * text-stream deltas (topic "lk.transcription"). Feeding the whole cumulative text on every
 * update would match each word many times and the lookahead would jump on repeated words
 * ("the"), firing ops early. The last token is held back until it is complete (a space
 * or punctuation follows it, or the segment is final), so "thir" is never matched as a word.
 */
export class TranscriptWordFeeder {
  private consumed = new Map<string, number>();
  private buffers = new Map<string, string>();
  private onWord: (w: string) => void;

  constructor(onWord: (w: string) => void) {
    this.onWord = onWord;
  }

  public pushCumulative(segmentId: string, text: string, final: boolean): void {
    const tokens = normalizeWord(text);
    const complete = final || /[\s.,!?;:)\]]$/.test(text);
    const stable = complete ? tokens.length : Math.max(0, tokens.length - 1);
    const done = this.consumed.get(segmentId) ?? 0;
    for (let i = done; i < stable; i++) this.onWord(tokens[i]);
    if (stable > done) this.consumed.set(segmentId, stable);
    if (this.consumed.size > 500) {
      const first = this.consumed.keys().next().value;
      if (first !== undefined) this.consumed.delete(first);
    }
  }

  public pushDelta(streamId: string, chunk: string, final: boolean): void {
    const text = (this.buffers.get(streamId) ?? '') + chunk;
    this.buffers.set(streamId, text);
    this.pushCumulative(streamId, text, final);
    if (final) this.buffers.delete(streamId);
  }

  public reset(): void {
    this.consumed.clear();
    this.buffers.clear();
  }
}

// frontend/src/app/eventRouter.ts
// App's tutor-event switch, extracted so it can be tested without a browser room.
import { CommandExecutor } from '../whiteboard/commandExecutor';
import { BoardLayout } from '../whiteboard/boardLayout';
import { TranscriptSyncMatcher, TranscriptWordFeeder, normalizeOp, normalizeStep } from '../whiteboard/transcriptSync';
import type { PageHeader } from '../types/events';

export interface RouterDeps {
  executor: CommandExecutor;
  matcher: TranscriptSyncMatcher | null;
  layout: BoardLayout | null;
  feeder: TranscriptWordFeeder | null;
  refs: { currentTurnId: { current: string }; currentGeneration: { current: number } };
  getAudioMode?: () => 'voice' | 'captions';
  ui: {
    setThinking(v: boolean): void;
    setLocked(v: boolean): void;
    setHasDrawn(v: boolean): void;
    setCanContinue(v: boolean): void;
    showNotice(m: string): void;
    setDraft(t: string | null): void;
    setAudioMode?(m: 'voice' | 'captions'): void;
    onCaptionStep?(step: any): void;
    showSessionEnded(reason: string): void;
    setLessonPlan?(pages: PageHeader[]): void;
    setCurrentPage?(index: number | null): void;
  };
}

// Events that are always delivered, even from a superseded generation: the tutor's
// non-step speech and the global status events must still reach the client.
const GEN_EXEMPT = new Set(['conv_state', 'notice', 'audio_status', 'aside', 'board_snapshot', 'session_ended']);

/** The wipe a new-figure doubt announced, held until that turn has content. */
interface PendingPage { turnId: string; pageId: string }
// Keyed by the executor: App builds a fresh deps/refs object for every event, the executor is
// the one long-lived board object.
const pendingPages = new WeakMap<object, PendingPage>();

function startNewPage(deps: RouterDeps, pageId: string): void {
  deps.executor.clearBoard(true);
  deps.layout?.reset(pageId);
  deps.layout?.setLayoutMode('TEXT_ONLY_3COL');
  deps.ui.setHasDrawn(false);
}

/** Apply a held new-page wipe when the first board content of ITS turn arrives. */
function applyPendingPage(deps: RouterDeps, turnId: string): void {
  const pending = pendingPages.get(deps.executor);
  if (pending && (!turnId || pending.turnId === turnId)) {
    pendingPages.delete(deps.executor);
    startNewPage(deps, pending.pageId);
  }
}

/** `current` is `pageId` itself or one of its overflow sub-pages (`${pageId}_pN`); board_snapshot
 *  re-roots the layout at a sub-page id, so both mean "the same page". */
function isSamePage(current: string, pageId: string): boolean {
  return current === pageId || current.startsWith(`${pageId}_p`);
}

export function routeTutorEvent(rawEvt: any, deps: RouterDeps): void {
  const executor = deps.executor;
  const matcher = deps.matcher;
  if (!executor) return;

  const evtType = rawEvt.type;
  const evtGen = typeof rawEvt.generation === 'number' ? rawEvt.generation : 0;
  const evtTurnId = rawEvt.turnId || rawEvt.turn_id || '';
  const refs = deps.refs;
  const ui = deps.ui;

  // Generation gate: page_restore/turn_started carry the NEW generation; anything older is a
  // late event from a superseded turn (e.g. its last step) and must never touch the board.
  if (typeof evtGen === 'number' && evtGen < refs.currentGeneration.current &&
      !GEN_EXEMPT.has(evtType)) {
    console.debug('[tutor.events] stale generation dropped', evtType, evtGen);
    return;
  }

  switch (evtType) {
    case 'turn_started': {
      refs.currentTurnId.current = evtTurnId;
      refs.currentGeneration.current = evtGen;
      executor.setGeneration(evtGen);
      matcher?.beginTurn(evtTurnId);
      matcher?.resume();               // a new turn always starts un-suspended
      deps.feeder?.reset();
      ui.setThinking(false);
      const pageIndex = typeof rawEvt.pageIndex === 'number' ? rawEvt.pageIndex : null;
      ui.setCurrentPage?.(pageIndex);  // highlight the chapter page being taught
      const isNewPage = rawEvt.newPage ?? rawEvt.new_page ?? false;
      const pageId = rawEvt.pageId || rawEvt.page_id || '';
      const layoutPage = deps.layout?.currentPageId;
      pendingPages.delete(executor);       // a newer turn supersedes a held wipe
      if (isNewPage && rawEvt.kind === 'doubt') {
        // A new-figure doubt must not wipe the board here, 10-35 s before its figure or
        // first step exists; if the doubt then fails the student would be left with a blank
        // board. Keep the old board (the doubt's context) until the doubt's content arrives.
        pendingPages.set(executor, { turnId: evtTurnId, pageId });
      } else if (isNewPage) {
        startNewPage(deps, pageId);
      } else if (pageIndex !== null && pageIndex > 0 && pageId
                 && typeof layoutPage === 'string' && !isSamePage(layoutPage, pageId)) {
        // Chapter page i>0 arrives with newPage:false (so sticky blocks can tween) and its
        // page_commit only re-lays the BLOCKS: the previous page's rows would stay on the
        // board, page 1 would write below them, and board reports would keep page 0's id (the
        // server ignores them as a foreign page). Start the new page's work column here; the
        // figure blocks stay for the page_commit to keep (sticky) or fade.
        deps.layout?.reset(pageId);
        executor.clearWorkRows();
      }
      ui.setLocked(false);
      break;
    }
    case 'page_restore': {
      pendingPages.delete(executor);
      refs.currentGeneration.current = Math.max(refs.currentGeneration.current, evtGen);
      matcher?.turnCancelled();
      const pageId = rawEvt.pageId || rawEvt.page_id || '';
      executor.pageRestore(pageId, rawEvt.diagram, rawEvt.ops || [], evtGen);
      if ((rawEvt.ops && rawEvt.ops.length > 0) || rawEvt.diagram) {
        ui.setHasDrawn(true);
      }
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
    case 'board_snapshot': {
      // Render the LAST sub-page instantly, then adopt the active turn and its
      // pending steps so a reconnected client resumes at the heard cursor.
      const current = rawEvt.current || {};
      const subPages = current.subPages || current.sub_pages || [];
      const last = subPages.length
        ? subPages[subPages.length - 1]
        : { subId: current.pageId || current.page_id || '', ops: [] };
      const ops = (last.ops || []).map(normalizeOp);
      pendingPages.delete(executor);
      refs.currentGeneration.current = Math.max(refs.currentGeneration.current, evtGen);
      executor.setGeneration(evtGen);
      matcher?.turnCancelled();
      executor.pageRestore(last.subId, current.diagram ?? null, ops, evtGen);
      const active = rawEvt.activeTurn ?? rawEvt.active_turn ?? null;
      if (active) {
        const activeTurnId = active.turnId || active.turn_id || '';
        refs.currentTurnId.current = activeTurnId;
        refs.currentGeneration.current = Math.max(refs.currentGeneration.current, active.generation ?? evtGen);
        executor.setGeneration(refs.currentGeneration.current);
        matcher?.beginTurn(activeTurnId);
        for (const rawStep of rawEvt.pendingSteps || rawEvt.pending_steps || []) {
          matcher?.enqueueStep(normalizeStep(rawStep));
        }
      }
      ui.setCanContinue(!!(rawEvt.canContinueLesson ?? rawEvt.can_continue_lesson));
      if (ops.length > 0 || current.diagram) ui.setHasDrawn(true);
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
    case 'diagram_commit': {
      if (evtTurnId && refs.currentTurnId.current && evtTurnId !== refs.currentTurnId.current) return;
      applyPendingPage(deps, evtTurnId);
      ui.setHasDrawn(true);
      ui.setThinking(false);
      try {
        // reference: a late figure appears whole at the next step start.
        executor.commitDiagram(rawEvt.diagram, evtTurnId, evtGen, rawEvt.reference === true);
      } catch (err) {
        // A malformed figure must never stop the lesson: teach on, text-only.
        console.error('[diagram_commit] render failed; continuing text-only', err);
      }
      break;
    }
    case 'page_commit': {
      // A page commit for another turn never touches the board.
      if (evtTurnId && refs.currentTurnId.current && evtTurnId !== refs.currentTurnId.current) return;
      applyPendingPage(deps, evtTurnId);
      ui.setHasDrawn(true);
      ui.setThinking(false);
      try {
        // Never instant: the RevealClock stages the commit and a reference figure reveals
        // everything at stage time (with its fade).
        executor.commitPage(rawEvt, false);
      } catch (err) {
        // A malformed page must never stop the lesson: teach on, text-only.
        console.error('[page_commit] render failed; continuing text-only', err);
      }
      break;
    }
    case 'step': {
      const step = normalizeStep(rawEvt.step);
      if (step.turnId && refs.currentTurnId.current && step.turnId !== refs.currentTurnId.current) return;
      applyPendingPage(deps, step.turnId);
      if (step.ops && step.ops.length > 0) {
        ui.setHasDrawn(true);
      }
      ui.setThinking(false);
      ui.setLocked(false);
      if (matcher) {
        matcher.enqueueStep(step);
      } else {
        for (const op of step.ops) executor.executeOp(op, evtGen, step.turnId);
      }
      if (deps.getAudioMode?.() === 'captions') ui.onCaptionStep?.(step);
      break;
    }
    case 'replay_from_step': {
      const stepIdx = rawEvt.stepIndex ?? rawEvt.step_index ?? 0;
      matcher?.replayFromStep(evtTurnId, stepIdx);
      matcher?.resume();               // replay follows a backchannel/hold, never an aside
      break;
    }
    case 'aside': {
      // Words spoken inside an aside (greeting, redirect, bridge, filler) never advance the
      // matcher.
      if (rawEvt.phase === 'start') matcher?.suspend();
      else matcher?.resume();
      break;
    }
    case 'turn_ended':
    case 'turn_cancelled': {
      // A doubt that ends without any content never wipes the board.
      if (pendingPages.get(executor)?.turnId === evtTurnId) pendingPages.delete(executor);
      if (evtType === 'turn_cancelled') {
        matcher?.turnCancelled(evtTurnId);
        matcher?.resume();
        executor.clearFocus(); // transient highlight must not outlive an interrupted turn
      } else if (evtType === 'turn_ended') {
        // A partial turn was cut by an LLM failure: only its already-heard steps may be drawn
        // (no flush dump); the pause line and Continue button handle the rest. A `pageOnly`
        // turn end is a chapter page boundary: the next page's steps arrive on their own, so
        // flushing here would dump the previous page's un-heard steps. In
        // captions mode the caption clock owns step completion, so flush would dump the board.
        if ((rawEvt.status ?? 'complete') === 'complete'
            && rawEvt.pageOnly !== true
            && deps.getAudioMode?.() !== 'captions') {
          matcher?.flushRemaining(evtTurnId);
        }
        // A turn end without an isLast step reveals every unrevealed group. Never on
        // turn_cancelled: an interrupted lesson keeps withholding its construction.
        executor.revealAll();
      }
      executor.reportFinal();  // cumulative repair report
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
    case 'conv_state': {
      ui.setCanContinue(rawEvt.canContinueLesson);
      break;
    }
    case 'audio_status': {
      ui.setAudioMode?.(rawEvt.mode === 'captions' ? 'captions' : 'voice');
      break;
    }
    case 'lesson_plan': {
      // Chapter chips. Not in GEN_EXEMPT: a superseded lesson's plan must never show.
      const pages: PageHeader[] = Array.isArray(rawEvt.pages) ? rawEvt.pages : [];
      ui.setLessonPlan?.(pages);
      break;
    }
    case 'session_ended': {
      // The job ended this session (a newer tab took the board): blocking message, no rejoin.
      ui.showSessionEnded(String(rawEvt.reason || 'ended'));
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
    case 'notice': {
      ui.showNotice(rawEvt.message);
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
    case 'question_draft': {
      // Photo -> text is a DRAFT for the student to confirm or edit; never submit it
      // automatically, OCR mistakes included.
      if (rawEvt.text) ui.setDraft(rawEvt.text);
      ui.setThinking(false);
      ui.setLocked(false);
      break;
    }
  }
}

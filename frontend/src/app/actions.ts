// frontend/src/app/actions.ts
// App action logic (continue, speed, photo upload) extracted for offline tests.

import { RpcAck, RpcHoldReason } from '../types/events';

import { API_BASE } from './endpoints';

export type RpcFn = (method: string, payload?: any) => Promise<any>;

export interface ActionUi {
  unblockAudio(): void;
  setThinking(v: boolean): void;
  setCanContinue(v: boolean): void;
  setCurrentSpeed(speed: 0.8 | 1.0 | 1.2): void;
  getCurrentSpeed(): 0.8 | 1.0 | 1.2;
  setOcrProcessing(v: boolean): void;
  setDraft(text: string | null): void;
  showNotice(message: string): void;
}

/** Retry once after 500 ms when the RPC call throws (timeout / transport). A returned
 *  `ok:false` is an answer, not a failure: never retried. */
export async function callRpcWithRetry(rpc: RpcFn, method: string, payload: any = {}): Promise<RpcAck | null> {
  try {
    return await rpc(method, payload);
  } catch {
    await new Promise((resolve) => setTimeout(resolve, 500));
    return await rpc(method, payload);
  }
}

export interface HoldDeps {
  rpc: RpcFn;
  registry: Set<string>;
  matcher: { hold(): void; release(): void } | null;
  setIsPaused(v: boolean): void;
}

/** Toggle one hold reason: the matcher releases only when the LAST hold goes. */
export async function setHold(deps: HoldDeps, reason: RpcHoldReason, armed: boolean): Promise<void> {
  if (armed === deps.registry.has(reason)) return;      // idempotent per reason
  const wasEmpty = deps.registry.size === 0;
  if (armed) deps.registry.add(reason);
  else deps.registry.delete(reason);
  if (armed && wasEmpty) deps.matcher?.hold();
  else if (!armed && deps.registry.size === 0) deps.matcher?.release();
  deps.setIsPaused(deps.registry.size > 0);
  await callRpcWithRetry(deps.rpc, armed ? 'hold' : 'release', { reason });
}

export async function continueLesson(rpc: RpcFn, ui: ActionUi): Promise<void> {
  console.log('[Continue lesson requested]');
  ui.unblockAudio();
  ui.setThinking(true);
  ui.setCanContinue(false);
  let ack: RpcAck | null = null;
  try {
    ack = await callRpcWithRetry(rpc, 'continue_lesson');
  } catch (err) {
    console.warn('[Continue lesson] RPC failed', err);
  }
  if (!ack?.ok) {
    ui.setThinking(false);
    if (ack?.reason === 'not_now') ui.showNotice('Let me finish this first.');
    else if (ack?.reason === 'nothing_to_continue') ui.setCanContinue(false);
  }
}

export async function changeSpeed(rpc: RpcFn, ui: ActionUi, speed: number): Promise<void> {
  const validSpeed = (speed === 0.8 || speed === 1.2 ? speed : 1.0) as 0.8 | 1.0 | 1.2;
  const previous = ui.getCurrentSpeed();
  ui.setCurrentSpeed(validSpeed);
  try {
    const ack = await rpc('set_speed', { speed: validSpeed });
    if (!ack?.ok) {
      ui.setCurrentSpeed(previous);
      if (ack?.reason === 'speed_unsupported') ui.showNotice("Speed can't be changed for this voice.");
    }
  } catch {
    ui.setCurrentSpeed(previous);
  }
}

export async function uploadPhoto(
  fetchFn: (url: string, init?: RequestInit) => Promise<Response>,
  blob: Blob,
  publish?: (data: Uint8Array) => Promise<void> | void,
): Promise<{ success: boolean; message?: string; text?: string } | undefined> {
  console.log('[Photo blob ready for upload]:', blob.size, 'bytes');

  try {
    // 1. Send to FastAPI /ocr endpoint
    const formData = new FormData();
    formData.append('file', blob, 'textbook_problem.jpg');

    const res = await fetchFn(`${API_BASE}/ocr`, {
      method: 'POST',
      body: formData,
    });

    if (res.ok) {
      const data = await res.json();
      if (data.success && data.text) {
        return { success: true, text: data.text };
      } else if (data.message) {
        console.warn('[OCR Notice]:', data.message);
        return { success: false, message: data.message };
      }
    } else {
      const errJson = await res.json().catch(() => null);
      const msg = errJson?.detail || `Upload failed with status ${res.status}`;
      return { success: false, message: msg };
    }
  } catch (err: any) {
    // Offline: the LiveKit image topic has no agent-side handler, so show an honest error
    // instead of a fake success.
    console.warn('[HTTP OCR request failed]:', err);
    return { success: false, message: 'Upload failed. Please check your connection and try again.' };
  }
}

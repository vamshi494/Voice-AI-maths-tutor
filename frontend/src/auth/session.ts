// frontend/src/auth/session.ts
// Name login: the only credential is a display name (a demo identity, not security). The server
// maps it to a stable user id and that user's latest board, so memory and lessons come back.

const NAME_KEY = 'tutor.userName';
const NEW_BOARD_KEY = 'tutor.newBoard';
export const NAME_MAX_LEN = 40;

/** Same cleaning the server applies: letters, digits, spaces and . ' - ; collapsed; capped. */
export function cleanName(raw: string): string {
  return raw.replace(/[^\p{L}\p{N} .'-]/gu, ' ').split(/\s+/).filter(Boolean).join(' ')
    .slice(0, NAME_MAX_LEN).trim();
}

export function getStoredName(): string | null {
  try {
    const v = localStorage.getItem(NAME_KEY);
    return v && cleanName(v) ? cleanName(v) : null;
  } catch {
    return null;
  }
}

export function storeName(name: string): void {
  try { localStorage.setItem(NAME_KEY, name); } catch { /* private mode: lasts this tab */ }
}

export function clearName(): void {
  try {
    localStorage.removeItem(NAME_KEY);
    localStorage.removeItem('tutor.boardId');     // legacy board-id key, cleared too
  } catch { /* ignore */ }
}

/** "New board" asks the server for a fresh board on the next connect (one-shot). */
export function requestNewBoard(): void {
  try { sessionStorage.setItem(NEW_BOARD_KEY, '1'); } catch { /* ignore */ }
}

export function takeNewBoardRequest(): boolean {
  try {
    const v = sessionStorage.getItem(NEW_BOARD_KEY) === '1';
    sessionStorage.removeItem(NEW_BOARD_KEY);
    return v;
  } catch {
    return false;
  }
}

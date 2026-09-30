// frontend/src/app/endpoints.ts
// Where the browser reaches the API and LiveKit.
//  * dev (vite on :3000): the local services.
//  * production build (docker deploy): SAME ORIGIN — the web server proxies /api -> FastAPI and
//    /livekit -> LiveKit signalling, so one https:// address works (the microphone needs a
//    secure context) and no CORS / extra ports are exposed to the browser for signalling.
//  * VITE_API_BASE / VITE_LIVEKIT_URL at build time override both.
const env = import.meta.env;

function sameOriginWs(path: string): string {
  if (typeof window === 'undefined') return `ws://localhost${path}`;
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}${path}`;
}

export const API_BASE: string = env.VITE_API_BASE || (env.DEV ? 'http://localhost:8000' : '/api');
export const LIVEKIT_URL: string =
  env.VITE_LIVEKIT_URL || (env.DEV ? 'ws://localhost:7880' : sameOriginWs('/livekit'));

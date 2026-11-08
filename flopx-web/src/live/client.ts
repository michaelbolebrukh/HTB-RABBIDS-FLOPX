// The FLOPX backend client: one base URL, one per-request timeout, JSON only.
//
// Base URL: VITE_API_BASE_URL at build time, else the production backend. The client never
// throws into React: every call resolves to a LiveResult, so a slow or failed read leaves the
// design's illustrative value on screen with its badge instead of a blank panel.

import { authHeaders, dropToken } from './token'

export const DEFAULT_API_BASE = 'https://flopx-backend.onrender.com'

/** Dev base: relative, so /flopx/* goes to the Vite dev server, which proxies it (vite.config.ts). */
export const DEV_PROXY_BASE = ''

export function apiBase(): string {
  const env = (import.meta as { env?: Record<string, string | boolean | undefined> }).env
  // In `vite` dev the browser origin is localhost:<port>, which the backend's CORS allowlist does
  // not admit, so dev goes through the proxy (which itself honours VITE_API_BASE_URL). Builds call
  // the backend directly. VITE_API_DIRECT=1 forces the direct call in dev.
  if (env?.DEV === true && env?.MODE !== 'test' && env?.VITE_API_DIRECT !== '1') return DEV_PROXY_BASE
  const raw = typeof env?.VITE_API_BASE_URL === 'string' ? env.VITE_API_BASE_URL.trim() : ''
  return (raw.length ? raw : DEFAULT_API_BASE).replace(/\/+$/, '')
}

/** Some reads take 2 to 6 seconds on the Render Starter plan; 12 s leaves room for a cold start. */
export const DEFAULT_TIMEOUT_MS = 12_000

export type LiveResult<T> =
  | { ok: true; data: T; ms: number }
  | { ok: false; error: string; status?: number; ms: number }

export interface FetchOpts {
  timeoutMs?: number
  method?: 'GET' | 'POST'
  body?: unknown
  /** Injected in tests. */
  fetchImpl?: typeof fetch
  base?: string
}

export async function fetchJson<T>(path: string, opts: FetchOpts = {}): Promise<LiveResult<T>> {
  const started = Date.now()
  const ctrl = new AbortController()
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  const url = `${opts.base ?? apiBase()}${path.startsWith('/') ? path : `/${path}`}`
  const f = opts.fetchImpl ?? fetch
  // A signed-in reader's token rides on every request (the backend widens some reads for it and
  // gates the paper account on it); a 401 for a request that carried one drops the token, once,
  // and the read fails like any other. Never a retry loop.
  const auth = authHeaders()
  const carried = 'authorization' in auth
  try {
    const res = await f(url, {
      method: opts.method ?? 'GET',
      headers: { ...(opts.body !== undefined ? { 'content-type': 'application/json', accept: 'application/json' } : { accept: 'application/json' }), ...auth },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
    })
    if (res.status === 401 && carried) dropToken()
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}`, status: res.status, ms: Date.now() - started }
    const data = (await res.json()) as T
    return { ok: true, data, ms: Date.now() - started }
  } catch (e) {
    const aborted = ctrl.signal.aborted
    return { ok: false, error: aborted ? `timeout after ${timeoutMs} ms` : e instanceof Error ? e.message : String(e), ms: Date.now() - started }
  } finally {
    clearTimeout(timer)
  }
}

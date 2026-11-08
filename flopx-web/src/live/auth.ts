// Sign-in for the demo: the backend's OAuth2 password form (POST /auth/token), header mode so the
// refresh token comes back in the body (a cross-site cookie from vercel.app to onrender.com does
// not work), a silent refresh before the access token expires, and sign-out. Tokens live in the
// token store (token.ts); the client attaches them to every read. The password goes into one
// request body and nowhere else: never stored, never logged, never prefilled.
//
// After a sign-in or sign-out the live modules are re-run (boot.ts reloadLive), so the reads the
// backend widens or gates on a token fetch again, and the pages re-render with what lands.
import { useSyncExternalStore } from 'react'
import { reloadLive } from './boot'
import { apiBase } from './client'
import { bump } from './store'
import { getTokens, hasToken, setTokens, subscribeTokens, type TokenSet } from './token'

export interface AuthOpts {
  fetchImpl?: typeof fetch
  base?: string
  /** Injected in tests; defaults to the live reload. */
  reload?: () => Promise<void>
  now?: () => number
}

/** The access token is refreshed this long before it expires. */
export const REFRESH_LEAD_MS = 60_000
/** Never schedule a refresh sooner than this (a token already inside the lead refreshes at once, once). */
export const REFRESH_MIN_DELAY_MS = 2_000

interface TokenWire {
  access_token?: unknown
  access_token_expiry?: unknown
  refresh_token?: unknown
  detail?: unknown
}

// ---------- UI state (busy flag, last error) ----------
const ui = { busy: false, error: null as string | null }
const listeners = new Set<() => void>()
function setUi(p: Partial<typeof ui>): void {
  Object.assign(ui, p)
  listeners.forEach((fn) => fn())
}

// ---------- The wire ----------
const JSON_HEADERS = { accept: 'application/json', 'x-auth-mode': 'header' }

async function post(path: string, init: { body?: URLSearchParams; bearer?: string }, opts: AuthOpts): Promise<Response> {
  const f = opts.fetchImpl ?? fetch
  const headers: Record<string, string> = { ...JSON_HEADERS }
  if (init.body) headers['content-type'] = 'application/x-www-form-urlencoded'
  if (init.bearer) headers.authorization = `Bearer ${init.bearer}`
  return f(`${opts.base ?? apiBase()}${path}`, { method: 'POST', headers, body: init.body })
}

/** The message the sign-in form shows for a failed token call. Never echoes the credentials. */
export function signInError(status: number, detail: unknown): string {
  // The backend answers 401 for a wrong password and 404 for an unknown email (its own wording
  // says so); the other statuses carry a detail worth showing as it is.
  if (status === 401) return 'Wrong email or password.'
  if (status === 404) return 'No account with this email.'
  if (status === 429) return 'Too many attempts. Wait a minute and try again.'
  if (status >= 500) return `FLOPX is not answering (HTTP ${status}). Try again shortly.`
  return typeof detail === 'string' && detail.trim() ? detail : `Sign-in failed (HTTP ${status}).`
}

function parseTokens(body: TokenWire, email: string, prev: TokenSet | null): TokenSet | null {
  if (typeof body.access_token !== 'string' || !body.access_token) return null
  const expiry = typeof body.access_token_expiry === 'string' ? body.access_token_expiry : new Date(Date.now() + 10 * 60_000).toISOString()
  return { accessToken: body.access_token, accessExpiry: expiry, refreshToken: typeof body.refresh_token === 'string' ? body.refresh_token : (prev?.refreshToken ?? null), email }
}

// ---------- Refresh timer ----------
let timer: ReturnType<typeof setTimeout> | null = null
let timerOpts: AuthOpts = {}

/** Milliseconds until the refresh should fire for a token expiring at `expiry`. */
export function refreshDelayMs(expiry: string, now = Date.now()): number {
  const exp = Date.parse(expiry)
  if (Number.isNaN(exp)) return REFRESH_MIN_DELAY_MS
  // setTimeout takes a 32-bit delay; a longer one would fire at once, not late (a timer past the
  // cap re-schedules itself from the token's expiry when it fires).
  return Math.min(2_147_483_647, Math.max(REFRESH_MIN_DELAY_MS, exp - now - REFRESH_LEAD_MS))
}

function clearRefresh(): void {
  if (timer) clearTimeout(timer)
  timer = null
}

export function scheduleRefresh(opts: AuthOpts = {}): number | null {
  clearRefresh()
  const t = getTokens()
  if (!t || !t.refreshToken) return null
  timerOpts = opts
  const now = opts.now?.() ?? Date.now()
  const delay = refreshDelayMs(t.accessExpiry, now)
  const due = Date.parse(t.accessExpiry) - REFRESH_LEAD_MS
  timer = setTimeout(() => {
    // Capped timer (see refreshDelayMs): not due yet, so wait again rather than refresh early.
    if (!Number.isNaN(due) && Date.now() < due - 1000) scheduleRefresh(timerOpts)
    else void refresh(timerOpts)
  }, delay)
  return delay
}

/**
 * Rotate: POST /auth/token/refresh with the refresh token as the bearer (header mode); the new
 * access token and the rotated refresh token come back in the body. A refusal drops the tokens and
 * re-runs the modules, so nothing gated stays on screen with a dead token.
 */
export async function refresh(opts: AuthOpts = {}): Promise<boolean> {
  const t = getTokens()
  if (!t || !t.refreshToken) return false
  let next: TokenSet | null = null
  try {
    const res = await post('/auth/token/refresh', { bearer: t.refreshToken }, opts)
    if (res.ok) next = parseTokens((await res.json()) as TokenWire, t.email, t)
    else if (res.status >= 500 || res.status === 429) {
      // A backend hiccup is not a revocation: keep the token and try again shortly.
      clearRefresh()
      timer = setTimeout(() => { void refresh(opts) }, 15_000)
      return false
    }
  } catch {
    clearRefresh()
    timer = setTimeout(() => { void refresh(opts) }, 15_000)
    return false
  }
  if (!next) {
    setTokens(null)
    clearRefresh()
    bump()
    void (opts.reload ?? reloadLive)()
    return false
  }
  setTokens(next)
  scheduleRefresh(opts)
  return true
}

// ---------- Sign in / out ----------
export async function signIn(email: string, password: string, opts: AuthOpts = {}): Promise<boolean> {
  const addr = email.trim()
  if (!addr || !password) {
    setUi({ error: 'Enter the account email and password.' })
    return false
  }
  setUi({ busy: true, error: null })
  let res: Response
  try {
    res = await post('/auth/token', { body: new URLSearchParams({ grant_type: 'password', username: addr, password }) }, opts)
  } catch (e) {
    setUi({ busy: false, error: `Could not reach FLOPX (${e instanceof Error ? e.message : 'network error'}).` })
    return false
  }
  let body: TokenWire = {}
  try { body = (await res.json()) as TokenWire } catch { /* no JSON body */ }
  if (!res.ok) {
    setUi({ busy: false, error: signInError(res.status, body.detail) })
    return false
  }
  const tokens = parseTokens(body, addr, null)
  if (!tokens) {
    setUi({ busy: false, error: 'FLOPX answered without a token.' })
    return false
  }
  setTokens(tokens)
  scheduleRefresh(opts)
  setUi({ busy: false, error: null })
  bump()
  void (opts.reload ?? reloadLive)()
  return true
}

/** Forget the tokens at once (the UI flips), tell the backend to revoke the session, re-run the modules. */
export async function signOut(opts: AuthOpts = {}): Promise<void> {
  const t = getTokens()
  setTokens(null)
  clearRefresh()
  setUi({ busy: false, error: null })
  bump()
  const reload = (opts.reload ?? reloadLive)()
  if (t?.refreshToken) {
    try { await post('/auth/logout', { bearer: t.refreshToken }, opts) } catch { /* the token is gone locally either way */ }
  }
  await reload
}

/**
 * On page load: a token set in sessionStorage (a reload mid-demo) is picked up, its refresh
 * scheduled, and an access token already past its expiry is refreshed before the modules read.
 */
export function initAuth(opts: AuthOpts = {}): void {
  const t = getTokens()
  if (!t) return
  if (!hasToken(opts.now?.() ?? Date.now())) void refresh(opts)
  else scheduleRefresh(opts)
}

// ---------- The hook ----------
export interface AuthView {
  signedIn: boolean
  email: string | null
  busy: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<boolean>
  signOut: () => Promise<void>
}

const actions = { signIn: (e: string, p: string) => signIn(e, p), signOut: () => signOut() }
let snapshot: AuthView | null = null
function getSnapshot(): AuthView {
  const t = getTokens()
  const next: AuthView = { signedIn: !!t, email: t?.email ?? null, busy: ui.busy, error: ui.error, ...actions }
  if (snapshot && snapshot.signedIn === next.signedIn && snapshot.email === next.email && snapshot.busy === next.busy && snapshot.error === next.error) return snapshot
  snapshot = next
  return next
}
function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  const off = subscribeTokens(fn)
  return () => { listeners.delete(fn); off() }
}

export function useAuth(): AuthView {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Test helper: forget UI state and timers (tokens are reset through token.ts). */
export function __resetAuth(): void {
  clearRefresh()
  ui.busy = false
  ui.error = null
  listeners.clear()
  snapshot = null
}

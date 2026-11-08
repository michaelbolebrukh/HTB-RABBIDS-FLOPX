// The token store: the one place the access token, its expiry and the refresh token live. Kept
// apart from the sign-in flow (auth.ts) so the client (client.ts) can attach the header and drop a
// rejected token without importing React or the sign-in code.
//
// Storage is sessionStorage (the demo machine; nothing survives a browser restart) behind a
// try/catch, with an in-memory copy as the source of truth, so a private window or a test runner
// without window storage behaves the same. Never log a token or a password.

export interface TokenSet {
  accessToken: string
  /** ISO instant the access token stops working. */
  accessExpiry: string
  refreshToken: string | null
  email: string
}

export const TOKEN_KEY = 'flopx.auth'

let current: TokenSet | null | undefined
const listeners = new Set<() => void>()

function storage(): Storage | null {
  try {
    const s = (globalThis as { sessionStorage?: Storage }).sessionStorage
    return s ?? null
  } catch {
    return null
  }
}

function readStored(): TokenSet | null {
  const s = storage()
  if (!s) return null
  try {
    const raw = s.getItem(TOKEN_KEY)
    if (!raw) return null
    const t = JSON.parse(raw) as Partial<TokenSet>
    if (typeof t.accessToken !== 'string' || typeof t.accessExpiry !== 'string' || typeof t.email !== 'string') return null
    return { accessToken: t.accessToken, accessExpiry: t.accessExpiry, refreshToken: typeof t.refreshToken === 'string' ? t.refreshToken : null, email: t.email }
  } catch {
    return null
  }
}

export function getTokens(): TokenSet | null {
  if (current === undefined) current = readStored()
  return current
}

export function setTokens(t: TokenSet | null): void {
  current = t
  const s = storage()
  if (s) {
    try {
      if (t) s.setItem(TOKEN_KEY, JSON.stringify(t))
      else s.removeItem(TOKEN_KEY)
    } catch { /* storage unavailable: the in-memory copy still serves this page */ }
  }
  listeners.forEach((fn) => fn())
}

export function subscribeTokens(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** True when an access token is held and has not passed its expiry. */
export function hasToken(now = Date.now()): boolean {
  const t = getTokens()
  if (!t) return false
  const exp = Date.parse(t.accessExpiry)
  return Number.isNaN(exp) || exp > now
}

/** Headers for a backend read: the bearer access token when one is held, else nothing. */
export function authHeaders(): Record<string, string> {
  const t = getTokens()
  return t ? { authorization: `Bearer ${t.accessToken}` } : {}
}

/** Called by the client on a 401 for a request that carried a token: the token is gone, no retry. */
export function dropToken(): void {
  if (getTokens()) setTokens(null)
}

/** Test helper. */
export function __resetTokens(): void {
  current = undefined
  listeners.clear()
  const s = storage()
  try { s?.removeItem(TOKEN_KEY) } catch { /* ignore */ }
}

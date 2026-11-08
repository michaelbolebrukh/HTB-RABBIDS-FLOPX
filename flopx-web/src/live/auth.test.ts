import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { __resetAuth, initAuth, refresh, refreshDelayMs, REFRESH_LEAD_MS, REFRESH_MIN_DELAY_MS, scheduleRefresh, signIn, signInError, signOut } from './auth'
import { fetchJson } from './client'
import { __resetLiveStore, getVersion } from './store'
import { __resetTokens, authHeaders, dropToken, getTokens, hasToken, setTokens, TOKEN_KEY } from './token'
import { chipLabel, signInKeyAction } from '@/components/SignIn'

// A recorded token fixture: the shape /auth/token answers in header mode. The token strings are
// not real credentials (three dot-separated base64 words), and no password appears anywhere here.
const TOKEN_FIXTURE = {
  access_token: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiIsInVzZSI6ImFjY2VzcyJ9.c2lnbmF0dXJl',
  access_token_expiry: '2026-11-08T13:00:00Z',
  refresh_token: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiIsInVzZSI6InJlZnJlc2gifQ.c2lnbmF0dXJl',
  token_type: 'bearer',
}

/** A sessionStorage stand-in for the node test runner. */
function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return {
    get length() { return m.size },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => { m.delete(k) },
    setItem: (k, v) => { m.set(k, String(v)) },
  }
}

type Call = { url: string; init: RequestInit }
function recorder(answer: (c: Call) => Response | Promise<Response>) {
  const calls: Call[] = []
  const f = vi.fn(async (url: string, init?: RequestInit) => {
    const c = { url, init: init ?? {} }
    calls.push(c)
    return answer(c)
  }) as unknown as typeof fetch
  return { f, calls }
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const headerOf = (init: RequestInit, name: string) => (init.headers as Record<string, string>)[name]

beforeEach(() => {
  ;(globalThis as { sessionStorage?: Storage }).sessionStorage = memoryStorage()
})
afterEach(() => {
  __resetAuth()
  __resetTokens()
  __resetLiveStore()
  delete (globalThis as { sessionStorage?: Storage }).sessionStorage
})

describe('token store', () => {
  it('keeps the token set in sessionStorage and answers authHeaders from it', () => {
    expect(authHeaders()).toEqual({})
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: 'r.s.t', email: 'demo@flopx.vercel.app' })
    expect(authHeaders()).toEqual({ authorization: 'Bearer a.b.c' })
    expect(JSON.parse(sessionStorage.getItem(TOKEN_KEY)!)).toMatchObject({ email: 'demo@flopx.vercel.app' })
    expect(hasToken()).toBe(true)
    expect(hasToken(Date.parse('2099-01-01T00:00:01Z'))).toBe(false)
    dropToken()
    expect(getTokens()).toBeNull()
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull()
  })
  it('survives a page reload: a stored token set is read back on first use', () => {
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify({ accessToken: 'x.y.z', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' }))
    __resetTokens()
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify({ accessToken: 'x.y.z', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' }))
    expect(getTokens()?.accessToken).toBe('x.y.z')
  })
})

describe('signIn', () => {
  it('posts the OAuth2 form in header mode, stores the tokens, bumps the live store and reloads the modules', async () => {
    const { f, calls } = recorder(() => json(TOKEN_FIXTURE))
    const reload = vi.fn(async () => {})
    const v0 = getVersion()
    const ok = await signIn('demo@flopx.vercel.app', 'not-the-real-one', { fetchImpl: f, base: 'http://x', reload })
    expect(ok).toBe(true)
    expect(calls[0].url).toBe('http://x/auth/token')
    expect(headerOf(calls[0].init, 'x-auth-mode')).toBe('header')
    expect(headerOf(calls[0].init, 'content-type')).toBe('application/x-www-form-urlencoded')
    const form = calls[0].init.body as URLSearchParams
    expect(form.get('username')).toBe('demo@flopx.vercel.app')
    expect(form.get('grant_type')).toBe('password')
    expect(getTokens()).toMatchObject({ accessToken: TOKEN_FIXTURE.access_token, refreshToken: TOKEN_FIXTURE.refresh_token, accessExpiry: TOKEN_FIXTURE.access_token_expiry, email: 'demo@flopx.vercel.app' })
    expect(getVersion()).toBeGreaterThan(v0)
    expect(reload).toHaveBeenCalledTimes(1)
    // The password is in the request body and nowhere else.
    expect(JSON.stringify(getTokens())).not.toContain('not-the-real-one')
    expect(sessionStorage.getItem(TOKEN_KEY)).not.toContain('not-the-real-one')
  })
  it('a wrong password leaves no token and a plain error; the modules are not re-run', async () => {
    const { f } = recorder(() => json({ detail: 'Incorrect email or password' }, 401))
    const reload = vi.fn(async () => {})
    const ok = await signIn('demo@flopx.vercel.app', 'wrong', { fetchImpl: f, base: 'http://x', reload })
    expect(ok).toBe(false)
    expect(getTokens()).toBeNull()
    expect(reload).not.toHaveBeenCalled()
  })
  it('maps the failure statuses to one honest line each', () => {
    expect(signInError(401, 'x')).toMatch(/Wrong email or password/)
    expect(signInError(404, "We couldn't find an account with this email.")).toMatch(/No account with this email/)
    expect(signInError(429, null)).toMatch(/Too many attempts/)
    expect(signInError(502, null)).toMatch(/HTTP 502/)
    expect(signInError(418, 'teapot')).toBe('teapot')
  })
})

describe('the client carries the token and drops it on a 401', () => {
  it('attaches Authorization when a token is held, and not otherwise', async () => {
    const { f, calls } = recorder(() => json({}))
    await fetchJson('/flopx/overview', { fetchImpl: f, base: 'http://x' })
    expect(headerOf(calls[0].init, 'authorization')).toBeUndefined()
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' })
    await fetchJson('/flopx/paper/account', { fetchImpl: f, base: 'http://x' })
    expect(headerOf(calls[1].init, 'authorization')).toBe('Bearer a.b.c')
  })
  it('a 401 for a request that carried the token drops it once, with no retry', async () => {
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: null, email: 'demo@flopx.vercel.app' })
    const { f, calls } = recorder(() => new Response('{"detail":"expired"}', { status: 401 }))
    const r = await fetchJson('/flopx/paper/account', { fetchImpl: f, base: 'http://x' })
    expect(r).toMatchObject({ ok: false, status: 401 })
    expect(calls).toHaveLength(1)
    expect(getTokens()).toBeNull()
    // Anonymous 401s do not touch the (absent) token and still do not retry.
    await fetchJson('/flopx/paper/account', { fetchImpl: f, base: 'http://x' })
    expect(calls).toHaveLength(2)
  })
})

describe('refresh and sign-out', () => {
  it('schedules the refresh a minute before expiry, never sooner than the floor', () => {
    const now = Date.parse('2026-11-08T12:00:00Z')
    expect(refreshDelayMs('2026-11-08T12:10:00Z', now)).toBe(10 * 60_000 - REFRESH_LEAD_MS)
    expect(refreshDelayMs('2026-11-08T12:00:30Z', now)).toBe(REFRESH_MIN_DELAY_MS)
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2026-11-08T12:10:00Z', refreshToken: 'r.s.t', email: 'e@x' })
    expect(scheduleRefresh({ now: () => now })).toBe(9 * 60_000)
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2026-11-08T12:10:00Z', refreshToken: null, email: 'e@x' })
    expect(scheduleRefresh({ now: () => now })).toBeNull()
  })
  it('rotates through /auth/token/refresh with the refresh token as the bearer', async () => {
    setTokens({ accessToken: 'old.a.b', accessExpiry: '2026-11-08T12:00:00Z', refreshToken: 'old.r.t', email: 'e@x' })
    const { f, calls } = recorder(() => json({ access_token: 'new.a.b', access_token_expiry: '2099-01-01T00:00:00Z', refresh_token: 'new.r.t', token_type: 'bearer' }))
    expect(await refresh({ fetchImpl: f, base: 'http://x' })).toBe(true)
    expect(calls[0].url).toBe('http://x/auth/token/refresh')
    expect(headerOf(calls[0].init, 'authorization')).toBe('Bearer old.r.t')
    expect(headerOf(calls[0].init, 'x-auth-mode')).toBe('header')
    expect(getTokens()).toMatchObject({ accessToken: 'new.a.b', refreshToken: 'new.r.t', email: 'e@x' })
  })
  it('a refused refresh drops the tokens and re-runs the modules', async () => {
    setTokens({ accessToken: 'old.a.b', accessExpiry: '2026-11-08T12:00:00Z', refreshToken: 'old.r.t', email: 'e@x' })
    const { f } = recorder(() => json({ detail: 'reuse' }, 401))
    const reload = vi.fn(async () => {})
    expect(await refresh({ fetchImpl: f, base: 'http://x', reload })).toBe(false)
    expect(getTokens()).toBeNull()
    expect(reload).toHaveBeenCalledTimes(1)
  })
  it('initAuth refreshes an expired stored token before the first reads', async () => {
    setTokens({ accessToken: 'old.a.b', accessExpiry: '2026-11-08T12:00:00Z', refreshToken: 'old.r.t', email: 'e@x' })
    const { f, calls } = recorder(() => json({ access_token: 'new.a.b', access_token_expiry: '2099-01-01T00:00:00Z', token_type: 'bearer' }))
    initAuth({ fetchImpl: f, base: 'http://x', now: () => Date.parse('2026-11-08T12:30:00Z') })
    await new Promise((r) => setTimeout(r, 0))
    expect(calls[0].url).toBe('http://x/auth/token/refresh')
    expect(getTokens()).toMatchObject({ accessToken: 'new.a.b', refreshToken: 'old.r.t' })
  })
  it('signOut forgets the tokens first, revokes with the refresh token, and re-runs the modules', async () => {
    setTokens({ accessToken: 'a.b.c', accessExpiry: '2099-01-01T00:00:00Z', refreshToken: 'r.s.t', email: 'e@x' })
    const { f, calls } = recorder(() => new Response(null, { status: 204 }))
    const reload = vi.fn(async () => {})
    const v0 = getVersion()
    await signOut({ fetchImpl: f, base: 'http://x', reload })
    expect(getTokens()).toBeNull()
    expect(calls[0].url).toBe('http://x/auth/logout')
    expect(headerOf(calls[0].init, 'authorization')).toBe('Bearer r.s.t')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(getVersion()).toBeGreaterThan(v0)
  })
})

describe('the dialog and the chip', () => {
  it('Enter submits, Escape closes, typing does neither', () => {
    expect(signInKeyAction('Enter')).toBe('submit')
    expect(signInKeyAction('Escape')).toBe('close')
    expect(signInKeyAction('a')).toBeNull()
    expect(signInKeyAction('Tab')).toBeNull()
  })
  it('the chip shows the full address where there is room and the local part where there is not', () => {
    expect(chipLabel('demo@flopx.vercel.app', false)).toBe('demo@flopx.vercel.app')
    expect(chipLabel('demo@flopx.vercel.app', true)).toBe('demo')
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchJson } from './client'
import { makeGetter, runModules, type LiveModule } from './boot'
import { __resetLiveStore, domain, getVersion, latestGeneratedAt } from './store'

afterEach(() => __resetLiveStore())

const okFetch = (body: unknown, delayMs = 0) =>
  vi.fn(async (_url: string, init?: RequestInit) => {
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(resolve, delayMs)
      init?.signal?.addEventListener('abort', () => {
        clearTimeout(t)
        reject(new DOMException('aborted', 'AbortError'))
      })
    })
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch

describe('fetchJson', () => {
  it('times out per request and resolves (never throws) with the reason', async () => {
    const r = await fetchJson('/slow', { fetchImpl: okFetch({}, 200), timeoutMs: 20, base: 'http://x' })
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toMatch(/timeout/)
  })
  it('reports HTTP errors', async () => {
    const f = vi.fn(async () => new Response('no', { status: 503 })) as unknown as typeof fetch
    const r = await fetchJson('/x', { fetchImpl: f, base: 'http://x' })
    expect(r).toMatchObject({ ok: false, status: 503 })
  })
})

describe('boot loader', () => {
  it('fetches each path once however many modules ask, and records generatedAt', async () => {
    const f = okFetch({ freshness: { generatedAt: '2026-11-08T00:39:13Z' } })
    const get = makeGetter({ fetchImpl: f, base: 'http://x' })
    await Promise.all([get('/flopx/overview'), get('/flopx/overview'), get('/flopx/gpu/index')])
    expect((f as unknown as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(2)
    expect(latestGeneratedAt()).toBe('2026-11-08T00:39:13Z')
  })

  it('retries a gateway error once and caps requests in flight', async () => {
    let calls = 0
    let inFlight = 0
    let peak = 0
    const f = vi.fn(async (url: string) => {
      calls++
      inFlight++
      peak = Math.max(peak, inFlight)
      await new Promise((r) => setTimeout(r, 5))
      inFlight--
      if (url.endsWith('/flaky') && calls === 1) return new Response('bad gateway', { status: 502 })
      return new Response('{}', { status: 200 })
    }) as unknown as typeof fetch
    const get = makeGetter({ fetchImpl: f, base: 'http://x', retryDelayMs: 1, maxInFlight: 2 })
    const flaky = await get('/flaky')
    expect(flaky.ok).toBe(true)
    await Promise.all(['/a', '/b', '/c', '/d', '/e'].map((p) => get(p)))
    expect(peak).toBeLessThanOrEqual(2)
  })

  it('runs modules in parallel; a failing module is marked failed without blocking the others', async () => {
    const applied: string[] = []
    const good: LiveModule = { id: 'good', load: async (get) => { const r = await get('/a'); applied.push('good'); return r.ok } }
    const bad: LiveModule = { id: 'bad', load: async () => { throw new Error('boom') } }
    const empty: LiveModule = { id: 'empty', load: async () => false }
    const v0 = getVersion()
    await runModules([good, bad, empty], { fetchImpl: okFetch({}), base: 'http://x' })
    expect(applied).toEqual(['good'])
    expect(domain('good').status).toBe('live')
    expect(domain('bad')).toMatchObject({ status: 'failed', error: 'boom' })
    expect(domain('empty').status).toBe('failed')
    expect(getVersion()).toBeGreaterThan(v0)
  })
})

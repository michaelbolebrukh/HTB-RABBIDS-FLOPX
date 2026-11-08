// The boot loader. Every live module declares what it needs and how to apply it; at start-up all
// modules run in parallel, each GET is fetched once however many modules ask for it, every request
// carries its own timeout, and each module applies (and re-renders) the moment its own data lands.
// A module that fails leaves the design's illustrative values in place; its domain state records
// why, so a badge or the ticker status can say "feed unavailable" instead of pretending.
import { fetchJson, type FetchOpts, type LiveResult } from './client'
import { bump, noteGeneratedAt, setDomain } from './store'
import { setAsOfFromGenerated } from './asof'

export type Getter = <T>(path: string, opts?: Pick<FetchOpts, 'timeoutMs'>) => Promise<LiveResult<T>>

export interface LiveModule {
  /** Domain id, e.g. 'gpu'. Shown in domain state; keep it stable. */
  id: string
  /**
   * Fetch through `get` (shared, de-duplicated, timed out) and mutate the design exports in place.
   * Throw, or return false, to mark the domain failed; the illustrative values then stay.
   * Call `apply()` part-way to re-render early when part of the data has landed.
   */
  load: (get: Getter, apply: () => void) => Promise<boolean | void>
}

export interface BootOpts {
  fetchImpl?: typeof fetch
  base?: string
}

/** Pull freshness.generatedAt off any payload that has one. */
function generatedAtOf(data: unknown): string | undefined {
  if (data && typeof data === 'object' && 'freshness' in data) {
    const f = (data as { freshness?: { generatedAt?: unknown } }).freshness
    if (f && typeof f.generatedAt === 'string') return f.generatedAt
  }
  return undefined
}

/**
 * At most this many requests in flight at once. The backend (Render Starter) has returned 502 for
 * every route under bursts of parallel boots; a small cap keeps the boot parallel without a spike.
 */
export const MAX_IN_FLIGHT = 6
/** One retry, after this delay, for gateway errors (502/503/504), which are usually a restart. */
export const RETRY_DELAY_MS = 3000

function limiter(max: number) {
  let active = 0
  const queue: (() => void)[] = []
  const next = () => {
    if (active >= max || !queue.length) return
    active++
    queue.shift()!()
  }
  return <T,>(task: () => Promise<T>): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      queue.push(() => task().then(resolve, reject).finally(() => { active--; next() }))
      next()
    })
}

export function makeGetter(opts: BootOpts & { retryDelayMs?: number; maxInFlight?: number } = {}): Getter {
  const cache = new Map<string, Promise<LiveResult<unknown>>>()
  const limit = limiter(opts.maxInFlight ?? MAX_IN_FLIGHT)
  const once = (path: string, o?: Pick<FetchOpts, 'timeoutMs'>) => limit(() => fetchJson<unknown>(path, { ...o, fetchImpl: opts.fetchImpl, base: opts.base }))
  const withRetry = async (path: string, o?: Pick<FetchOpts, 'timeoutMs'>) => {
    const r = await once(path, o)
    if (r.ok || !r.status || ![502, 503, 504].includes(r.status)) return r
    await new Promise((res) => setTimeout(res, opts.retryDelayMs ?? RETRY_DELAY_MS))
    return once(path, o)
  }
  return <T,>(path: string, o?: Pick<FetchOpts, 'timeoutMs'>) => {
    let p = cache.get(path)
    if (!p) {
      p = withRetry(path, o).then((r) => {
        if (r.ok) {
          const g = generatedAtOf(r.data)
          if (noteGeneratedAt(g)) setAsOfFromGenerated(g!)
        }
        return r
      })
      cache.set(path, p)
    }
    return p as Promise<LiveResult<T>>
  }
}

export async function runModules(modules: LiveModule[], opts: BootOpts = {}): Promise<void> {
  const get = makeGetter(opts)
  for (const m of modules) setDomain(m.id, { status: 'pending' })
  bump()
  await Promise.all(
    modules.map(async (m) => {
      const started = Date.now()
      try {
        const ok = await m.load(get, bump)
        setDomain(m.id, ok === false ? { status: 'failed', error: 'no usable data', ms: Date.now() - started } : { status: 'live', ms: Date.now() - started })
      } catch (e) {
        setDomain(m.id, { status: 'failed', error: e instanceof Error ? e.message : String(e), ms: Date.now() - started })
      }
      bump()
    }),
  )
}

let started = false
let booted: { modules: LiveModule[]; opts: BootOpts } | null = null

/** Called once from main.tsx. Never blocks the first paint. */
export function startLiveBoot(modules: LiveModule[], opts: BootOpts = {}): void {
  if (started) return
  started = true
  booted = { modules, opts }
  void runModules(modules, opts)
}

/**
 * Re-run every booted module with a fresh getter (new requests, new de-dup cache): the sign-in and
 * sign-out path, so the reads the backend widens or gates on a token are fetched again with the
 * token the client now holds (or no longer holds). The design values on screen stay until each
 * module's data lands again, as at boot. Resolves when every module has settled.
 */
export async function reloadLive(): Promise<void> {
  if (!booted) return
  await runModules(booted.modules, booted.opts)
}

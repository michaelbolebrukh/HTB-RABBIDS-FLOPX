import { useSyncExternalStore } from 'react'

/** Which panels the viewer has folded, as `page:panelId` keys. Persisted per browser; a convenience, never required state. */
const STORAGE_KEY = 'flopx.collapsed'

export const panelKey = (page: string, id: string) => `${page}:${id}`

export function toggleIn(keys: readonly string[], key: string): string[] {
  return keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key]
}

export function withoutPage(keys: readonly string[], page: string): string[] {
  return keys.filter((k) => !k.startsWith(`${page}:`))
}

function load(): string[] {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

let state: readonly string[] = load()
const subs = new Set<() => void>()

function commit(next: string[]) {
  state = next
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)) } catch { /* storage blocked: keep in memory */ }
  subs.forEach((f) => f())
}

export const isCollapsed = (page: string, id: string) => state.includes(panelKey(page, id))

export const collapse = {
  toggle: (page: string, id: string) => commit(toggleIn(state, panelKey(page, id))),
  resetPage: (page: string) => commit(withoutPage(state, page)),
  anyOnPage: (page: string) => state.some((k) => k.startsWith(`${page}:`)),
}

/** Re-renders when any panel folds or unfolds. */
export function useCollapsed(): readonly string[] {
  return useSyncExternalStore(
    (cb) => { subs.add(cb); return () => { subs.delete(cb) } },
    () => state,
    () => state,
  )
}

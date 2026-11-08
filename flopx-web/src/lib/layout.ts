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

/** Width in px of a panel folded sideways into a rail. */
export const RAIL = 40

/** Store id for a panel's sideways fold, kept apart from its up fold so the two toggle independently. */
export const sideKey = (id: string) => `${id}:side`

/**
 * CSS grid-template-columns for a row of panels. A number is a flexible weight (fr), a string a fixed
 * track ('380px'). Folded tracks shrink to a rail; the last track always stays open so the row never vanishes,
 * and once every flexible track is folded the open fixed tracks stretch to fill the row.
 */
export function rowTemplate(tracks: readonly (number | string)[], folded: readonly boolean[]): string {
  const all = tracks.length > 0 && tracks.every((_, i) => folded[i])
  const shut = tracks.map((_, i) => folded[i] && !(all && i === tracks.length - 1))
  // With every flexible track folded, open fixed tracks would leave blank space beside the rails; let them stretch.
  const stretch = shut.some(Boolean) && !tracks.some((t, i) => typeof t === 'number' && !shut[i])
  return tracks
    .map((t, i) => (shut[i] ? `${RAIL}px` : typeof t === 'number' || stretch ? `minmax(0,${typeof t === 'number' ? t : 1}fr)` : t))
    .join(' ')
}

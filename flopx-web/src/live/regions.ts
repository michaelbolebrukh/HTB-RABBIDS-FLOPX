// Names for backend region ids. Backend hubs come from /flopx/map/regions (e.g. 'us-va' is
// "US East and Midwest (Ashburn hub)"); Vast.ai offers carry ISO country codes ('tw', 'cz'),
// named through Intl.DisplayNames; 'global' and 'us' are offers whose provider does not publish a
// finer location. This module must not import data/core.ts (core imports it).

const hubNames = new Map<string, { name: string; short: string }>()

export function setHubNames(list: { id: string; name: string }[]): void {
  for (const r of list) hubNames.set(r.id, { name: r.name, short: shortOf(r.name, r.id) })
}

/** "US Central (Dallas hub)" -> "Dallas"; otherwise an upper-cased id. */
function shortOf(name: string, id: string): string {
  const m = name.match(/\(([^)]+?) hub\)/)
  return m ? m[1] : id.toUpperCase()
}

const SPECIAL: Record<string, string> = {
  global: 'Location not published',
  us: 'United States (location not published)',
}

let displayNames: Intl.DisplayNames | null | undefined
function countryName(code: string): string | undefined {
  if (!/^[a-z]{2}$/i.test(code)) return undefined
  if (displayNames === undefined) {
    try {
      displayNames = new Intl.DisplayNames(['en-GB'], { type: 'region' })
    } catch {
      displayNames = null
    }
  }
  const n = displayNames?.of(code.toUpperCase())
  return n && n !== code.toUpperCase() ? n : undefined
}

export function liveRegionName(id: string): string {
  return hubNames.get(id)?.name ?? SPECIAL[id] ?? countryName(id) ?? id
}

export function liveRegionShort(id: string): string {
  const h = hubNames.get(id)
  if (h) return h.short
  if (id === 'global') return 'n/p'
  return id.toUpperCase()
}

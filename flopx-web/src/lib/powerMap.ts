import type { Project, Stage } from '@/data/infra'

/** Day-ahead power above this ($/MWh) is shown as expensive. */
export const POWER_HOT = 90

export const powerTone = (usdPerMWh: number): 'down' | 'bone' => (usdPerMWh > POWER_HOT ? 'down' : 'bone')

/** Most built first, so a region's pips read left to right from real capacity to paper capacity. */
export const STAGE_ORDER: Stage[] = ['operational', 'construction', 'proposed', 'announced']

/** Each region's facility stages, most built first. Regions with no facilities are absent. */
export function stagesByRegion(projects: readonly Pick<Project, 'region' | 'stage'>[]): Map<string, Stage[]> {
  const out = new Map<string, Stage[]>()
  for (const p of projects) out.set(p.region, [...(out.get(p.region) ?? []), p.stage])
  for (const s of out.values()) s.sort((a, b) => STAGE_ORDER.indexOf(a) - STAGE_ORDER.indexOf(b))
  return out
}

/** At most `max` pips; the rest collapse into a "+n" count. */
export function capPips(stages: readonly Stage[], max = 4): { shown: Stage[]; more: number } {
  return { shown: stages.slice(0, max), more: Math.max(0, stages.length - max) }
}

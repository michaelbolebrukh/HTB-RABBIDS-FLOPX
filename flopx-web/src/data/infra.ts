import { shaped, type Evidence, type Freshness } from './core'

export type Stage = 'operational' | 'construction' | 'proposed' | 'announced'

export interface Milestone {
  label: string
  date: string
  status: 'done' | 'slipped' | 'pending' | 'at-risk'
  source?: string
  originally?: string
}

export interface Project {
  id: string
  name: string
  region: string
  lat: number
  lon: number
  developer: string
  developerTicker?: string
  tenant?: string
  stage: Stage
  reportedMW: number
  energizedMW: number
  expectedOnline: string
  effectiveRange: [number, number] // inferred MW usable by Mar 2027
  onTimeProb: number
  gridZone: string
  marketplaceRegion: string
  milestones: Milestone[]
  evidence: Evidence[]
  uncertainties: string[]
  impact: string
}

// The design's projects are synthetic (the backend's facility records are sample data too), so
// every project evidence row is illustrative whatever the design shipped (src/live/README.md, step 5).
const ev = (id: string, label: string, value: string, source: string, time: string, extra: Partial<Evidence> = {}): Evidence => ({
  id, label, value, source, time, truth: 'obs', weight: 0.2, direction: 'flat', ...extra, freshness: 'illustrative',
})

export const PROJECTS: Project[] = [
  {
    id: 'red-plains', name: 'Red Plains Campus Ph.2', region: 'us-tx', lat: 32.45, lon: -99.73, developer: 'Applied Digital (illustr.)', developerTicker: 'APLD', tenant: 'Undisclosed neocloud', stage: 'construction',
    reportedMW: 600, energizedMW: 0, expectedOnline: 'Q4 2026 (was Q2 2026)', effectiveRange: [120, 380], onTimeProb: 0.38, gridZone: 'ERCOT West', marketplaceRegion: 'US-Central',
    milestones: [
      { label: 'Announced', date: 'Mar 2024', status: 'done', source: 'Press release' },
      { label: 'Land + permits', date: 'Sep 2024', status: 'done', source: 'County filings' },
      { label: 'Interconnect agreement', date: 'Feb 2025', status: 'done', source: 'ERCOT LLIS list' },
      { label: 'Substation complete', date: 'Sep 2026', status: 'slipped', originally: 'Feb 2026', source: 'Developer Q4 call' },
      { label: 'First 200 MW energised', date: 'Q4 2026', status: 'at-risk', originally: 'Q2 2026' },
      { label: 'Full 600 MW', date: 'H2 2027', status: 'pending' },
    ],
    evidence: [
      ev('rp1', 'Substation completion', 'Feb → Sep 2026', 'Developer Q4-25 earnings call', '24 Feb 2026', { direction: 'up', weight: 0.4 }),
      ev('rp2', 'Transformer delivery', '≥ 40-week lead time', 'Industry supplier note', '10 Jan 2026', { weight: 0.2 }),
      ev('rp3', 'Satellite construction activity', 'Steel up on 3 of 4 halls', 'Commercial imagery (monthly)', '01 Mar 2026', { freshness: 'historical', weight: 0.2 }),
      ev('rp4', 'Tenant contract', 'Signed — 15-yr lease (reported)', 'Press release', '08 Jan 2026', { weight: 0.2 }),
    ],
    uncertainties: ['Substation date relies on a single developer statement', 'Tenant GPU delivery schedule unknown', 'ERCOT large-load rules may change mid-2026'],
    impact: 'If energisation slips to Q4, FLOPX estimates TX effective supply in Q3 2026 is 9–16% lower than announced capacity implies; regional H100 premium forecast +4–7 pp.',
  },
  {
    id: 'lone-star', name: 'Lone Star Compute Park', region: 'us-tx', lat: 31.9, lon: -102.1, developer: 'Private (illustr.)', stage: 'proposed', reportedMW: 1200, energizedMW: 0, expectedOnline: '2028 (developer claim)', effectiveRange: [0, 60], onTimeProb: 0.12, gridZone: 'ERCOT West', marketplaceRegion: 'US-Central',
    milestones: [{ label: 'Announced', date: 'Nov 2025', status: 'done' }, { label: 'Interconnect request', date: 'Dec 2025', status: 'done' }, { label: 'Permits', date: '—', status: 'pending' }, { label: 'Groundbreak', date: '—', status: 'pending' }],
    evidence: [ev('ls1', 'Queue entry', '1.2 GW large-load request', 'ERCOT queue', '15 Dec 2025')],
    uncertainties: ['No financing disclosed', 'No tenant', 'Gas-turbine bridge power unconfirmed'], impact: 'Negligible to 2027 effective supply. Announced MW should not be read as compute.',
  },
  {
    id: 'dfw-op', name: 'DFW-07 Campus', region: 'us-tx', lat: 32.9, lon: -96.9, developer: 'DC operator (illustr.)', developerTicker: 'DLR', stage: 'operational', reportedMW: 180, energizedMW: 180, expectedOnline: 'Online since 2023', effectiveRange: [170, 180], onTimeProb: 1, gridZone: 'ERCOT North', marketplaceRegion: 'US-Central',
    milestones: [{ label: 'Operational', date: 'Jun 2023', status: 'done' }], evidence: [ev('dfw1', 'Operational status', 'Fully leased', 'Supplemental Q4-25', '20 Feb 2026')], uncertainties: [], impact: 'Already part of observed supply.',
  },
  {
    id: 'ash-21', name: 'Ashburn DC-21', region: 'us-va', lat: 39.04, lon: -77.49, developer: 'DC operator (illustr.)', developerTicker: 'EQIX', stage: 'construction', reportedMW: 240, energizedMW: 60, expectedOnline: 'Q3 2026', effectiveRange: [140, 220], onTimeProb: 0.61, gridZone: 'PJM Dominion', marketplaceRegion: 'US-East',
    milestones: [{ label: 'Announced', date: 'Jan 2024', status: 'done' }, { label: 'First 60 MW', date: 'Dec 2025', status: 'done' }, { label: 'Remaining 180 MW', date: 'Q3 2026', status: 'pending' }],
    evidence: [ev('a1', 'Phase 1 energised', '60 MW', 'Operator filing', '05 Jan 2026')], uncertainties: ['Dominion transmission upgrade timing'], impact: 'Moderately relieves NoVA pressure from Q4 2026.',
  },
  {
    id: 'prince-w', name: 'Prince William Digital Gateway', region: 'us-va', lat: 38.8, lon: -77.6, developer: 'Consortium (illustr.)', stage: 'proposed', reportedMW: 2000, energizedMW: 0, expectedOnline: '2029+', effectiveRange: [0, 0], onTimeProb: 0.1, gridZone: 'PJM Dominion', marketplaceRegion: 'US-East',
    milestones: [{ label: 'Rezoning', date: 'Dec 2023', status: 'done' }, { label: 'Legal challenge', date: '2025–', status: 'at-risk' }],
    evidence: [ev('pw1', 'Court ruling', 'Rezoning challenged', 'County court records', '12 Feb 2026', { direction: 'up' })], uncertainties: ['Litigation outcome'], impact: 'None before 2028.',
  },
  {
    id: 'mesa-az', name: 'Mesa East Campus', region: 'us-az', lat: 33.4, lon: -111.6, developer: 'Hyperscaler (illustr.)', developerTicker: 'MSFT', stage: 'construction', reportedMW: 300, energizedMW: 100, expectedOnline: 'Q2 2026', effectiveRange: [220, 290], onTimeProb: 0.72, gridZone: 'SRP', marketplaceRegion: 'US-West',
    milestones: [{ label: 'Phase 1', date: 'Oct 2025', status: 'done' }, { label: 'Phase 2', date: 'Q2 2026', status: 'pending' }], evidence: [ev('m1', 'Phase 1 energised', '100 MW', 'Utility filing', '30 Oct 2025')], uncertainties: ['Water permit renewal'], impact: 'Hyperscaler self-use; limited effect on rental market.',
  },
  {
    id: 'fjord-1', name: 'Fjordlys Hall 3', region: 'no-nordics', lat: 60.4, lon: 5.3, developer: 'Neocloud (illustr.)', developerTicker: 'NBIS', stage: 'operational', reportedMW: 75, energizedMW: 75, expectedOnline: 'Online Jan 2026 (on time)', effectiveRange: [70, 75], onTimeProb: 1, gridZone: 'NO5', marketplaceRegion: 'EU-North',
    milestones: [{ label: 'Announced', date: 'Feb 2024', status: 'done' }, { label: 'Energised', date: 'Jan 2026', status: 'done' }], evidence: [ev('f1', 'Energised on schedule', '75 MW', 'Company update', '15 Jan 2026', { direction: 'down' })], uncertainties: [], impact: 'Added supply is loosening Nordic rental prices.',
  },
  {
    id: 'lulea-x', name: 'Norrbotten AI Park', region: 'no-nordics', lat: 65.6, lon: 22.1, developer: 'Private (illustr.)', stage: 'construction', reportedMW: 250, energizedMW: 0, expectedOnline: 'Q1 2027', effectiveRange: [150, 240], onTimeProb: 0.66, gridZone: 'SE1', marketplaceRegion: 'EU-North',
    milestones: [{ label: 'Groundbreak', date: 'Jun 2025', status: 'done' }, { label: 'Energise', date: 'Q1 2027', status: 'pending' }], evidence: [ev('l1', 'Construction progress', 'On schedule', 'Municipal update', '20 Feb 2026')], uncertainties: ['Tenant not disclosed'], impact: 'Further loosening expected in 2027.',
  },
  {
    id: 'johor-7', name: 'Johor Sedenak Block 7', region: 'sg', lat: 1.64, lon: 103.6, developer: 'DC operator (illustr.)', stage: 'construction', reportedMW: 400, energizedMW: 80, expectedOnline: 'Q4 2026', effectiveRange: [200, 340], onTimeProb: 0.55, gridZone: 'TNB Johor', marketplaceRegion: 'APAC-SE',
    milestones: [{ label: 'Phase 1', date: 'Nov 2025', status: 'done' }, { label: 'Phase 2', date: 'Q4 2026', status: 'pending' }], evidence: [ev('j1', 'Water + power approvals', 'Tightened criteria', 'State regulator', '02 Feb 2026', { direction: 'up' })], uncertainties: ['New Johor approval criteria'], impact: 'Main relief valve for Singapore pressure.',
  },
  {
    id: 'dub-x', name: 'Grange Castle Ext.', region: 'ie-dub', lat: 53.32, lon: -6.43, developer: 'Hyperscaler (illustr.)', stage: 'announced', reportedMW: 150, energizedMW: 0, expectedOnline: 'Unknown — grid connection not granted', effectiveRange: [0, 20], onTimeProb: 0.08, gridZone: 'EirGrid Dublin', marketplaceRegion: 'EU-West',
    milestones: [{ label: 'Announced', date: 'Apr 2025', status: 'done' }, { label: 'Grid connection', date: '—', status: 'at-risk' }], evidence: [ev('d1', 'Grid connection policy', 'Restricted in Dublin region', 'EirGrid statement', '2022–present')], uncertainties: ['Policy may change in 2026 review'], impact: 'Announced MW is unlikely to become usable capacity.',
  },
  {
    id: 'abu-1', name: 'Saadiyat Compute Campus', region: 'ae-uae', lat: 24.5, lon: 54.6, developer: 'Sovereign consortium (illustr.)', stage: 'announced', reportedMW: 1000, energizedMW: 0, expectedOnline: '2026–2027 (announced)', effectiveRange: [100, 600], onTimeProb: 0.35, gridZone: 'n/a', marketplaceRegion: 'MEA',
    milestones: [{ label: 'Announced', date: 'May 2025', status: 'done' }, { label: 'First phase', date: '2026', status: 'pending' }], evidence: [ev('ab1', 'Announcement', '1 GW (headline)', 'Government press release', '15 May 2025')], uncertainties: ['No independent progress data', 'Export-licence dependency for GPUs'], impact: 'Wide range — experimental coverage.',
  },
  {
    id: 'qc-hydro', name: 'Beauharnois HPC', region: 'ca-qc', lat: 45.3, lon: -73.9, developer: 'Neocloud (illustr.)', stage: 'operational', reportedMW: 90, energizedMW: 90, expectedOnline: 'Online', effectiveRange: [85, 90], onTimeProb: 1, gridZone: 'Hydro-Québec', marketplaceRegion: 'CA-East',
    milestones: [{ label: 'Operational', date: 'Aug 2025', status: 'done' }], evidence: [ev('q1', 'Online', '90 MW', 'Company', 'Aug 2025')], uncertainties: [], impact: 'Discounted capacity relative to US-East.',
  },
]
export const projectById = (id: string) => PROJECTS.find((p) => p.id === id)

export interface EnergyMarket {
  id: string
  name: string
  operator: string
  currency: string
  price: number[] // per MWh monthly, in `currency`
  /** Which month slots carry a backend value (src/live/dates.ts placeOnAxis); absent = none. */
  real?: boolean[]
  /** True when the design months without a backend value were scaled to join the backend ones (G14). */
  scaled?: boolean
  priceNow: number
  /** 30-day change in %; NaN when the feed does not report one (pages show a dash). */
  chg30: number
  scarcityHours: number | null
  reserveMargin: number | null
  queueGW: number | null
  constraint: string
  /** 'illustrative' until a backend zone lands for this market (design values are synthetic). */
  freshness: Freshness
  regions: string[]
  /** Backend provenance note for a market fed live (src/live/adapt-w3.ts). */
  sourceNote?: string
}

const em = (id: string, name: string, operator: string, keys: [number, number][], seed: number, rest: Omit<EnergyMarket, 'id' | 'name' | 'operator' | 'price' | 'priceNow' | 'currency'>): EnergyMarket => {
  const price = shaped(seed, keys, 27, 4)
  // Every design market is synthetic until the live energy module replaces it.
  return { id, name, operator, currency: 'USD', price, priceNow: price[26], ...rest, freshness: 'illustrative' }
}

export const ENERGY: EnergyMarket[] = [
  em('ercot', 'ERCOT', 'Electric Reliability Council of Texas', [[0, 38], [7, 62], [12, 41], [19, 70], [24, 44], [26, 58]], 41, { chg30: 21.4, scarcityHours: 37, reserveMargin: 11.8, queueGW: 182, constraint: 'West-to-North transmission congestion; large-load interconnect rules under review', freshness: 'delayed', regions: ['us-tx'] }),
  em('pjm', 'PJM', 'PJM Interconnection', [[0, 48], [12, 55], [20, 74], [26, 71]], 42, { chg30: 3.2, scarcityHours: 12, reserveMargin: 14.6, queueGW: 96, constraint: 'Dominion zone transmission limits; capacity auction clearing at record', freshness: 'delayed', regions: ['us-va'] }),
  em('wecc-az', 'AZ (SRP/APS)', 'Salt River Project / APS', [[0, 42], [26, 50]], 43, { chg30: 1.1, scarcityHours: 4, reserveMargin: 16, queueGW: 61, constraint: 'Summer peak; water constraints on cooling', freshness: 'historical', regions: ['us-az'] }),
  em('bpa', 'BPA / Mid-C', 'Bonneville Power Administration', [[0, 35], [26, 38]], 44, { chg30: -2.4, scarcityHours: 0, reserveMargin: 19, queueGW: 34, constraint: 'Hydro-dependent; low-water years raise prices', freshness: 'historical', regions: ['us-or'] }),
  em('hq', 'Hydro-Québec', 'Hydro-Québec', [[0, 30], [26, 31]], 45, { chg30: 0, scarcityHours: 0, reserveMargin: null, queueGW: 12, constraint: 'Allocation-based for large loads', freshness: 'historical', regions: ['ca-qc'] }),
  em('sem', 'I-SEM', 'EirGrid / SONI', [[0, 130], [12, 105], [26, 112]], 46, { chg30: 4.0, scarcityHours: 22, reserveMargin: 7.1, queueGW: 8, constraint: 'Dublin data-centre grid connections restricted', freshness: 'delayed', regions: ['ie-dub'] }),
  em('gb', 'GB (N2EX)', 'NESO', [[0, 101], [26, 96]], 47, { chg30: -1.2, scarcityHours: 8, reserveMargin: 9.4, queueGW: 41, constraint: 'West London connection queue to 2030s', freshness: 'delayed', regions: ['uk-lon'] }),
  em('nl', 'NL (EPEX)', 'TenneT NL', [[0, 95], [26, 84]], 48, { chg30: -3.0, scarcityHours: 5, reserveMargin: null, queueGW: 19, constraint: 'Grid congestion map: no new large loads in N-Holland', freshness: 'delayed', regions: ['nl-ams'] }),
  em('de', 'DE (EPEX)', 'Amprion / TenneT DE', [[0, 92], [26, 89]], 49, { chg30: -0.8, scarcityHours: 6, reserveMargin: null, queueGW: 27, constraint: 'Frankfurt connection wait 4–6 years', freshness: 'delayed', regions: ['de-fra'] }),
  em('nordpool', 'Nord Pool NO/SE', 'Statnett / Svenska kraftnät', [[0, 42], [12, 35], [26, 30]], 50, { chg30: -8.2, scarcityHours: 0, reserveMargin: null, queueGW: 22, constraint: 'Ample hydro; northern zones export-limited', freshness: 'delayed', regions: ['no-nordics'] }),
  em('sg', 'Singapore USEP', 'EMA', [[0, 140], [26, 132]], 51, { chg30: 0.4, scarcityHours: 3, reserveMargin: null, queueGW: 6, constraint: 'DC capacity allocation programme (limited)', freshness: 'historical', regions: ['sg'] }),
  em('jepx', 'JEPX', 'JEPX', [[0, 110], [26, 101]], 52, { chg30: -1.4, scarcityHours: 2, reserveMargin: null, queueGW: 14, constraint: 'Tokyo-area substation capacity', freshness: 'delayed', regions: ['jp-tyo'] }),
  em('aemo', 'NEM NSW', 'AEMO', [[0, 85], [26, 92]], 53, { chg30: 2.0, scarcityHours: 9, reserveMargin: null, queueGW: 18, constraint: 'Coal retirements; transmission build delays', freshness: 'historical', regions: ['au-syd'] }),
]
export const energyById = (id: string) => ENERGY.find((e) => e.id === id)

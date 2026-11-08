/** Price-pane chart types for StockChart, grouped as the dropdown shows them. */
export const CHART_TYPE_GROUPS = [
  [
    { id: 'bars', label: 'Bars' },
    { id: 'candles', label: 'Candles' },
    { id: 'hollow', label: 'Hollow candles' },
    { id: 'volcandles', label: 'Volume candles' },
    { id: 'heikin', label: 'Heikin Ashi' },
  ],
  [
    { id: 'line', label: 'Line' },
    { id: 'markers', label: 'Line with markers' },
    { id: 'step', label: 'Step line' },
  ],
  [
    { id: 'area', label: 'Area' },
    { id: 'hlc', label: 'HLC area' },
    { id: 'baseline', label: 'Baseline' },
  ],
  [
    { id: 'columns', label: 'Columns' },
    { id: 'highlow', label: 'High-low' },
  ],
] as const

export type ChartType = (typeof CHART_TYPE_GROUPS)[number][number]['id']

export const CHART_TYPES = CHART_TYPE_GROUPS.flat()

/** Types that draw the OHLC readout (O H L C); the rest show the line readout. */
export const OHLC_TYPES: ReadonlySet<ChartType> = new Set(['bars', 'candles', 'hollow', 'volcandles', 'heikin', 'hlc', 'highlow'])

/** Types that draw the `lines` prop (primary CPX line + benchmarks) rather than candle data. */
export const LINE_TYPES: ReadonlySet<ChartType> = new Set(['line', 'markers', 'step'])

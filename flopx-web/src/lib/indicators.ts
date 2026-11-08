/** Technical indicators for StockChart: catalogue (this file) + math (indicatorMath.ts). */

export type IndicatorCategory = 'Moving averages' | 'Bands & channels' | 'Momentum' | 'Volatility' | 'Volume' | 'Trend'

export interface IndicatorDef {
  id: IndicatorId
  label: string
  /** Short tag for chips and pane titles, with default params, e.g. 'SMA 20'. */
  short: string
  category: IndicatorCategory
  /** 'overlay' draws on the price pane in price units; 'pane' gets its own pane under the price. */
  placement: 'overlay' | 'pane'
  description: string
}

export const INDICATORS = [
  { id: 'sma', label: 'Simple moving average', short: 'SMA 20', category: 'Moving averages', placement: 'overlay', description: 'Mean close over the last 20 bars.' },
  { id: 'ema', label: 'Exponential moving average', short: 'EMA 20', category: 'Moving averages', placement: 'overlay', description: 'Close smoothed with weight 2/(20+1) on the newest bar.' },
  { id: 'wma', label: 'Weighted moving average', short: 'WMA 20', category: 'Moving averages', placement: 'overlay', description: 'Linearly weighted mean close, newest bar heaviest.' },
  { id: 'ma_cross', label: 'MA cross (10 / 30)', short: 'MA 10/30', category: 'Moving averages', placement: 'overlay', description: 'Fast and slow simple moving averages; crossings mark trend changes.' },
  { id: 'bb', label: 'Bollinger Bands', short: 'BB 20 2', category: 'Bands & channels', placement: 'overlay', description: 'SMA 20 with bands at ±2 standard deviations.' },
  { id: 'keltner', label: 'Keltner channels', short: 'KC 20 2', category: 'Bands & channels', placement: 'overlay', description: 'EMA 20 with bands at ±2 × ATR 10.' },
  { id: 'donchian', label: 'Donchian channels', short: 'DC 20', category: 'Bands & channels', placement: 'overlay', description: 'Highest high and lowest low over 20 bars, with midline.' },
  { id: 'psar', label: 'Parabolic SAR', short: 'SAR', category: 'Trend', placement: 'overlay', description: 'Trailing stop-and-reverse dots (step 0.02, max 0.2).' },
  { id: 'vwap', label: 'Anchored VWAP', short: 'VWAP', category: 'Volume', placement: 'overlay', description: 'Volume-weighted typical price anchored at the first bar.' },
  { id: 'rsi', label: 'Relative strength index', short: 'RSI 14', category: 'Momentum', placement: 'pane', description: 'Wilder RSI over 14 bars; 70 / 30 guides.' },
  { id: 'macd', label: 'MACD', short: 'MACD 12 26 9', category: 'Momentum', placement: 'pane', description: 'EMA 12 − EMA 26, signal EMA 9, histogram.' },
  { id: 'stoch', label: 'Stochastic', short: 'Stoch 14 3', category: 'Momentum', placement: 'pane', description: '%K over 14 bars, %D = SMA 3 of %K; 80 / 20 guides.' },
  { id: 'cci', label: 'Commodity channel index', short: 'CCI 20', category: 'Momentum', placement: 'pane', description: 'Typical price vs its SMA 20 over mean deviation; ±100 guides.' },
  { id: 'willr', label: 'Williams %R', short: '%R 14', category: 'Momentum', placement: 'pane', description: 'Close within the 14-bar range, −100 to 0; −20 / −80 guides.' },
  { id: 'roc', label: 'Rate of change', short: 'ROC 12', category: 'Momentum', placement: 'pane', description: '% change of close vs 12 bars ago.' },
  { id: 'aroon', label: 'Aroon', short: 'Aroon 14', category: 'Trend', placement: 'pane', description: 'Bars since 14-bar high / low, as Aroon up and down (0–100).' },
  { id: 'adx', label: 'Average directional index', short: 'ADX 14', category: 'Trend', placement: 'pane', description: 'Trend strength (ADX) with +DI and −DI, Wilder 14.' },
  { id: 'atr', label: 'Average true range', short: 'ATR 14', category: 'Volatility', placement: 'pane', description: 'Wilder average of true range over 14 bars.' },
  { id: 'hv', label: 'Historical volatility', short: 'HV 20', category: 'Volatility', placement: 'pane', description: 'Annualised stdev of weekly log returns over 20 bars (×√52), in %.' },
  { id: 'obv', label: 'On-balance volume', short: 'OBV', category: 'Volume', placement: 'pane', description: 'Cumulative volume signed by the close direction.' },
  { id: 'ad', label: 'Accumulation/distribution', short: 'A/D', category: 'Volume', placement: 'pane', description: 'Cumulative close-location value × volume.' },
  { id: 'mfi', label: 'Money flow index', short: 'MFI 14', category: 'Volume', placement: 'pane', description: 'Volume-weighted RSI of typical price over 14 bars; 80 / 20 guides.' },
] as const satisfies readonly (Omit<IndicatorDef, 'id'> & { id: string })[]

export type IndicatorId = (typeof INDICATORS)[number]['id']

export const indicatorById = (id: IndicatorId): IndicatorDef => INDICATORS.find((d) => d.id === id)! as IndicatorDef

/** One drawable series from an indicator. Values align 1:1 with the candles; null = not yet defined (warm-up). */
export interface IndicatorLine {
  id: string
  label: string
  values: (number | null)[]
  /** CSS colour, use theme vars (var(--color-fct), var(--color-inf), var(--color-dec), var(--color-warn), var(--color-up), var(--color-down), var(--color-mute)). */
  color: string
  kind?: 'line' | 'dashed' | 'dots' | 'histogram'
}

export interface IndicatorResult {
  id: IndicatorId
  lines: IndicatorLine[]
  /** Filled band between two series (Bollinger, Keltner, Donchian). */
  band?: { upper: (number | null)[]; lower: (number | null)[]; color: string }
  /** Fixed y-range for bounded oscillators (RSI [0,100], %R [-100,0]); omit to auto-fit. */
  range?: [number, number]
  /** Horizontal guide levels (70/30, 0, ±100, …). */
  guides?: number[]
  /** Value formatter for the pane axis and readout. */
  fmt?: 'price' | 'number' | 'percent' | 'volume'
}

// Math lives in indicatorMath.ts: export function computeIndicator(id: IndicatorId, candles: Candle[], volume: number[]): IndicatorResult

/** Keep the last n points of every series in an indicator result. */
export function tail(r: IndicatorResult, n: number): IndicatorResult {
  const cut = <T,>(v: T[]) => v.slice(-n)
  return { ...r, lines: r.lines.map((l) => ({ ...l, values: cut(l.values) })), band: r.band && { ...r.band, upper: cut(r.band.upper), lower: cut(r.band.lower) } }
}

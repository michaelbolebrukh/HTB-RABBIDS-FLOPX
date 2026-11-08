import type { Candle } from '@/data/fund'
import type { IndicatorId, IndicatorLine, IndicatorResult } from './indicators'

export type Series = (number | null)[]

const fin = (x: number): number | null => (Number.isFinite(x) ? x : null)
const nulls = (n: number): Series => new Array<number | null>(n).fill(null)

/** Rolling mean over `n` values; null until a full window of non-null values exists. */
export function sma(v: Series, n: number): Series {
  const out = nulls(v.length)
  if (n < 1) return out
  for (let i = n - 1; i < v.length; i++) {
    let s = 0
    let ok = true
    for (let j = i - n + 1; j <= i; j++) {
      const x = v[j]
      if (x == null) { ok = false; break }
      s += x
    }
    if (ok) out[i] = fin(s / n)
  }
  return out
}

/** Exponential average seeded with the SMA of the first `n` defined values. */
export function ema(v: Series, n: number): Series {
  const out = nulls(v.length)
  if (n < 1) return out
  const k = 2 / (n + 1)
  let start = v.findIndex((x) => x != null)
  if (start < 0 || start + n > v.length) return out
  let s = 0
  for (let j = start; j < start + n; j++) {
    if (v[j] == null) return out
    s += v[j] as number
  }
  let prev = s / n
  out[start + n - 1] = prev
  for (let i = start + n; i < v.length; i++) {
    const x = v[i]
    if (x == null) break
    prev = x * k + prev * (1 - k)
    out[i] = fin(prev)
  }
  return out
}

/** Wilder smoothing (RMA): seeded with the mean of the first `n` defined values, then (prev*(n-1)+x)/n. */
export function wilder(v: Series, n: number): Series {
  const out = nulls(v.length)
  if (n < 1) return out
  const start = v.findIndex((x) => x != null)
  if (start < 0 || start + n > v.length) return out
  let s = 0
  for (let j = start; j < start + n; j++) {
    if (v[j] == null) return out
    s += v[j] as number
  }
  let prev = s / n
  out[start + n - 1] = prev
  for (let i = start + n; i < v.length; i++) {
    const x = v[i]
    if (x == null) break
    prev = (prev * (n - 1) + x) / n
    out[i] = fin(prev)
  }
  return out
}

/** Linearly weighted mean, newest value weighted n, oldest 1. */
export function wma(v: Series, n: number): Series {
  const out = nulls(v.length)
  if (n < 1) return out
  const den = (n * (n + 1)) / 2
  for (let i = n - 1; i < v.length; i++) {
    let s = 0
    let ok = true
    for (let j = 0; j < n; j++) {
      const x = v[i - n + 1 + j]
      if (x == null) { ok = false; break }
      s += x * (j + 1)
    }
    if (ok) out[i] = fin(s / den)
  }
  return out
}

/** Rolling standard deviation (population by default, sample if `sample`). */
export function stdev(v: Series, n: number, sample = false): Series {
  const out = nulls(v.length)
  const d = sample ? n - 1 : n
  if (n < 1 || d < 1) return out
  for (let i = n - 1; i < v.length; i++) {
    let s = 0
    let ok = true
    for (let j = i - n + 1; j <= i; j++) {
      const x = v[j]
      if (x == null) { ok = false; break }
      s += x
    }
    if (!ok) continue
    const m = s / n
    let q = 0
    for (let j = i - n + 1; j <= i; j++) q += ((v[j] as number) - m) ** 2
    out[i] = fin(Math.sqrt(q / d))
  }
  return out
}

export function rollingMax(v: number[], n: number): Series {
  const out = nulls(v.length)
  for (let i = n - 1; i < v.length && n > 0; i++) out[i] = Math.max(...v.slice(i - n + 1, i + 1))
  return out
}

export function rollingMin(v: number[], n: number): Series {
  const out = nulls(v.length)
  for (let i = n - 1; i < v.length && n > 0; i++) out[i] = Math.min(...v.slice(i - n + 1, i + 1))
  return out
}

/** True range; the first bar uses high − low. */
export function trueRange(c: Candle[]): number[] {
  return c.map((b, i) => {
    if (i === 0) return b.h - b.l
    const pc = c[i - 1].c
    return Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc))
  })
}

const combine = (a: Series, b: Series, f: (x: number, y: number) => number): Series =>
  a.map((x, i) => (x == null || b[i] == null ? null : fin(f(x, b[i] as number))))

const typical = (c: Candle[]) => c.map((b) => (b.h + b.l + b.c) / 3)

function rsiOf(values: number[], n: number): Series {
  const gain: Series = [null]
  const loss: Series = [null]
  for (let i = 1; i < values.length; i++) {
    const d = values[i] - values[i - 1]
    gain.push(Math.max(d, 0))
    loss.push(Math.max(-d, 0))
  }
  const g = wilder(gain.slice(0, values.length), n)
  const l = wilder(loss.slice(0, values.length), n)
  return g.map((x, i) => {
    const y = l[i]
    if (x == null || y == null) return null
    if (y === 0) return x === 0 ? 50 : 100
    return 100 - 100 / (1 + x / y)
  })
}

function parabolicSar(c: Candle[], step = 0.02, max = 0.2): Series {
  const out = nulls(c.length)
  if (c.length < 2) return out
  let up = c[1].c >= c[0].c
  let sar = up ? c[0].l : c[0].h
  let ep = up ? c[1].h : c[1].l
  let af = step
  out[1] = sar
  for (let i = 2; i < c.length; i++) {
    sar = sar + af * (ep - sar)
    if (up) {
      sar = Math.min(sar, c[i - 1].l, c[i - 2].l)
      if (c[i].l < sar) { up = false; sar = ep; ep = c[i].l; af = step }
      else if (c[i].h > ep) { ep = c[i].h; af = Math.min(af + step, max) }
    } else {
      sar = Math.max(sar, c[i - 1].h, c[i - 2].h)
      if (c[i].h > sar) { up = true; sar = ep; ep = c[i].h; af = step }
      else if (c[i].l < ep) { ep = c[i].l; af = Math.min(af + step, max) }
    }
    out[i] = fin(sar)
  }
  return out
}

const line = (id: string, label: string, values: Series, color: string, kind?: IndicatorLine['kind']): IndicatorLine =>
  kind ? { id, label, values, color, kind } : { id, label, values, color }

const clean = (s: Series): Series => s.map((x) => (x == null ? null : fin(x)))

function compute(id: IndicatorId, c: Candle[], vol: number[]): IndicatorResult {
  const n = c.length
  const close = c.map((b) => b.c)
  const high = c.map((b) => b.h)
  const low = c.map((b) => b.l)
  const v = (i: number) => (Number.isFinite(vol[i]) ? vol[i] : 0)

  switch (id) {
    case 'sma':
      return { id, fmt: 'price', lines: [line('sma', 'SMA 20', sma(close, 20), 'var(--color-fct)')] }
    case 'ema':
      return { id, fmt: 'price', lines: [line('ema', 'EMA 20', ema(close, 20), 'var(--color-inf)')] }
    case 'wma':
      return { id, fmt: 'price', lines: [line('wma', 'WMA 20', wma(close, 20), 'var(--color-warn)')] }
    case 'ma_cross':
      return {
        id, fmt: 'price',
        lines: [
          line('fast', 'SMA 10', sma(close, 10), 'var(--color-warn)'),
          line('slow', 'SMA 30', sma(close, 30), 'var(--color-inf)'),
        ],
      }
    case 'bb': {
      const mid = sma(close, 20)
      const sd = stdev(close, 20)
      return {
        id, fmt: 'price',
        lines: [line('mid', 'SMA 20', mid, 'var(--color-fct)', 'dashed')],
        band: { upper: combine(mid, sd, (m, s) => m + 2 * s), lower: combine(mid, sd, (m, s) => m - 2 * s), color: 'var(--color-fct)' },
      }
    }
    case 'keltner': {
      const mid = ema(close, 20)
      const atr = wilder(trueRange(c), 10)
      return {
        id, fmt: 'price',
        lines: [line('mid', 'EMA 20', mid, 'var(--color-inf)', 'dashed')],
        band: { upper: combine(mid, atr, (m, a) => m + 2 * a), lower: combine(mid, atr, (m, a) => m - 2 * a), color: 'var(--color-inf)' },
      }
    }
    case 'donchian': {
      const upper = rollingMax(high, 20)
      const lower = rollingMin(low, 20)
      return {
        id, fmt: 'price',
        lines: [line('mid', 'Mid', combine(upper, lower, (a, b) => (a + b) / 2), 'var(--color-warn)', 'dashed')],
        band: { upper, lower, color: 'var(--color-warn)' },
      }
    }
    case 'psar':
      return { id, fmt: 'price', lines: [line('sar', 'SAR', parabolicSar(c), 'var(--color-warn)', 'dots')] }
    case 'vwap': {
      const tp = typical(c)
      let pv = 0
      let vv = 0
      const out = nulls(n)
      for (let i = 0; i < n; i++) {
        pv += tp[i] * v(i)
        vv += v(i)
        out[i] = vv > 0 ? fin(pv / vv) : null
      }
      return { id, fmt: 'price', lines: [line('vwap', 'VWAP', out, 'var(--color-fct)')] }
    }
    case 'rsi':
      return { id, fmt: 'number', range: [0, 100], guides: [70, 30], lines: [line('rsi', 'RSI 14', rsiOf(close, 14), 'var(--color-fct)')] }
    case 'macd': {
      const m = combine(ema(close, 12), ema(close, 26), (a, b) => a - b)
      const sig = ema(m, 9)
      return {
        id, fmt: 'number', guides: [0],
        lines: [
          line('hist', 'Histogram', combine(m, sig, (a, b) => a - b), 'var(--color-mute)', 'histogram'),
          line('macd', 'MACD', m, 'var(--color-fct)'),
          line('signal', 'Signal', sig, 'var(--color-warn)'),
        ],
      }
    }
    case 'stoch': {
      const hh = rollingMax(high, 14)
      const ll = rollingMin(low, 14)
      const k = close.map((x, i) => {
        if (hh[i] == null || ll[i] == null) return null
        const r = (hh[i] as number) - (ll[i] as number)
        return r > 0 ? fin(((x - (ll[i] as number)) / r) * 100) : 50
      })
      return {
        id, fmt: 'number', range: [0, 100], guides: [80, 20],
        lines: [line('k', '%K', k, 'var(--color-fct)'), line('d', '%D', sma(k, 3), 'var(--color-warn)')],
      }
    }
    case 'cci': {
      const tp = typical(c)
      const m = sma(tp, 20)
      const out = nulls(n)
      for (let i = 19; i < n; i++) {
        const mean = m[i] as number
        let dev = 0
        for (let j = i - 19; j <= i; j++) dev += Math.abs(tp[j] - mean)
        dev /= 20
        out[i] = dev > 0 ? fin((tp[i] - mean) / (0.015 * dev)) : 0
      }
      return { id, fmt: 'number', guides: [100, -100], lines: [line('cci', 'CCI 20', out, 'var(--color-fct)')] }
    }
    case 'willr': {
      const hh = rollingMax(high, 14)
      const ll = rollingMin(low, 14)
      const out = close.map((x, i) => {
        if (hh[i] == null || ll[i] == null) return null
        const r = (hh[i] as number) - (ll[i] as number)
        return r > 0 ? fin(((hh[i] as number) - x) / r * -100) || 0 : -50
      })
      return { id, fmt: 'number', range: [-100, 0], guides: [-20, -80], lines: [line('willr', '%R 14', out, 'var(--color-fct)')] }
    }
    case 'roc': {
      const out = close.map((x, i) => (i >= 12 && close[i - 12] !== 0 ? fin((x / close[i - 12] - 1) * 100) : null))
      return { id, fmt: 'percent', guides: [0], lines: [line('roc', 'ROC 12', out, 'var(--color-fct)')] }
    }
    case 'aroon': {
      const up = nulls(n)
      const down = nulls(n)
      for (let i = 14; i < n; i++) {
        let hi = i, lo = i
        for (let j = i; j >= i - 14; j--) {
          if (high[j] > high[hi]) hi = j
          if (low[j] < low[lo]) lo = j
        }
        up[i] = (100 * (14 - (i - hi))) / 14
        down[i] = (100 * (14 - (i - lo))) / 14
      }
      return {
        id, fmt: 'number', range: [0, 100],
        lines: [line('up', 'Aroon up', up, 'var(--color-up)'), line('down', 'Aroon down', down, 'var(--color-down)')],
      }
    }
    case 'adx': {
      const pdm: Series = [null]
      const mdm: Series = [null]
      for (let i = 1; i < n; i++) {
        const u = high[i] - high[i - 1]
        const d = low[i - 1] - low[i]
        pdm.push(u > d && u > 0 ? u : 0)
        mdm.push(d > u && d > 0 ? d : 0)
      }
      const tr: Series = trueRange(c).map((x, i) => (i === 0 ? null : x))
      const str = wilder(tr.slice(0, n), 14)
      const pdi = combine(wilder(pdm.slice(0, n), 14), str, (a, t) => (t > 0 ? (100 * a) / t : 0))
      const mdi = combine(wilder(mdm.slice(0, n), 14), str, (a, t) => (t > 0 ? (100 * a) / t : 0))
      const dx = combine(pdi, mdi, (p, m) => (p + m > 0 ? (100 * Math.abs(p - m)) / (p + m) : 0))
      return {
        id, fmt: 'number', range: [0, 100],
        lines: [
          line('adx', 'ADX', wilder(dx, 14), 'var(--color-fct)'),
          line('pdi', '+DI', pdi, 'var(--color-up)'),
          line('mdi', '−DI', mdi, 'var(--color-down)'),
        ],
      }
    }
    case 'atr':
      return { id, fmt: 'price', lines: [line('atr', 'ATR 14', wilder(trueRange(c), 14), 'var(--color-fct)')] }
    case 'hv': {
      const r: Series = close.map((x, i) => (i > 0 && x > 0 && close[i - 1] > 0 ? Math.log(x / close[i - 1]) : null))
      const sd = stdev(r, 20, true)
      return { id, fmt: 'percent', lines: [line('hv', 'HV 20', sd.map((x) => (x == null ? null : fin(x * Math.sqrt(52) * 100))), 'var(--color-fct)')] }
    }
    case 'obv': {
      let acc = 0
      const out = close.map((x, i) => {
        if (i > 0) acc += Math.sign(x - close[i - 1]) * v(i)
        return fin(acc)
      })
      return { id, fmt: 'volume', lines: [line('obv', 'OBV', out, 'var(--color-fct)')] }
    }
    case 'ad': {
      let acc = 0
      const out = c.map((b, i) => {
        const r = b.h - b.l
        if (r > 0) acc += (((b.c - b.l) - (b.h - b.c)) / r) * v(i)
        return fin(acc)
      })
      return { id, fmt: 'volume', lines: [line('ad', 'A/D', out, 'var(--color-fct)')] }
    }
    case 'mfi': {
      const tp = typical(c)
      const out = nulls(n)
      for (let i = 14; i < n; i++) {
        let pos = 0, neg = 0
        for (let j = i - 13; j <= i; j++) {
          const mf = tp[j] * v(j)
          if (tp[j] > tp[j - 1]) pos += mf
          else if (tp[j] < tp[j - 1]) neg += mf
        }
        out[i] = neg === 0 ? (pos === 0 ? 50 : 100) : fin(100 - 100 / (1 + pos / neg))
      }
      return { id, fmt: 'number', range: [0, 100], guides: [80, 20], lines: [line('mfi', 'MFI 14', out, 'var(--color-fct)')] }
    }
  }
}

/** Compute one indicator over candles (and per-bar volume). All output arrays have candles.length entries. */
export function computeIndicator(id: IndicatorId, candles: Candle[], volume: number[]): IndicatorResult {
  const r = compute(id, candles, volume)
  return {
    ...r,
    lines: r.lines.map((l) => ({ ...l, values: clean(l.values) })),
    ...(r.band ? { band: { ...r.band, upper: clean(r.band.upper), lower: clean(r.band.lower) } } : {}),
  }
}

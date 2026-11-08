import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { curveLinear, curveMonotoneX, curveStepAfter, type CurveFactory } from 'd3-shape'

/** User-chosen look for every chart in the app. Global, persisted per browser. */
export interface ChartStyle {
  curve: 'linear' | 'smooth' | 'step'
  /** Multiplier on every series stroke width. */
  weight: 'thin' | 'regular' | 'bold'
  grid: 'none' | 'y' | 'xy'
  /** Draw a dot at every data point (capped by density in the renderer). */
  points: boolean
  /** Soft gradient fill under every line series, not just ones that request `area`. */
  area: boolean
  /** Axis/tick/annotation label face. */
  font: 'mono' | 'sans'
  /** Tick count: sparse ≈ 0.6×, normal 1×, dense ≈ 1.6× of the chart's default. */
  ticks: 'sparse' | 'normal' | 'dense'
  /** Animate path/bar transitions when data or size changes. */
  motion: boolean
}

export const DEFAULT_CHART_STYLE: ChartStyle = {
  curve: 'linear',
  weight: 'regular',
  grid: 'y',
  points: false,
  area: false,
  font: 'mono',
  ticks: 'normal',
  motion: true,
}

const KEY = 'flopx.chartStyle'

function load(): ChartStyle {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw) return { ...DEFAULT_CHART_STYLE, ...JSON.parse(raw) }
  } catch {
    /* storage unavailable or corrupt: fall back to defaults */
  }
  return DEFAULT_CHART_STYLE
}

const Ctx = createContext<{ style: ChartStyle; set: (patch: Partial<ChartStyle>) => void; reset: () => void }>({
  style: DEFAULT_CHART_STYLE,
  set: () => {},
  reset: () => {},
})

export function ChartStyleProvider({ children }: { children: ReactNode }) {
  const [style, setStyle] = useState<ChartStyle>(load)
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(style))
    } catch {
      /* ignore */
    }
  }, [style])
  return (
    <Ctx.Provider value={{ style, set: (p) => setStyle((s) => ({ ...s, ...p })), reset: () => setStyle(DEFAULT_CHART_STYLE) }}>
      {children}
    </Ctx.Provider>
  )
}

export const useChartStyleControls = () => useContext(Ctx)

/** Resolved values renderers use directly. */
export function useChartStyle() {
  const { style } = useContext(Ctx)
  const curve: CurveFactory = style.curve === 'smooth' ? curveMonotoneX : style.curve === 'step' ? curveStepAfter : curveLinear
  return {
    ...style,
    curveFactory: curve,
    /** Multiply a series' nominal stroke width by this. */
    strokeScale: style.weight === 'thin' ? 0.7 : style.weight === 'bold' ? 1.6 : 1,
    /** Multiply a chart's default tick count by this (round the result, min 2). */
    tickScale: style.ticks === 'sparse' ? 0.6 : style.ticks === 'dense' ? 1.6 : 1,
    /** className for SVG <text> labels. */
    fontClass: style.font === 'mono' ? 'font-mono' : 'font-sans',
    /** CSS transition string for animatable SVG attributes ('' when motion is off). */
    transition: style.motion ? 'd .35s ease, x .35s ease, y .35s ease, width .35s ease, height .35s ease, cx .35s ease, cy .35s ease' : '',
  }
}

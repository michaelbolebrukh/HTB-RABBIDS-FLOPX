// Motion helpers for the tour: reduced-motion detection, a count-up hook and a staged-reveal hook.
// Everything collapses to its final state at once when the viewer prefers reduced motion.
import { useEffect, useRef, useState } from 'react'

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

export const easeOutCubic = (k: number) => 1 - (1 - k) ** 3
export const easeInOut = (k: number) => (k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2)

/**
 * Animate a number from 0 to `to` over `ms` (ease-out). Re-runs when `to` or `key` changes; the
 * returned value is `to` itself after the run, so formatted output never drifts from the real figure.
 */
export function useCountUp(to: number | null, ms = 900, key: string | number = ''): number | null {
  const [v, setV] = useState<number | null>(to == null ? null : prefersReducedMotion() ? to : 0)
  const raf = useRef<number | null>(null)
  useEffect(() => {
    if (raf.current) cancelAnimationFrame(raf.current)
    if (to == null || !Number.isFinite(to)) { setV(to); return }
    if (prefersReducedMotion() || ms <= 0) { setV(to); return }
    const start = performance.now()
    const tick = (now: number) => {
      const k = Math.min(1, (now - start) / ms)
      setV(k >= 1 ? to : to * easeOutCubic(k))
      if (k < 1) raf.current = requestAnimationFrame(tick)
    }
    raf.current = requestAnimationFrame(tick)
    return () => { if (raf.current) cancelAnimationFrame(raf.current) }
  }, [to, ms, key])
  return v
}

/** Number of items revealed so far: 0..n over `stepMs` each, restarting when `key` changes. */
export function useStagger(n: number, stepMs = 220, key: string | number = ''): number {
  const [shown, setShown] = useState(prefersReducedMotion() ? n : 0)
  useEffect(() => {
    if (prefersReducedMotion()) { setShown(n); return }
    setShown(0)
    let i = 0
    const id = setInterval(() => {
      i++
      setShown(Math.min(n, i))
      if (i >= n) clearInterval(id)
    }, stepMs)
    return () => clearInterval(id)
  }, [n, stepMs, key])
  return shown
}

/** True once mounted (next frame), so width/opacity transitions can run from their zero state. */
export function useMounted(key: string | number = ''): boolean {
  const [m, setM] = useState(prefersReducedMotion())
  useEffect(() => {
    if (prefersReducedMotion()) { setM(true); return }
    setM(false)
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setM(true)))
    return () => cancelAnimationFrame(id)
  }, [key])
  return m
}

/**
 * Drive a value from `from` to `to` over `ms` through `apply` (used to move the X-Ray reveal slider
 * programmatically). Returns a cancel function. Instant under reduced motion.
 */
export function tween(from: number, to: number, ms: number, apply: (v: number) => void, ease = easeInOut): () => void {
  if (prefersReducedMotion() || ms <= 0) { apply(to); return () => {} }
  let raf: number | null = null
  const start = performance.now()
  const tick = (now: number) => {
    const k = Math.min(1, (now - start) / ms)
    apply(from + (to - from) * ease(k))
    if (k < 1) raf = requestAnimationFrame(tick)
  }
  raf = requestAnimationFrame(tick)
  return () => { if (raf) cancelAnimationFrame(raf) }
}

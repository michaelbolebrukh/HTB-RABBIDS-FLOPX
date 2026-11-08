// AS_OF and the month axis follow the newest backend generatedAt that has landed.
import { relabelAxis, setAsOf } from '@/data/core'
import { setAxisAnchor } from './dates'

export function setAsOfFromGenerated(iso: string): void {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return
  setAsOf(d)
  if (setAxisAnchor(d)) relabelAxis(d)
}

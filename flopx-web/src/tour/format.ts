/** Format `n` the way `like` is formatted: same prefix, decimals and suffix ("$2.31" -> "$1.07"). Pure. */
export function fmtLike(like: string, n: number): string {
  const m = like.match(/^([^\d−-]*)(-?−?[\d,]+(?:\.(\d+))?)(.*)$/)
  if (!m) return like
  const [, pre, , dec, suf] = m
  const d = dec ? dec.length : 0
  const neg = n < 0 ? '−' : ''
  return `${pre}${neg}${Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: d, maximumFractionDigits: d })}${suf}`
}

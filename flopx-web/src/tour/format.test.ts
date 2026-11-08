import { describe, expect, it } from 'vitest'
import { fmtLike } from './format'

describe('fmtLike', () => {
  it('keeps the prefix, decimals and suffix of the target string', () => {
    expect(fmtLike('$2.31', 1.0749)).toBe('$1.07')
    expect(fmtLike('1,234', 567)).toBe('567')
    expect(fmtLike('1,234', 1234)).toBe('1,234')
    expect(fmtLike('+12.5%', 3.14159)).toBe('+3.1%')
    expect(fmtLike('+12.50%', 3.14159)).toBe('+3.14%')
    expect(fmtLike('34.9', 0)).toBe('0.0')
    expect(fmtLike('48¢', 12.6)).toBe('13¢')
  })
  it('renders negatives with a proper minus and leaves non-numeric targets alone', () => {
    expect(fmtLike('−3.2%', -1.25)).toBe('−1.3%')
    expect(fmtLike('−3.25%', -1.25)).toBe('−1.25%')
    expect(fmtLike('n/a', 5)).toBe('n/a')
  })
})

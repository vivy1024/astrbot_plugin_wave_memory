import { describe, expect, it } from 'vitest'

import { formatDisplayNumber, formatSignedDisplayNumber } from '@/lib/format-number'

describe('formatDisplayNumber', () => {
  it('截断二进制浮点并保留一位小数', () => {
    expect(formatDisplayNumber(5.549999999999999)).toBe('5.5')
    expect(formatDisplayNumber(12)).toBe('12')
    expect(formatDisplayNumber(Number.NaN)).toBe('—')
  })

  it('正数带符号、零不带加号', () => {
    expect(formatSignedDisplayNumber(12.04)).toBe('+12')
    expect(formatSignedDisplayNumber(-3.26)).toBe('-3.3')
    expect(formatSignedDisplayNumber(0)).toBe('0')
  })
})

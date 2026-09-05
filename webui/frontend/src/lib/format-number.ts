/** 关系/校准/雷达共用的展示格式：截断二进制浮点，整数不带小数。 */
export function formatDisplayNumber(value: unknown, fallback = '—'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  const rounded = Math.round(value * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

export function formatSignedDisplayNumber(value: unknown, fallback = '—'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  const text = formatDisplayNumber(value, fallback)
  return value > 0 && !text.startsWith('+') ? `+${text}` : text
}

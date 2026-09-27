/**
 * 昵称 / 显示名的显示兜底，与后端 `domain/display_name.py` 的清洗规则一致。
 *
 * QQ 群名片偶尔带着 protobuf 残片入库（`\n\x11\x12\x0f猫猫副队长\n\t\n\x07$ÿĀ…`）。后端写入口与
 * 存量迁移已清洗；这里在显示前再兜一层，避免旧数据或漏网的写入口把乱码画到页面上。
 *
 * - U+FFFD 与孤立代理项直接删除；
 * - 不含硬控制字符（C0 除 \t\n\r、DEL、C1）时只折叠空白、去首尾空白；
 * - 含硬控制字符时按控制字符切段，剥掉贴着控制字符的「ASCII 符号 + 扩展拉丁字母」残渣，
 *   取可读字符最多的一段（同分取靠前）。
 */

/* eslint-disable no-control-regex */
const HARD_CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/
const ANY_CONTROL = /[\u0000-\u001f\u007f-\u009f]+/
/* eslint-enable no-control-regex */
const SOFT_WHITESPACE = /[\t\n\r]/g
const MULTI_WHITESPACE = /\s{2,}/g
const LETTER_OR_NUMBER = /^[\p{L}\p{N}]$/u
const SYMBOL = /^\p{S}$/u

const LATIN_EXT_MIN = 0x80
const LATIN_EXT_MAX = 0x24f

function codeOf(ch: string): number {
  return ch.codePointAt(0) ?? 0
}

function dropInvalid(text: string): string {
  return Array.from(text)
    .filter((ch) => {
      const code = codeOf(ch)
      return code !== 0xfffd && !(code >= 0xd800 && code <= 0xdfff)
    })
    .join('')
}

function foldWhitespace(text: string): string {
  return text.replace(SOFT_WHITESPACE, ' ').replace(MULTI_WHITESPACE, ' ').trim()
}

function isLatinExt(ch: string): boolean {
  const code = codeOf(ch)
  return code >= LATIN_EXT_MIN && code <= LATIN_EXT_MAX
}

function isAsciiSymbol(ch: string): boolean {
  return codeOf(ch) < 0x80 && !LETTER_OR_NUMBER.test(ch) && !/\s/.test(ch)
}

function isResidueChar(ch: string): boolean {
  return isAsciiSymbol(ch) || isLatinExt(ch)
}

function isReadable(ch: string): boolean {
  const code = codeOf(ch)
  if (LETTER_OR_NUMBER.test(ch)) return code < 0x80 || code > LATIN_EXT_MAX
  return code >= 0x2000 && SYMBOL.test(ch)
}

function looksLikeResidue(run: string[]): boolean {
  const latin = run.filter(isLatinExt).length
  const symbols = run.filter(isAsciiSymbol).length
  return latin >= 2 || (latin >= 1 && symbols >= 1)
}

function trimEdge(segment: string, left: boolean, right: boolean): string {
  let chars = Array.from(segment)
  if (right) {
    let end = chars.length
    while (end > 0 && isResidueChar(chars[end - 1])) end -= 1
    if (end < chars.length && looksLikeResidue(chars.slice(end))) chars = chars.slice(0, end)
  }
  if (left) {
    let start = 0
    while (start < chars.length && isResidueChar(chars[start])) start += 1
    if (start > 0 && looksLikeResidue(chars.slice(0, start))) chars = chars.slice(start)
  }
  return chars.join('')
}

/** 清洗昵称；非字符串或清洗后无可读内容时返回空串。 */
export function sanitizeDisplayName(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  const text = dropInvalid(value)
  if (!HARD_CONTROL.test(text)) return foldWhitespace(text)

  const parts = text.split(ANY_CONTROL)
  const last = parts.length - 1
  let best = ''
  let bestScore = 0
  parts.forEach((part, index) => {
    if (!part) return
    const segment = foldWhitespace(trimEdge(part, index > 0, index < last))
    const score = Array.from(segment).filter(isReadable).length
    if (score > bestScore) {
      best = segment
      bestScore = score
    }
  })
  return best
}

/** 原值是否带控制字符或替换字符（即需要清洗才能显示）。 */
export function isDirtyDisplayName(value: unknown): boolean {
  return typeof value === 'string' && (ANY_CONTROL.test(value) || dropInvalid(value) !== value)
}

/**
 * 按顺序挑一个可显示的名字：优先第一个本身就干净的候选，其次第一个清洗后非空的候选，都没有时用 fallback。
 * 例如 `pickDisplayName([person.display_name, person.nickname], person.user_id)`：
 * display_name 带残片而 nickname 干净时显示 nickname。
 */
export function pickDisplayName(candidates: readonly unknown[], fallback = ''): string {
  for (const candidate of candidates) {
    if (isDirtyDisplayName(candidate)) continue
    const name = sanitizeDisplayName(candidate)
    if (name) return name
  }
  for (const candidate of candidates) {
    const name = sanitizeDisplayName(candidate)
    if (name) return name
  }
  return sanitizeDisplayName(fallback) || fallback
}

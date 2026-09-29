import type { ApiError } from '@/api/client'
import type { GameItemStatus } from '@/api/gameKnowledge'
import { humanizeApiError } from '@/lib/reason-label'

export const STATUS_LABELS: Record<GameItemStatus, string> = { draft: '草稿', active: '启用', archived: '归档' }
export const STATUS_VARIANTS: Record<GameItemStatus, 'default' | 'secondary' | 'outline'> = { active: 'default', draft: 'secondary', archived: 'outline' }
export const SOURCE_TYPE_LABELS: Record<string, string> = {
  manual: '手工录入',
  import: '文件导入',
  tutorial: '教程整理',
  starter_pack: '起始包',
  memory_migration: '记忆表迁移',
}
export const selectClass = 'h-8 rounded-md border bg-background px-2 text-xs text-foreground'
export const number = new Intl.NumberFormat('zh-CN')

export function errorText(reason: unknown, fallback = '操作失败'): string {
  const payload = (reason as ApiError | undefined)?.payload as { error?: { code?: string; current_version?: number } } | undefined
  if ((reason as ApiError | undefined)?.status === 409 && payload?.error?.code?.endsWith('_version_conflict')) {
    const current = payload.error.current_version
    return `刚被别处改过${current ? `（当前版本 ${current}）` : ''}，请重新打开后再改`
  }
  return humanizeApiError(reason, fallback)
}

/** 逗号、顿号、换行分隔的一段文字 → 去空去重的列表（关键词、别名）。 */
export function splitList(text: string): string[] {
  const seen = new Set<string>()
  const items: string[] = []
  for (const raw of text.split(/[\n,，、;；|]+/)) {
    const item = raw.trim()
    if (item && !seen.has(item)) {
      seen.add(item)
      items.push(item)
    }
  }
  return items
}

/** 每行一条（要点、注意、条件）。 */
export function splitLines(text: string): string[] {
  return text.split(/\n+/).map((line) => line.trim().replace(/^[-·•*]\s*/, '')).filter(Boolean)
}

/** 试匹配用的状态：每行「键=值」，数字按数字、true/false 按布尔，其余是短字符串。 */
export function parseStateLines(text: string): Record<string, string | number | boolean> {
  const state: Record<string, string | number | boolean> = {}
  for (const line of text.split(/\n+/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:：]\s*(.+?)\s*$/)
    if (!match) continue
    const [, key, raw] = match
    if (/^-?\d+(\.\d+)?$/.test(raw)) state[key] = Number(raw)
    else if (raw === 'true' || raw === 'false') state[key] = raw === 'true'
    else state[key] = raw
  }
  return state
}

export function when(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
}

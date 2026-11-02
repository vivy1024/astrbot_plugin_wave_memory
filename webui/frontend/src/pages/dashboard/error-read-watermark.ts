import type { RecentErrorItem } from '@/api/system'

/** 仪表盘告警卡“已读”水位：保存已读到的最新条目 ts（秒）。 */
export const ERRORS_READ_UNTIL_KEY = 'wm.errors.readUntilTs'

export function readErrorsWatermark(): number {
  try {
    const value = Number(window.localStorage.getItem(ERRORS_READ_UNTIL_KEY))
    return Number.isFinite(value) && value > 0 ? value : 0
  } catch {
    return 0
  }
}

export function writeErrorsWatermark(ts: number): void {
  try {
    window.localStorage.setItem(ERRORS_READ_UNTIL_KEY, String(ts))
  } catch {
    // localStorage 不可用（隐私模式等）时只在本次会话内生效
  }
}

/** 只保留 ts 大于水位的条目；保持后端给出的顺序（最新在前）。 */
export function unreadErrors(items: RecentErrorItem[], watermark: number): RecentErrorItem[] {
  return items.filter((item) => Number(item.ts) > watermark)
}

export function latestErrorTs(items: RecentErrorItem[]): number {
  return items.reduce((max, item) => {
    const ts = Number(item.ts)
    return Number.isFinite(ts) && ts > max ? ts : max
  }, 0)
}

/** 相对时间：“刚刚”“3 分钟前”“2 小时前”“1 天前”。 */
export function formatRelativeSeconds(ts: number, nowMs = Date.now()): string {
  const value = Number(ts)
  if (!Number.isFinite(value) || value <= 0) return '时间未知'
  const diff = Math.max(0, Math.floor(nowMs / 1000 - value))
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

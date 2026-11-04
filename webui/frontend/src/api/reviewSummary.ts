import { ApiRequestCancelledError, fetchJson } from './client'

export type ReviewSummaryKind = 'facts' | 'jargon' | 'expressions' | 'beliefs' | 'game_drafts'

export interface ReviewSummary {
  bot_id: string
  /** 任一计数读取失败时为 null，原因见 errors。 */
  counts: Record<ReviewSummaryKind, number | null>
  total: number
  generated_at: number
  errors?: Record<string, string>
}

const CACHE_TTL_MS = 30_000

const cache = new Map<string, { value: ReviewSummary; at: number }>()
const inflight = new Map<string, Promise<ReviewSummary>>()
const listeners = new Set<() => void>()

/**
 * 全局待审数：按 botId 去重并发请求，并做 30 秒内存缓存。
 * 并发的多个调用方共享同一个请求，因此这里不把单个调用方的 signal 传给 fetch，
 * 调用方取消时只是不再使用结果。
 */
export function getReviewSummary(botId: string, signal?: AbortSignal): Promise<ReviewSummary> {
  if (!botId) return Promise.reject(new Error('读取待审核汇总需要 Bot'))
  const cached = cache.get(botId)
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return Promise.resolve(cached.value)
  let request = inflight.get(botId)
  if (!request) {
    request = fetchJson<unknown>(`/api/review/summary?${new URLSearchParams({ bot_id: botId })}`)
      .then((payload) => {
        const value = normalizeReviewSummary(payload)
        // 请求期间若已被 invalidate，结果仍可返回给调用方，但不写回缓存。
        if (inflight.get(botId) === request) cache.set(botId, { value, at: Date.now() })
        return value
      })
      .finally(() => { if (inflight.get(botId) === request) inflight.delete(botId) })
    inflight.set(botId, request)
  }
  if (!signal) return request
  return new Promise<ReviewSummary>((resolve, reject) => {
    if (signal.aborted) { reject(new ApiRequestCancelledError()); return }
    const onAbort = () => reject(new ApiRequestCancelledError())
    signal.addEventListener('abort', onAbort, { once: true })
    request!.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort))
  })
}

function normalizeReviewSummary(payload: unknown): ReviewSummary {
  const record = typeof payload === 'object' && payload !== null ? payload as Record<string, unknown> : null
  const counts = record && typeof record.counts === 'object' && record.counts !== null ? record.counts as Record<string, unknown> : null
  if (!record || !counts) throw new Error('待审核汇总格式无效')
  const count = (key: ReviewSummaryKind) => {
    const value = counts[key]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  return {
    bot_id: String(record.bot_id ?? ''),
    counts: { facts: count('facts'), jargon: count('jargon'), expressions: count('expressions'), beliefs: count('beliefs'), game_drafts: count('game_drafts') },
    total: typeof record.total === 'number' && Number.isFinite(record.total) ? record.total : 0,
    generated_at: typeof record.generated_at === 'number' ? record.generated_at : 0,
    errors: typeof record.errors === 'object' && record.errors !== null ? record.errors as Record<string, string> : undefined,
  }
}

/** 审核完成后调用：清空缓存并通知徽标立即重拉。不传 botId 时清空全部。 */
export function invalidateReviewSummary(botId?: string): void {
  if (botId) { cache.delete(botId); inflight.delete(botId) }
  else { cache.clear(); inflight.clear() }
  listeners.forEach((listener) => listener())
}

export function subscribeReviewSummary(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** 各类待审项所在的页面；侧栏徽标与总览「待审核」卡片共用。表达候选在风格样例页的「群友表达审核」区。 */
export const REVIEW_SUMMARY_ROUTES: ReadonlyArray<{ kind: ReviewSummaryKind; path: string; label: string }> = [
  { kind: 'facts', path: '/facts', label: '事实' },
  { kind: 'jargon', path: '/jargon', label: '黑话' },
  { kind: 'expressions', path: '/knowledge/style-examples', label: '表达候选' },
  { kind: 'beliefs', path: '/beliefs', label: '看法' },
  { kind: 'game_drafts', path: '/knowledge/games', label: '游戏草稿' },
]

export function formatReviewBadge(count: number | null | undefined): string | null {
  if (typeof count !== 'number' || !Number.isFinite(count) || count <= 0) return null
  return count > 99 ? '99+' : String(count)
}

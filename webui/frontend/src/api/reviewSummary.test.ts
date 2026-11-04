import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchJson } from './client'
import { formatReviewBadge, getReviewSummary, invalidateReviewSummary, subscribeReviewSummary } from './reviewSummary'

vi.mock('./client', async (original) => ({ ...await original<typeof import('./client')>(), fetchJson: vi.fn() }))

const summary = (facts: number) => ({ bot_id: 'yushu', counts: { facts, jargon: 2, expressions: null, beliefs: 0, game_drafts: 1 }, total: facts + 3, generated_at: 1, errors: { expressions: 'boom' } })

beforeEach(() => {
  invalidateReviewSummary()
  vi.mocked(fetchJson).mockReset().mockResolvedValue(summary(5))
})
afterEach(() => vi.useRealTimers())

describe('全局待审核汇总客户端', () => {
  it('同一 Bot 并发调用只发一次请求，并按契约传 bot_id', async () => {
    const [a, b] = await Promise.all([getReviewSummary('yushu'), getReviewSummary('yushu')])
    expect(fetchJson).toHaveBeenCalledTimes(1)
    expect(vi.mocked(fetchJson).mock.calls[0][0]).toBe('/api/review/summary?bot_id=yushu')
    expect(a).toEqual(b)
    expect(a.counts).toEqual({ facts: 5, jargon: 2, expressions: null, beliefs: 0, game_drafts: 1 })
  })

  it('30 秒内命中缓存，超时后重新请求；不同 Bot 互不共享', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(1_000_000)
    await getReviewSummary('yushu')
    vi.setSystemTime(1_000_000 + 29_000)
    await getReviewSummary('yushu')
    expect(fetchJson).toHaveBeenCalledTimes(1)
    await getReviewSummary('baizz')
    expect(fetchJson).toHaveBeenCalledTimes(2)
    vi.setSystemTime(1_000_000 + 31_000)
    await getReviewSummary('yushu')
    expect(fetchJson).toHaveBeenCalledTimes(3)
  })

  it('invalidate 后重新请求并通知订阅者；取消订阅后不再通知', async () => {
    await getReviewSummary('yushu')
    const listener = vi.fn()
    const unsubscribe = subscribeReviewSummary(listener)
    vi.mocked(fetchJson).mockResolvedValue(summary(4))
    invalidateReviewSummary('yushu')
    expect(listener).toHaveBeenCalledTimes(1)
    expect((await getReviewSummary('yushu')).counts.facts).toBe(4)
    expect(fetchJson).toHaveBeenCalledTimes(2)
    unsubscribe()
    invalidateReviewSummary('yushu')
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('调用方取消只影响自己，共享请求的其他调用方照常拿到结果', async () => {
    const controller = new AbortController()
    const cancelled = getReviewSummary('yushu', controller.signal)
    const other = getReviewSummary('yushu')
    controller.abort()
    await expect(cancelled).rejects.toMatchObject({ name: 'AbortError' })
    await expect(other).resolves.toMatchObject({ total: 8 })
    expect(fetchJson).toHaveBeenCalledTimes(1)
  })

  it('徽标文本：0 / null 不显示，超过 99 显示 99+', () => {
    expect(formatReviewBadge(0)).toBeNull()
    expect(formatReviewBadge(null)).toBeNull()
    expect(formatReviewBadge(7)).toBe('7')
    expect(formatReviewBadge(99)).toBe('99')
    expect(formatReviewBadge(150)).toBe('99+')
  })
})

import { useEffect, useState } from 'react'

import { getReviewSummary, subscribeReviewSummary, type ReviewSummary } from '@/api/reviewSummary'

const POLL_INTERVAL_MS = 60_000

/**
 * 当前 Bot 的全局待审数。botId 为空不请求；每 60 秒刷新（页面隐藏时跳过），
 * 收到 invalidateReviewSummary 通知立即重拉。读取失败只返回 error，不抛出。
 */
export function useReviewSummary(botId: string): { data: ReviewSummary | null; error: unknown } {
  const [state, setState] = useState<{ botId: string; data: ReviewSummary | null; error: unknown }>({ botId, data: null, error: undefined })

  useEffect(() => {
    if (!botId) return
    let controller: AbortController | null = null
    const load = () => {
      controller?.abort()
      const current = new AbortController()
      controller = current
      getReviewSummary(botId, current.signal).then(
        (data) => { if (!current.signal.aborted) setState({ botId, data, error: undefined }) },
        (error: unknown) => { if (!current.signal.aborted) setState((prev) => ({ botId, data: prev.botId === botId ? prev.data : null, error })) },
      )
    }
    load()
    const timer = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      load()
    }, POLL_INTERVAL_MS)
    const unsubscribe = subscribeReviewSummary(load)
    return () => {
      controller?.abort()
      window.clearInterval(timer)
      unsubscribe()
    }
  }, [botId])

  if (!botId || state.botId !== botId) return { data: null, error: undefined }
  return { data: state.data, error: state.error }
}

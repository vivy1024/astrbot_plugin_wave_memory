import { useCallback, useEffect, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { getScopeOptions, type SessionOptionDto } from '@/api/options'
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS, type PageSize } from '@/components/shared/types'

export interface PaginationSearchParamsOptions {
  offsetKey?: string
  limitKey?: string
  defaultLimit?: PageSize
}

export type SearchFilterValue = string | number | boolean | null | undefined

function parseOffset(value: string | null): number {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0
}

function parseLimit(value: string | null, fallback: PageSize): PageSize {
  const parsed = Number(value)
  return PAGE_SIZE_OPTIONS.includes(parsed as PageSize) ? (parsed as PageSize) : fallback
}

export type SearchFilterSetter = (filters: Record<string, SearchFilterValue>, replace?: boolean) => void

export interface CanonicalScopeDefaultOptions {
  botId: string
  sessionId: string
  setFilters: SearchFilterSetter
  enabled?: boolean
}

function preferredSession(sessions: SessionOptionDto[], botId: string, sessionId: string): SessionOptionDto | undefined {
  const exact = sessions.find((item) => item.bot_id === botId && item.id === sessionId)
  if (exact) {
    if (exact.is_primary_alias === false && exact.alias_session_ids?.length) {
      const primary = sessions.find((item) => item.id === exact.alias_session_ids?.[0] && item.bot_id === exact.bot_id)
      return primary ?? exact
    }
    return exact
  }
  const bySession = sessionId ? sessions.find((item) => item.id === sessionId && (!botId || item.bot_id === botId)) : undefined
  if (bySession) return bySession
  const candidates = (botId ? sessions.filter((item) => item.bot_id === botId) : sessions).filter((item) => item.is_primary_alias !== false)
  return [...candidates].sort((left, right) => (right.count ?? 0) - (left.count ?? 0) || left.id.localeCompare(right.id))[0]
}

/**
 * 将缺失或失效的 URL Scope 收敛到服务端证实的 canonical group session。
 * 只读取正式 Scope 参数，不从 QQ 号或不完整的会话标识猜测 RuntimeScope。
 */
export function useCanonicalScopeDefault({ botId, sessionId, setFilters, enabled = true }: CanonicalScopeDefaultOptions) {
  useEffect(() => {
    if (!enabled) return
    let active = true
    getScopeOptions().then((payload) => {
      if (!active) return
      const selected = preferredSession(payload.sessions.filter((item) => item.kind === 'group'), botId, sessionId)
      if (!selected || (selected.bot_id === botId && selected.id === sessionId)) return
      setFilters({ bot_id: selected.bot_id, session_id: selected.id, visibility: 'group' }, true)
    }).catch(() => {
      // Scope options 的错误由各页既有 QueryState 呈现；这里不制造 fallback。
    })
    return () => { active = false }
  }, [botId, enabled, sessionId, setFilters])
}

export function usePaginationSearchParams(options: PaginationSearchParamsOptions = {}) {
  const { offsetKey = 'offset', limitKey = 'limit', defaultLimit = DEFAULT_PAGE_SIZE } = options
  const [searchParams, setSearchParams] = useSearchParams()
  const offset = parseOffset(searchParams.get(offsetKey))
  const limit = parseLimit(searchParams.get(limitKey), defaultLimit)

  const update = useCallback(
    (mutate: (next: URLSearchParams) => void, replace = false) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          mutate(next)
          return next
        },
        { replace },
      )
    },
    [setSearchParams],
  )

  const setOffset = useCallback(
    (nextOffset: number, replace = false) => {
      update((next) => {
        const normalized = Math.max(0, Math.floor(nextOffset))
        if (normalized === 0) next.delete(offsetKey)
        else next.set(offsetKey, String(normalized))
      }, replace)
    },
    [offsetKey, update],
  )

  const setLimit = useCallback(
    (nextLimit: PageSize) => {
      update((next) => {
        if (nextLimit === defaultLimit) next.delete(limitKey)
        else next.set(limitKey, String(nextLimit))
        next.delete(offsetKey)
      })
    },
    [defaultLimit, limitKey, offsetKey, update],
  )

  const setFilters = useCallback(
    (filters: Record<string, SearchFilterValue>, replace = false) => {
      update((next) => {
        Object.entries(filters).forEach(([key, value]) => {
          if (value === undefined || value === null || value === '') next.delete(key)
          else next.set(key, String(value))
        })
        next.delete(offsetKey)
      }, replace)
    },
    [offsetKey, update],
  )

  const goToPage = useCallback(
    (page: number) => setOffset((Math.max(1, Math.floor(page)) - 1) * limit),
    [limit, setOffset],
  )

  return useMemo(
    () => ({
      offset,
      limit,
      page: Math.floor(offset / limit) + 1,
      searchParams,
      setOffset,
      setLimit,
      setFilters,
      resetOffset: () => setOffset(0, true),
      goToPage,
    }),
    [goToPage, limit, offset, searchParams, setFilters, setLimit, setOffset],
  )
}

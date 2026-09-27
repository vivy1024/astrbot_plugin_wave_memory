import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'

import { getScopeOptions, type ScopeOptionsPayload } from '@/api/options'
import { getRouteByPath } from '@/app/routes'
import {
  SCOPE_DEPENDENT_QUERY_KEYS,
  readStoredScope,
  resolveScopeDefault,
  visibilityForSession,
  writeStoredScope,
  type RouteScopeAllowances,
  type RouteScopeLevel,
} from '@/lib/global-scope'
import { humanizeApiError } from '@/lib/reason-label'

export type ScopeOptionsStatus = 'idle' | 'loading' | 'ready' | 'error'

interface GlobalScopeContextValue {
  level: RouteScopeLevel
  allow: RouteScopeAllowances
  payload: ScopeOptionsPayload | null
  optionsStatus: ScopeOptionsStatus
  optionsError: string
  /** 选项已加载但 URL 仍待自动填充 / canonical 收敛。 */
  pending: boolean
  reloadOptions: () => void
}

const GlobalScopeContext = createContext<GlobalScopeContextValue | null>(null)
const NO_EXTRA_ALLOWANCES: RouteScopeAllowances = {}

export function routeScopeFor(pathname: string): { level: RouteScopeLevel; allow: RouteScopeAllowances } {
  const route = getRouteByPath(pathname)
  // 旧路由表 / 测试替身没有声明 scope 时按 none 处理：宁可不填，也不改动与作用域无关页面的数据。
  return { level: route.path === pathname ? route.scope ?? 'none' : 'none', allow: route.scopeAllows ?? NO_EXTRA_ALLOWANCES }
}

/**
 * 全局 Bot / 群作用域的唯一来源：URL 查询参数 bot_id / session_id / visibility。
 * Provider 负责加载选项、在需要作用域的路由上自动填充并把合法选择写入 localStorage。
 */
export function GlobalScopeProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { level, allow } = routeScopeFor(location.pathname)
  const [payload, setPayload] = useState<ScopeOptionsPayload | null>(null)
  const [optionsStatus, setOptionsStatus] = useState<ScopeOptionsStatus>('idle')
  const [optionsError, setOptionsError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  const visibility = searchParams.get('visibility') ?? ''
  const needsScope = level !== 'none'

  // 每次从 none 页面进入需要作用域的页面时加载（已有选项则静默刷新，吸收 Bot 管理页的改动）；none 页面不发请求。
  useEffect(() => {
    if (!needsScope) return
    let active = true
    setOptionsStatus((current) => (current === 'ready' ? current : 'loading'))
    getScopeOptions().then((next) => {
      if (!active) return
      setPayload(next)
      setOptionsStatus('ready')
      setOptionsError('')
    }).catch((reason: unknown) => {
      if (!active) return
      setOptionsStatus((current) => (current === 'ready' ? current : 'error'))
      setOptionsError(humanizeApiError(reason, '作用域选项加载失败'))
    })
    return () => { active = false }
  }, [needsScope, reloadKey])

  const target = useMemo(() => {
    if (!payload || !needsScope) return null
    return resolveScopeDefault({ payload, level, allow, current: { botId, sessionId, visibility }, stored: readStoredScope() })
  }, [allow, botId, level, needsScope, payload, sessionId, visibility])

  useEffect(() => {
    if (!needsScope || !payload) return
    if (target) {
      setSearchParams((current) => {
        const next = new URLSearchParams(current)
        next.set('bot_id', target.botId)
        if (target.sessionId) next.set('session_id', target.sessionId)
        else next.delete('session_id')
        if (target.sessionId) next.set('visibility', target.visibility)
        next.delete('offset')
        return next
      }, { replace: true })
      return
    }
    // URL 已是合法作用域：记住它，下次无参数进入时回到这里。
    if (botId) writeStoredScope({ botId, sessionId, visibility: visibility || 'group' })
  }, [botId, needsScope, payload, sessionId, setSearchParams, target, visibility])

  const reloadOptions = useCallback(() => setReloadKey((key) => key + 1), [])
  const value = useMemo<GlobalScopeContextValue>(() => ({
    level,
    allow,
    payload,
    optionsStatus,
    optionsError,
    pending: needsScope && (optionsStatus === 'idle' || optionsStatus === 'loading' || target !== null),
    reloadOptions,
  }), [allow, level, needsScope, optionsError, optionsStatus, payload, reloadOptions, target])

  return <GlobalScopeContext.Provider value={value}>{children}</GlobalScopeContext.Provider>
}

export interface SetScopeInput {
  botId?: string | null
  sessionId?: string | null
}

export interface GlobalScopeValue {
  botId: string
  sessionId: string
  /** URL 未声明时按 group 处理。 */
  visibility: string
  level: RouteScopeLevel
  allow: RouteScopeAllowances
  /** resolving：选项加载或自动填充尚未完成；ready：URL 即最终作用域；error：选项加载失败。 */
  status: 'resolving' | 'ready' | 'error'
  /** 当前页面声明的作用域是否已齐备（none 页面恒为 true）。 */
  hasRequiredScope: boolean
  payload: ScopeOptionsPayload | null
  optionsStatus: ScopeOptionsStatus
  optionsError: string
  reloadOptions: () => void
  inProvider: boolean
  setScope: (next: SetScopeInput) => void
}

/**
 * 页面读取全局作用域的唯一入口。脱离 GlobalScopeProvider（单页测试等）时退化为只读写 URL，不做自动填充。
 */
export function useGlobalScope(): GlobalScopeValue {
  const context = useContext(GlobalScopeContext)
  const [searchParams, setSearchParams] = useSearchParams()
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  const visibility = searchParams.get('visibility') || 'group'
  const payload = context?.payload ?? null

  const setScope = useCallback(({ botId: nextBot, sessionId: nextSession }: SetScopeInput) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      const previousBot = next.get('bot_id') ?? ''
      const previousSession = next.get('session_id') ?? ''
      if (nextBot !== undefined) {
        if (nextBot) next.set('bot_id', nextBot)
        else next.delete('bot_id')
      }
      const botChanged = nextBot !== undefined && (nextBot ?? '') !== previousBot
      if (nextSession !== undefined && nextSession !== null && nextSession !== '') {
        next.set('session_id', nextSession)
        next.set('visibility', visibilityForSession(nextSession, payload?.sessions))
      } else if (botChanged || nextSession === null || nextSession === '') {
        // 切换 Bot 时旧群必然失效，必须清空，不能跨 Bot 沿用。
        next.delete('session_id')
        next.delete('visibility')
      }
      if (botChanged || (next.get('session_id') ?? '') !== previousSession) {
        for (const key of SCOPE_DEPENDENT_QUERY_KEYS) next.delete(key)
      }
      return next
    })
  }, [payload?.sessions, setSearchParams])

  const level = context?.level ?? 'session'
  const hasRequiredScope = level === 'none' || (level === 'bot' ? Boolean(botId) : Boolean(botId && sessionId))
  const status: GlobalScopeValue['status'] = !context
    ? 'ready'
    : context.optionsStatus === 'error' && !context.payload
      ? 'error'
      : context.pending
        ? 'resolving'
        : 'ready'

  return {
    botId,
    sessionId,
    visibility,
    level,
    allow: context?.allow ?? NO_EXTRA_ALLOWANCES,
    status,
    hasRequiredScope,
    payload,
    optionsStatus: context?.optionsStatus ?? 'idle',
    optionsError: context?.optionsError ?? '',
    reloadOptions: context?.reloadOptions ?? noop,
    inProvider: Boolean(context),
    setScope,
  }
}

function noop() {}

/** 应用内（顶栏已有全局选择器）时页面不再渲染自己的 Bot / 群下拉；单独渲染页面（测试等）时照常显示。 */
export function useHidePageScopeSelectors(): boolean {
  const context = useContext(GlobalScopeContext)
  return Boolean(context && context.level !== 'none')
}

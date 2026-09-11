import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { checkAuth, login as loginRequest } from '@/api/auth'
import { clearStoredToken, getStoredToken, setAuthFailureHandler, setStoredToken } from '@/api/client'
import { getSystemStatus } from '@/api/system'
import { AuthContext, type AuthContextValue, type AuthState } from '@/app/auth-context'
import { humanizeApiError } from '@/lib/reason-label'

function initialAuthState(): AuthState {
  // 乐观假定：本地已存 Token 时前置设为 ready，防止初始化阶段瞬间闪烁 LoginPage 影响美观
  if (typeof window !== 'undefined') {
    const token = window.localStorage.getItem('wavememory.webui.token')
    if (token && token !== 'no-auth') {
      return { status: 'ready', token, requiresAuth: true }
    }
  }
  return { status: 'checking' }
}

function errorMessage(error: unknown): string {
  return humanizeApiError(error, '认证状态检查失败，请检查网络或服务端后重试。')
}

function isUnauthorized(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'status' in error && (error as { status?: number }).status === 401
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(initialAuthState)
  const stateRef = useRef(state)

  const commitState = useCallback((nextState: AuthState) => {
    stateRef.current = nextState
    setState(nextState)
  }, [])

  const logout = useCallback(() => {
    clearStoredToken()
    commitState({ status: 'anonymous', requiresAuth: true })
  }, [commitState])

  const refresh = useCallback(async () => {
    const previous = stateRef.current
    const storedToken = getStoredToken()
    const knownFacts = 'requiresAuth' in previous
      ? { requiresAuth: previous.requiresAuth, ...('token' in previous ? { token: previous.token } : {}) }
      : storedToken
        ? { requiresAuth: true, token: storedToken }
        : {}

    commitState({ status: 'checking', ...knownFacts })

    let auth: Awaited<ReturnType<typeof checkAuth>>
    try {
      auth = await checkAuth()
    } catch (error) {
      if (isUnauthorized(error)) {
        clearStoredToken()
        commitState({ status: 'anonymous', requiresAuth: true })
        return
      }
      commitState({ status: 'error', message: errorMessage(error), ...knownFacts })
      return
    }

    if (!auth.requires_auth) {
      clearStoredToken()
      commitState({ status: 'ready', token: null, requiresAuth: false })
      return
    }

    const token = getStoredToken()
    if (!token) {
      commitState({ status: 'anonymous', requiresAuth: true })
      return
    }

    try {
      await getSystemStatus()
      commitState({ status: 'ready', token, requiresAuth: true })
    } catch (error) {
      if (isUnauthorized(error)) {
        clearStoredToken()
        commitState({ status: 'anonymous', requiresAuth: true })
        return
      }
      commitState({ status: 'error', message: errorMessage(error), token, requiresAuth: true })
    }
  }, [commitState])

  const login = useCallback(async (password: string) => {
    const response = await loginRequest(password)
    setStoredToken(response.token)
    commitState({ status: 'ready', token: response.token, requiresAuth: true })
  }, [commitState])

  useEffect(() => {
    setAuthFailureHandler(logout)
    void refresh()
    return () => setAuthFailureHandler(undefined)
  }, [logout, refresh])

  const value = useMemo<AuthContextValue>(() => ({ state, login, logout, refresh }), [state, login, logout, refresh])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

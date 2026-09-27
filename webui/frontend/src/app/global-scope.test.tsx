import type { ReactNode } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ScopeOptionsPayload } from '@/api/options'
import { GlobalScopeProvider, useGlobalScope, useHidePageScopeSelectors } from '@/app/global-scope'

const payload: ScopeOptionsPayload = {
  bots: [{ db_id: 'yushu', name: '羽书' }, { db_id: 'baizz', name: '白真真' }],
  sessions: [
    { id: '羽书:group:2', bot_id: 'yushu', platform_id: '羽书', kind: 'group', conversation_id: '2', label: '大群', count: 900 },
    { id: '白真真:group:3', bot_id: 'baizz', platform_id: '白真真', kind: 'group', conversation_id: '3', label: '白群', count: 5 },
  ],
  channels: [],
  generated_at: 0,
  source: { health: 'healthy', reason_code: null },
}

vi.mock('@/api/options', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/options')>()),
  getScopeOptions: vi.fn(async () => payload),
}))

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="search">{decodeURIComponent(location.search)}</div>
}

function wrapper(initial: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initial]}>
      <GlobalScopeProvider>{children}<LocationProbe /></GlobalScopeProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  try {
    window.localStorage?.removeItem?.('wavememory.webui.global-scope.v1')
  } catch {
    // 测试环境可能没有 localStorage
  }
})

describe('GlobalScopeProvider', () => {
  it('群级页面在没有参数时自动填上 Bot 和最活跃的群', async () => {
    render(<div />, { wrapper: wrapper('/memories') })
    await waitFor(() => expect(screen.getByTestId('search').textContent).toContain('session_id=羽书:group:2'))
    expect(screen.getByTestId('search').textContent).toContain('bot_id=yushu')
  })

  it('不需要作用域的页面不改地址', async () => {
    render(<div />, { wrapper: wrapper('/settings') })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.getByTestId('search').textContent).toBe('')
  })

  it('切换 Bot 时清空旧群与对象级参数', async () => {
    const { result } = renderHook(() => useGlobalScope(), { wrapper: wrapper('/memories?bot_id=yushu&session_id=羽书:group:2&visibility=group&ref=abc&offset=40') })
    await waitFor(() => expect(result.current.status).toBe('ready'))
    act(() => result.current.setScope({ botId: 'baizz' }))
    await waitFor(() => expect(result.current.botId).toBe('baizz'))
    const search = screen.getByTestId('search').textContent ?? ''
    expect(search).not.toContain('ref=abc')
    expect(search).not.toContain('offset=40')
    expect(search).not.toContain('羽书:group:2')
  })

  it('应用内隐藏页面自带的作用域下拉，脱离应用单独渲染时保留', () => {
    const inApp = renderHook(() => useHidePageScopeSelectors(), { wrapper: wrapper('/memories') })
    expect(inApp.result.current).toBe(true)
    const standalone = renderHook(() => useHidePageScopeSelectors(), {
      wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={['/memories']}>{children}</MemoryRouter>,
    })
    expect(standalone.result.current).toBe(false)
  })
})

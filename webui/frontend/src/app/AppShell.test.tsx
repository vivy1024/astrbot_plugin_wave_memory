import type { ReactNode } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { AppRoutes } from '@/app/AppShell'

vi.mock('@/app/routes', () => ({
  defaultRoute: '/dashboard',
  appRoutes: [
    { path: '/dashboard', title: '总览', description: '', group: 'home', icon: () => null, element: () => <div>总览内容</div> },
    { path: '/graph', scope: 'session', title: '关系图谱', description: '', group: 'memory', icon: () => null, element: () => <div>关系图谱内容</div> },
  ],
}))
vi.mock('@/app/global-scope', () => ({ GlobalScopeProvider: ({ children }: { children: ReactNode }) => <>{children}</> }))
vi.mock('@/components/layout/PageHeader', () => ({ PageHeader: () => <div>外层页头</div> }))
vi.mock('@/components/layout/WaveSidebar', () => ({ WaveSidebar: () => <div>外层侧栏</div> }))
vi.mock('@/components/ui/scroll-area', () => ({ ScrollArea: ({ children }: { children: ReactNode }) => <div>{children}</div> }))
vi.mock('@/components/ui/sidebar', () => ({
  SidebarProvider: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SidebarInset: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}))
vi.mock('@/components/ui/sonner', () => ({ Toaster: () => null }))

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

function renderAt(entry: string) {
  return render(<MemoryRouter initialEntries={[entry]}><AppRoutes /><LocationProbe /></MemoryRouter>)
}

describe('AppRoutes', () => {
  it('关系图谱与普通页面一样使用 AppShell（不再有全屏特例）', () => {
    renderAt('/graph?layer=kg')
    expect(screen.getByText('关系图谱内容')).toBeVisible()
    expect(screen.getByText('外层侧栏')).toBeVisible()
    expect(screen.getByText('外层页头')).toBeVisible()
  })

  it('/tags/graph 重定向到 /graph?layer=tags，并保留原查询参数', () => {
    renderAt('/tags/graph?bot_id=yushu&session_id=%E7%BE%BD%E4%B9%A6%3Agroup%3A42&visibility=group&ref=oref.abc')
    expect(screen.getByText('关系图谱内容')).toBeVisible()
    const location = screen.getByTestId('location').textContent ?? ''
    const [pathname, search] = location.split('?')
    const params = new URLSearchParams(search)
    expect(pathname).toBe('/graph')
    expect(params.get('layer')).toBe('tags')
    expect(params.get('bot_id')).toBe('yushu')
    expect(params.get('session_id')).toBe('羽书:group:42')
    expect(params.get('ref')).toBe('oref.abc')
  })

  it('/explore 重定向到 /graph?layer=kg，丢弃旧 iframe 的 embed 参数', () => {
    renderAt('/explore?bot_id=yushu&session_id=qq%3Agroup%3A42&visibility=group&embed=1')
    const params = new URLSearchParams((screen.getByTestId('location').textContent ?? '').split('?')[1])
    expect(screen.getByTestId('location').textContent).toMatch(/^\/graph\?/)
    expect(params.get('layer')).toBe('kg')
    expect(params.get('bot_id')).toBe('yushu')
    expect(params.has('embed')).toBe(false)
  })

  it('普通页面继续使用 AppShell', () => {
    renderAt('/dashboard')
    expect(screen.getByText('总览内容')).toBeVisible()
    expect(screen.getByText('外层侧栏')).toBeVisible()
  })
})

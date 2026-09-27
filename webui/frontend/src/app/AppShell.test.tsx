import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { AppRoutes } from '@/app/AppShell'

vi.mock('@/app/routes', () => ({
  defaultRoute: '/dashboard',
  appRoutes: [
    { path: '/dashboard', title: '总览', description: '', group: 'overview', icon: () => null, element: () => <div>总览内容</div> },
    { path: '/explore', title: '神经云图', description: '', group: 'overview', icon: () => null, element: () => <div>全屏 Explore 内容</div> },
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

describe('AppRoutes Explore 布局边界', () => {
  it('/explore 仍在认证后的 AppRoutes 内，但不渲染 AppShell 侧栏和页头', () => {
    render(<MemoryRouter initialEntries={['/explore']}><AppRoutes /></MemoryRouter>)

    expect(screen.getByText('全屏 Explore 内容')).toBeVisible()
    expect(screen.queryByText('外层侧栏')).not.toBeInTheDocument()
    expect(screen.queryByText('外层页头')).not.toBeInTheDocument()
  })

  it('普通页面继续使用 AppShell', () => {
    render(<MemoryRouter initialEntries={['/dashboard']}><AppRoutes /></MemoryRouter>)

    expect(screen.getByText('总览内容')).toBeVisible()
    expect(screen.getByText('外层侧栏')).toBeVisible()
    expect(screen.getByText('外层页头')).toBeVisible()
  })
})

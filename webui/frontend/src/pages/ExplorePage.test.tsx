import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ExplorePage } from '@/pages/PlaceholderPage'

vi.mock('@/api/options', () => ({
  getScopeOptions: vi.fn(async () => ({ bots: [], sessions: [], channels: [] })),
  scopeOptionsFor: vi.fn(() => []),
}))
vi.mock('@/components/shared', () => ({
  ScopeSelect: ({ label }: { label: string }) => <div data-testid={`scope-${label}`}>{label}</div>,
}))

describe('ExplorePage 全屏壳层', () => {
  it('缺少完整 Scope 时强制展示 Scope Dialog，且不创建 iframe', () => {
    const view = render(<MemoryRouter initialEntries={['/explore']}><ExplorePage /></MemoryRouter>)

    expect(screen.getByRole('dialog')).toHaveTextContent('选择神经云图的 Bot 和群')
    expect(screen.getByTestId('scope-Bot')).toBeVisible()
    expect(screen.getByTestId('scope-群 / 会话')).toBeVisible()
    expect(screen.queryByTitle('3D Cosmic NeuroGalaxy')).not.toBeInTheDocument()
    expect(view.container.querySelector('[data-page="explore-galaxy"]')).toHaveClass('h-[100svh]', 'w-[100vw]')
  })

  it('完整 Scope 下渲染无 token 的 embed iframe 与紧凑控制条', () => {
    render(<MemoryRouter initialEntries={['/explore?bot_id=bot%3Ayushu&session_id=qq%3Agroup%3A42&visibility=group']}><ExplorePage /></MemoryRouter>)

    const iframe = screen.getByTitle('3D Cosmic NeuroGalaxy')
    const src = iframe.getAttribute('src') ?? ''
    const query = new URLSearchParams(src.split('?')[1])
    expect(query.get('bot_id')).toBe('bot:yushu')
    expect(query.get('session_id')).toBe('qq:group:42')
    expect(query.get('visibility')).toBe('group')
    expect(query.get('embed')).toBe('1')
    expect(query.has('token')).toBe(false)
    expect(iframe).toHaveAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups')
    expect(screen.getByRole('button', { name: '返回总览' })).toBeVisible()
    expect(screen.getByRole('button', { name: '切换 Bot / 群' })).toBeVisible()
    expect(screen.getByText('loading · 加载中')).toBeVisible()
  })
})

import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { TooltipProvider } from '@/components/ui/tooltip'
import { setViewport } from '@/test/setup'
import { WaveSidebar } from './WaveSidebar'

const api = vi.hoisted(() => ({ getReviewSummary: vi.fn(), getSystemStatus: vi.fn() }))

vi.mock('@/api/system', () => ({ getSystemStatus: api.getSystemStatus }))
vi.mock('@/api/reviewSummary', async (original) => ({ ...await original<typeof import('@/api/reviewSummary')>(), getReviewSummary: api.getReviewSummary }))
vi.mock('@/components/theme/ThemeToggle', () => ({ ThemeToggle: () => null }))

function show(entry = '/home?bot_id=yushu', children?: ReactNode) {
  return render(<MemoryRouter initialEntries={[entry]}><TooltipProvider><SidebarProvider><WaveSidebar />{children}</SidebarProvider></TooltipProvider></MemoryRouter>)
}

function item(title: string) {
  return screen.getByRole('link', { name: title }).closest('[data-slot="sidebar-menu-item"]') as HTMLElement
}

beforeEach(() => {
  setViewport(1280)
  api.getSystemStatus.mockReset().mockResolvedValue({})
  api.getReviewSummary.mockReset().mockResolvedValue({
    bot_id: 'yushu', counts: { facts: 12, jargon: 150, expressions: 0, beliefs: null, game_drafts: 3 }, total: 165, generated_at: 1,
  })
})

describe('侧栏待审核徽标', () => {
  it('按当前 Bot 渲染计数，0 与 null 不显示，超过 99 显示 99+', async () => {
    show()
    expect(await screen.findByLabelText('待审核 12 条')).toHaveTextContent('12')
    expect(api.getReviewSummary).toHaveBeenCalledWith('yushu', expect.any(AbortSignal))
    expect(within(item('事实')).getByLabelText('待审核 12 条')).toBeInTheDocument()
    expect(within(item('黑话与口癖')).getByLabelText('待审核 150 条')).toHaveTextContent('99+')
    expect(within(item('游戏知识库')).getByLabelText('待审核 3 条')).toHaveTextContent('3')
    expect(within(item('风格样例')).queryByLabelText(/待审核/)).not.toBeInTheDocument()
    expect(within(item('看法')).queryByLabelText(/待审核/)).not.toBeInTheDocument()
    expect(within(item('记忆')).queryByLabelText(/待审核/)).not.toBeInTheDocument()
  })

  it('没有 Bot 时不请求、不显示徽标', async () => {
    show('/home')
    await screen.findByRole('link', { name: '事实' })
    expect(api.getReviewSummary).not.toHaveBeenCalled()
    expect(screen.queryByLabelText(/待审核/)).not.toBeInTheDocument()
  })

  it('读取失败时不显示徽标，也不影响导航', async () => {
    api.getReviewSummary.mockRejectedValue(new Error('boom'))
    show()
    await vi.waitFor(() => expect(api.getReviewSummary).toHaveBeenCalled())
    expect(screen.getByRole('link', { name: '事实' })).toBeVisible()
    expect(screen.queryByLabelText(/待审核/)).not.toBeInTheDocument()
  })
})

import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DashboardPage } from './DashboardPage'

const api = vi.hoisted(() => ({ system: vi.fn(), errors: vi.fn(), channels: vi.fn(), metrics: vi.fn() }))
vi.mock('@/api/system', () => ({ getSystemStatus: api.system, getRecentErrors: api.errors, getInjectionMetrics: api.metrics }))
vi.mock('@/api/channels', () => ({ getChannelConfig: api.channels }))
vi.mock('./InjectionTrendCard', () => ({ InjectionTrendCard: ({ metrics }: { metrics?: { range?: string } }) => <div>趋势 {metrics?.range ?? '无数据'}</div> }))
vi.mock('./SystemHealthCard', () => ({ SystemHealthCard: () => <div>系统健康详情</div> }))

beforeEach(() => {
  api.system.mockRejectedValue(new Error('system unavailable'))
  api.errors.mockResolvedValue({ errors: [{ level: 'error', message: 'channel exploded' }] })
  api.channels.mockRejectedValue(new Error('channels unavailable'))
  api.metrics.mockResolvedValue({ range: '7d', ranking: [{ key: 'exp_memories_tokens', sum: 12 }] })
})

describe('DashboardPage 局部错误边界', () => {
  it('system/channels 失败不拖垮 errors 与 metrics，且不展示伪默认通道值', async () => {
    render(<MemoryRouter><DashboardPage /></MemoryRouter>)

    expect(await screen.findByText('系统总览加载失败')).toBeVisible()
    expect(await screen.findByText('channel exploded')).toBeVisible()
    expect(screen.getByText('趋势 7d')).toBeVisible()
    expect(screen.getByText('通道配置加载失败')).toBeVisible()
    expect(screen.getAllByText(/不可用/).length).toBeGreaterThan(0)
    expect(screen.queryByText('5 条')).not.toBeInTheDocument()
    expect(screen.queryByText('220')).not.toBeInTheDocument()
    expect(screen.queryByText('系统状态正常')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '重试' }).length).toBeGreaterThanOrEqual(2)
  })

  it('局部分区未返回时不把未知待办和指标显示成正常或零', async () => {
    api.system.mockResolvedValue({ memories: { total: 3 } })
    api.errors.mockResolvedValue({ errors: [] })
    api.channels.mockResolvedValue({ current: { channels: {} } })
    api.metrics.mockRejectedValue(new Error('metrics unavailable'))

    render(<MemoryRouter><DashboardPage /></MemoryRouter>)

    expect(await screen.findByText('系统待办状态不可用')).toBeVisible()
    expect(await screen.findByText('指标不可用 / 未返回')).toBeVisible()
    expect(screen.queryByText('系统状态正常')).not.toBeInTheDocument()
    expect(screen.queryByText(/当前窗口 消耗 0 token/)).not.toBeInTheDocument()
  })

  it('待办处理入口保持内容宽度，不挤压标题区', async () => {
    api.system.mockResolvedValue({
      memories: { total: 3 },
      todos: { untagged_count: 8, pending_fewshot: 0, has_errors: false },
    })
    api.errors.mockResolvedValue({ errors: [] })
    api.channels.mockResolvedValue({ current: { channels: {} } })
    api.metrics.mockResolvedValue({ range: '7d', ranking: [] })

    render(<MemoryRouter><DashboardPage /></MemoryRouter>)

    const action = await screen.findByRole('link', { name: '去处理' })
    expect(action).toHaveAttribute('href', '/tags')
    expect(action).toHaveClass('w-auto')
    expect(action).not.toHaveClass('w-full')
  })
})

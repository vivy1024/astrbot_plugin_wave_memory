import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DashboardPage } from './DashboardPage'

const api = vi.hoisted(() => ({ system: vi.fn(), errors: vi.fn(), channels: vi.fn(), metrics: vi.fn() }))
vi.mock('@/api/system', () => ({ getSystemStatus: api.system, getRecentErrors: api.errors, getInjectionMetrics: api.metrics }))
vi.mock('@/api/channels', () => ({ getChannelConfig: api.channels }))
vi.mock('./InjectionTrendCard', () => ({ InjectionTrendCard: ({ metrics }: { metrics?: { range?: string } }) => <div>趋势 {metrics?.range ?? '无数据'}</div> }))
vi.mock('./SystemHealthCard', () => ({ SystemHealthCard: () => <div>系统健康详情</div> }))

beforeEach(() => {
  // jsdom 下 Node 自带的 localStorage 不完整，这里换成内存实现
  const values = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      key: (index: number) => Array.from(values.keys())[index] ?? null,
      get length() { return values.size },
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, String(value)),
    } satisfies Storage,
  })
  api.system.mockRejectedValue(new Error('system unavailable'))
  api.errors.mockResolvedValue({ errors: [{ source: 'injection', level: 'error', message: 'channel exploded', ts: Math.floor(Date.now() / 1000) - 180 }] })
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

  it('告警卡按已读水位过滤，标为已读后不再占位；仅警告时用琥珀色“运行警告”', async () => {
    const now = Math.floor(Date.now() / 1000)
    window.localStorage.setItem('wm.errors.readUntilTs', String(now - 600))
    api.errors.mockResolvedValue({ errors: [
      { source: 'tagger', level: 'warning', message: '新警告', ts: now - 120 },
      { source: 'injection', level: 'error', message: '已读的旧错误', ts: now - 900 },
    ] })
    const user = userEvent.setup()
    render(<MemoryRouter><DashboardPage /></MemoryRouter>)

    expect(await screen.findByText('运行警告')).toBeVisible()
    expect(screen.queryByText('检测到系统异常故障')).not.toBeInTheDocument()
    expect(screen.getByText('新警告')).toBeVisible()
    expect(screen.getByText('2 分钟前')).toBeVisible()
    expect(screen.getByText('tagger')).toBeVisible()
    expect(screen.queryByText('已读的旧错误')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '标为已读' }))
    expect(window.localStorage.getItem('wm.errors.readUntilTs')).toBe(String(now - 120))
    expect(screen.queryByText('运行警告')).not.toBeInTheDocument()
  })
})

import { MemoryRouter } from 'react-router-dom'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SoulPage } from './SoulPage'

const api = vi.hoisted(() => ({ formal: vi.fn(), refresh: vi.fn(), scopes: vi.fn(), relationships: vi.fn() }))
vi.mock('@/api/soul', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/soul')>()
  return { ...actual, getSoulState: api.formal, refreshSoulState: api.refresh }
})
vi.mock('@/api/options', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/options')>()
  return { ...actual, getScopeOptions: api.scopes }
})
vi.mock('@/api/people', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/people')>()
  return { ...actual, getRelationships: api.relationships }
})
vi.mock('@/components/shared', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/shared')>()
  return { ...actual, EvidenceList: () => <div>证据列表</div>, ObjectDeepLink: ({ children }: { children: React.ReactNode }) => <span>{children}</span> }
})
vi.mock('@/components/ui/chart', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/chart')>()
  return { ...actual, ChartContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>, ChartLegend: () => null, ChartLegendContent: () => null, ChartTooltip: () => null, ChartTooltipContent: () => null }
})
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    Bar: () => null,
    BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    CartesianGrid: () => null,
    Line: () => null,
    LineChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    Legend: () => null,
    PolarAngleAxis: () => null,
    PolarGrid: () => null,
    PolarRadiusAxis: () => null,
    Radar: () => null,
    RadarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    Tooltip: () => null,
    XAxis: () => null,
    YAxis: () => null,
  }
})

const page = { items: [], page: { total: 0, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 0, has_more: false } }

beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
  api.scopes.mockResolvedValue({
    bots: [{ db_id: 'bot-a', name: 'Bot A', status: 'active' }],
    sessions: [{ id: 'session-a', bot_id: 'bot-a', platform_id: 'qq', kind: 'group', conversation_id: '1', label: '群 1' }],
    channels: [], generated_at: 1, source: { health: 'healthy', reason_code: null },
  })
  api.relationships.mockResolvedValue({ items: [], page })
  api.formal.mockResolvedValue({
    source: { health: 'healthy', reason_code: null },
    mood: { value: '平静', state: 'known', components: null, policy_version: 'v1', revision: 1, evidence: [] },
    concerns: page,
    timeline: page,
    relationship_history: page,
    soul_context: { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null },
    relationship: { affinity: 0.5, state: 'known', revision: 1, evidence: [], people_ref: null },
    capabilities: { mutate: { available: false, reason_code: 'readonly' }, runtime_refresh: { available: false, reason_code: 'unavailable' } },
    runtime_refresh: { status: 'unavailable', operation: null, reason_code: 'unavailable' },
  })
})

describe('SoulPage 仅加载正式 Scope 数据', () => {
  it('展示 formal scoped 数据且不渲染旧数据审计区', async () => {
    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&visibility=group']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByText('平静')).toBeVisible()
    expect(screen.queryByText(/只读审计/)).not.toBeInTheDocument()
    expect(screen.getByText(/关切和时间线共用下面的分页/)).toBeVisible()
    expect(screen.getByRole('link', { name: '人物与印象时间线' })).toHaveAttribute('href', expect.stringContaining('/people'))
    expect(api.formal).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-a', session_id: 'session-a' }), 25, 0, expect.anything(), { from_ts: undefined, to_ts: undefined })
  })

  it('心智页加载群级只读关系对照，但不增加单人关系写入口', async () => {
    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&visibility=group']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByText('平静')).toBeVisible()
    expect(await screen.findByText('本群关系分布（只读对照）')).toBeVisible()
    expect(screen.getByText('当前群还没有可对照的五维关系记录。')).toBeInTheDocument()
    await waitFor(() => expect(api.relationships).toHaveBeenCalledWith(
      {
        bot_id: 'bot-a',
        session_id: 'session-a',
        visibility: 'group',
        relationship_state: 'known',
        sort_by: 'affinity',
        sort_order: 'desc',
        limit: 500,
        offset: 0,
      },
      expect.anything(),
    ))
    expect(screen.queryByText('对某个群友的关系')).not.toBeInTheDocument()
    expect(screen.queryByText('关系变化')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /校准|保存|提交/ })).not.toBeInTheDocument()
    // 正式 Soul 的基础 scope 不携带 subject_principal_id
    const formalCallArgs = api.formal.mock.calls[0]?.[0] ?? {}
    expect(formalCallArgs).toMatchObject({ bot_id: 'bot-a', session_id: 'session-a', visibility: 'group' })
    expect(formalCallArgs).not.toHaveProperty('subject_principal_id')
  })

  it('切换群友与维度时按需读取同一作用域的关系历史摘要', async () => {
    api.relationships.mockResolvedValue({
      items: [{
        subject_principal_id: 'qq:user:u1',
        person: { user_id: 'u1', display_name: '群友甲' },
        affinity: 42,
        state: 'known',
        revision: 3,
        values: {
          familiarity: { effective_value: 50 },
          trust: { effective_value: 25 },
          fun: { effective_value: 30 },
          hostility: { effective_value: 0 },
          depth: { effective_value: 20 },
        },
        evidence: [],
        object_ref: { ref: 'relationship-u1', kind: 'relationship', locator: 'qq:user:u1', scope_query: { bot_id: 'bot-a', session_id: 'session-a', visibility: 'group', subject_principal_id: 'qq:user:u1' } },
        calibration: { available: true, reason_code: null },
      }],
      page,
    } as never)
    api.formal.mockImplementation(async (scope: { subject_principal_id?: string }) => ({
      source: { health: 'healthy', reason_code: null },
      mood: { value: '平静', state: 'known', components: null, policy_version: 'v1', revision: 1, evidence: [] },
      concerns: page,
      timeline: page,
      relationship_history: scope.subject_principal_id ? {
        ...page,
        items: [{ id: 'history-1', dimension: 'trust', timestamp: 1720000000, reason: '被感谢', kind: 'automatic', action: null, revision: 2, source_memory_id: 11, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null }],
      } : page,
      soul_context: { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null },
      relationship: { affinity: 0.5, state: 'known', revision: 1, evidence: [], people_ref: null },
      capabilities: { mutate: { available: false, reason_code: 'readonly' }, runtime_refresh: { available: false, reason_code: 'unavailable' } },
      runtime_refresh: { status: 'unavailable', operation: null, reason_code: 'unavailable' },
    } as never))

    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&visibility=group']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByRole('option', { name: '查看 群友甲 关系雷达' })).toBeVisible()
    await waitFor(() => expect(api.formal).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: 'bot-a', session_id: 'session-a', visibility: 'group', subject_principal_id: 'qq:user:u1' }),
      100,
      0,
      expect.anything(),
      { from_ts: undefined, to_ts: undefined },
    ))

    fireEvent.click(screen.getByRole('button', { name: '信任' }))
    expect(await screen.findByText('被感谢')).toBeVisible()
    expect(screen.queryByRole('button', { name: /校准|保存|提交/ })).not.toBeInTheDocument()
  })

  it('强制自省按钮触发只读重算并重新拉取正式数据', async () => {
    api.refresh.mockResolvedValue({ runtime_refresh: { status: 'refreshed', operation: null, reason_code: null, refreshed_at: 123 } })
    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&visibility=group']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByText('平静')).toBeVisible()
    const loadsBefore = api.formal.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /强制自省/ }))

    await waitFor(() => expect(api.refresh).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: 'bot-a', session_id: 'session-a', visibility: 'group' }),
      { from_ts: undefined, to_ts: undefined },
    ))
    await waitFor(() => expect(api.formal.mock.calls.length).toBeGreaterThan(loadsBefore))
  })

  it('只读展示关切状态标签，不提供结案或状态变更入口', async () => {
    api.formal.mockResolvedValue({
      source: { health: 'healthy', reason_code: null },
      mood: { value: '平静', state: 'known', components: null, policy_version: 'v1', revision: 1, evidence: [] },
      concerns: {
        items: [{
          id: 11,
          topic: '考研结果还没公布',
          status: 'active',
          concern_type: 'follow_up',
          last_triggered: 1720000000,
          revision: 2,
          evidence: [],
        }],
        page: { total: 1, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false },
      },
      timeline: page,
      relationship_history: page,
      soul_context: { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null },
      relationship: { affinity: 0.5, state: 'known', revision: 1, evidence: [], people_ref: null },
      capabilities: { mutate: { available: false, reason_code: 'readonly' }, runtime_refresh: { available: false, reason_code: 'unavailable' } },
      runtime_refresh: { status: 'unavailable', operation: null, reason_code: 'unavailable' },
    })

    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&visibility=group']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByText('考研结果还没公布')).toBeVisible()
    expect(screen.getByText('激活')).toBeVisible()
    expect(screen.getByText('follow_up')).toBeVisible()
    expect(screen.getByText(/只读展示，可作为自然关心方向，不是强制回复指令/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /结案|归档|恢复|推进/ })).not.toBeInTheDocument()
  })
})

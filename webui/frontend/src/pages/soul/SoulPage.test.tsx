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
  return { ...actual, ChartContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>, ChartLegend: () => null, ChartLegendContent: () => null }
})
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return { ...actual, Bar: () => null, BarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>, CartesianGrid: () => null, Line: () => null, LineChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>, Legend: () => null, XAxis: () => null, YAxis: () => null }
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
    expect(api.formal).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-a', session_id: 'session-a' }), 25, 0, expect.anything(), { from_ts: undefined, to_ts: undefined })
  })

  it('展示关系三层轨迹、真实来源和时间范围', async () => {
    api.relationships.mockResolvedValue({ items: [{
      subject_principal_id: 'qq:user:u1',
      person: { user_id: 'u1', display_name: 'Alice', group_id: '1', bot_id: 'bot-a', aliases: [], scope: { user_id: 'u1', group_id: '1', bot_id: 'bot-a' }, scope_key: 'bot-a:session-a:u1', metadata: {}, registry_metadata: {}, person_registry: {}, affinity: null, affinity_status: 'unavailable', affinity_reason_code: 'formal_relationship_projection'
      },
      affinity: 40, state: 'friendly', revision: 3, values: { trust: { dimension: 'trust', automatic_value: 24, manual_adjustment: 5, manual_override: null, effective_value: 29, relationship_revision: 3, evidence: [] } }, evidence: [], object_ref: { ref: 'relationship-ref', kind: 'relationship', locator: 'qq:user:u1' }, calibration: { available: true, reason_code: null }
    }], page })
    api.formal.mockResolvedValue({
      source: { health: 'healthy', reason_code: null },
      mood: { value: '平静', state: 'known', components: null, policy_version: 'v1', revision: 1, evidence: [] },
      concerns: page,
      timeline: page,
      relationship_history: { items: [{ id: 'relationship-event:1', event_id: 1, kind: 'automatic', event_type: 'direct_reply', action: null, dimension: 'trust', delta: 4, reason: '真实消息导致变化', source_episode_id: null, source_memory_id: 101, revision: 2, timestamp: 100, operation_id: null, actor: null, value_layer: 'automatic', before: { dimension: 'trust', automatic_value: 20, manual_adjustment: null, manual_override: null, effective_value: 20 }, after: { dimension: 'trust', automatic_value: 24, manual_adjustment: null, manual_override: null, effective_value: 24 }, evidence: [] }], page },
      soul_context: { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null },
      relationship: { affinity: 40, state: 'friendly', revision: 3, evidence: [], people_ref: { ref: 'relationship-ref', kind: 'relationship', locator: 'qq:user:u1' }, values: { trust: { dimension: 'trust', automatic_value: 24, manual_adjustment: 5, manual_override: null, effective_value: 29, relationship_revision: 3, evidence: [] } }, calibration: { available: true, reason_code: null } },
      capabilities: { mutate: { available: false, reason_code: 'readonly' }, runtime_refresh: { available: false, reason_code: 'unavailable' } },
      runtime_refresh: { status: 'unavailable', operation: null, reason_code: 'unavailable' },
    })
    render(<MemoryRouter initialEntries={['/soul?bot_id=bot-a&session_id=session-a&subject_principal_id=qq:user:u1&from_ts=90&to_ts=110']}><SoulPage /></MemoryRouter>)

    expect(await screen.findByText('关系变化')).toBeVisible()
    expect(screen.getByText('真实消息导致变化')).toBeVisible()
    expect(screen.getByText('memory:101')).toBeVisible()
    expect(screen.getByText('作息与精力')).toBeVisible()
    expect(api.formal).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-a', session_id: 'session-a', subject_principal_id: 'qq:user:u1' }), 25, 0, expect.anything(), { from_ts: 90, to_ts: 110 })
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
})

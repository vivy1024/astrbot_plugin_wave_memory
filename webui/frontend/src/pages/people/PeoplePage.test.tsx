import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

import { PeoplePage } from '@/pages/people/PeoplePage'

const api = vi.hoisted(() => ({
  getScopeOptions: vi.fn(),
  getPeople: vi.fn(),
  getLegacyPeople: vi.fn(),
  getRelationships: vi.fn(),
  getRelationshipHistoricalAudit: vi.fn(),
  getPersonTimeline: vi.fn(),
  clearImpression: vi.fn(),
}))

vi.mock('@/api/options', () => ({
  getScopeOptions: api.getScopeOptions,
  scopeOptionsFor: () => [],
  groupSessionOptions: () => [],
}))

vi.mock('@/api/people', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/people')>()
  return {
    ...actual,
    getPeople: api.getPeople,
    getLegacyPeople: api.getLegacyPeople,
    getRelationships: api.getRelationships,
    getRelationshipHistoricalAudit: api.getRelationshipHistoricalAudit,
    getPersonTimeline: api.getPersonTimeline,
    clearImpression: api.clearImpression,
  }
})

vi.mock('@/components/relationship/RelationshipCalibrationPanel', () => ({ RelationshipCalibrationPanel: () => null }))
vi.mock('@/components/relationship/RelationshipRadarCard', () => ({ RelationshipRadarCard: () => null }))
vi.mock('@/components/relationship/RelationshipTrajectoryCard', () => ({ RelationshipTrajectoryCard: () => null }))
vi.mock('@/components/ui/sheet', () => ({
  Sheet: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetContent: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  SheetHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
  SheetDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
}))

function page(items: unknown[], total = items.length) {
  return {
    items,
    page: {
      total,
      total_status: 'exact',
      reason_code: null,
      limit: 25,
      offset: 0,
      page: 1,
      page_count: 1,
      has_more: false,
    },
  }
}

function timelinePage(items: unknown[], total = items.length) {
  return { ...page(items, total), timeline: 'impression', readonly: true }
}

function timelineEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    user_id: 'u1',
    group_id: 'g1',
    bot_id: 'bot-a',
    kind: 'impression',
    summary: '愿意核对事实',
    detail: '愿意核对事实',
    subject: '',
    predicate: '',
    object: '',
    confidence: null,
    occurred_at: 1_700_000_000,
    created_at: 1_700_000_000,
    event_type: 'impression',
    dimension: '',
    delta: null,
    readonly: true,
    timeline: 'impression',
    ...overrides,
  }
}

function person(overrides: Record<string, unknown> = {}) {
  return {
    user_id: 'u1',
    group_id: 'g1',
    bot_id: 'bot-a',
    display_name: '甲',
    aliases: [],
    interaction_count: 12,
    scope: { user_id: 'u1', group_id: 'g1', bot_id: 'bot-a' },
    scope_key: 'u1|g1|bot-a',
    metadata: {},
    registry_metadata: {},
    person_registry: {},
    affinity: null,
    affinity_status: 'unavailable',
    affinity_reason_code: 'scoped_affinity_projection_unavailable',
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/people?bot_id=bot-a&session_id=qq:group:g1']}>
      <PeoplePage />
    </MemoryRouter>,
  )
}

describe('PeoplePage impression 可见化', () => {
  beforeEach(() => {
    api.getScopeOptions.mockResolvedValue({ sessions: [] })
    api.getPeople.mockResolvedValue(page([person({ metadata: { impression: '说话谨慎但常帮着整理群聊记录的人。曾纠正过我两次事实错误。' } })]))
    api.getRelationships.mockResolvedValue(page([]))
    api.getRelationshipHistoricalAudit.mockResolvedValue(page([], 0))
    api.getPersonTimeline.mockResolvedValue(timelinePage([], 0))
  })

  it('列表展示 impression 摘要列并互链 Bot 经历时间线', async () => {
    renderPage()
    expect(await screen.findByText('说话谨慎但常帮着整理群聊记录的人。曾纠正过我两次事实错误。')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Bot 印象' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Bot 经历时间线' })).toHaveAttribute('href', expect.stringContaining('/soul'))
    await waitFor(() => expect(api.getPeople).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-a', session_id: 'qq:group:g1' })))
  })

  it('高级筛选把条件发给服务端而不是只筛当前页', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: /高级筛选/ }))
    await user.click(screen.getByRole('button', { name: '高好感 ≥15' }))
    await waitFor(() => expect(api.getPeople).toHaveBeenCalledWith(expect.objectContaining({
      bot_id: 'bot-a',
      session_id: 'qq:group:g1',
      relationship_state: 'known',
      min_affinity: '15',
    })))
    await waitFor(() => expect(api.getRelationships).toHaveBeenCalledWith(expect.objectContaining({
      relationship_state: 'known',
      min_affinity: '15',
      limit: 25,
    })))
  })

  it('按好感排序默认从高到低，未记录不会挤到第一页', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: /高级筛选/ }))
    await user.selectOptions(screen.getByLabelText('列表排序字段'), '好感')
    await waitFor(() => expect(api.getPeople).toHaveBeenCalledWith(expect.objectContaining({
      sort_by: 'affinity',
      sort_order: 'desc',
    })))
    expect(screen.getByRole('button', { name: '当前从高到低，点击改为从低到高' })).toHaveTextContent('高→低')
  })

  it('好感从低到高会显式发给服务端', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: /高级筛选/ }))
    await user.selectOptions(screen.getByLabelText('列表排序字段'), '好感')
    await user.click(screen.getByRole('button', { name: '当前从高到低，点击改为从低到高' }))
    await waitFor(() => expect(api.getPeople).toHaveBeenCalledWith(expect.objectContaining({
      sort_by: 'affinity',
      sort_order: 'asc',
    })))
  })

  it('打开详情展示当前印象与内嵌印象时间线面板', async () => {
    const user = userEvent.setup()
    api.getPersonTimeline.mockResolvedValue(timelinePage([
      timelineEvent({ id: 2, kind: 'person_fact', summary: '别名 时雨', event_type: 'person_fact' }),
      timelineEvent({ id: 1, kind: 'affinity', summary: '好感从 0 升至 2', dimension: 'trust', delta: 2, event_type: 'deep_talk' }),
    ], 2))
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))

    await waitFor(() => expect(screen.getByText('Bot 当前印象')).toBeInTheDocument())
    // 列表摘要列与详情卡片各渲染一份当前印象
    expect(screen.getAllByText('说话谨慎但常帮着整理群聊记录的人。曾纠正过我两次事实错误。').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/我眼中的他；下方可按类型与关键词浏览/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '清除当前印象' })).toBeInTheDocument()
    // 内嵌实时面板按 user_id 拉全量并渲染
    await waitFor(() => expect(api.getPersonTimeline).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: 'bot-a', session_id: 'qq:group:g1', user_id: 'u1' }),
      expect.anything(),
    ))
    expect(await screen.findByText('别名 时雨')).toBeInTheDocument()
    expect(screen.getByText('好感从 0 升至 2')).toBeInTheDocument()
    expect(screen.getAllByText('人物事实').length).toBeGreaterThan(0)
    expect(screen.getByText(/trust\+2/)).toBeInTheDocument()
  })

  it('内嵌面板支持类型筛选并打到服务端', async () => {
    const user = userEvent.setup()
    api.getPersonTimeline.mockResolvedValue(timelinePage([]))
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))
    await waitFor(() => expect(api.getPersonTimeline).toHaveBeenCalled())
    api.getPersonTimeline.mockClear()
    await user.selectOptions(screen.getByLabelText('印象类型筛选'), 'person_fact')
    await waitFor(() => expect(api.getPersonTimeline).toHaveBeenCalledWith(
      expect.objectContaining({ user_id: 'u1', kind: 'person_fact' }),
      expect.anything(),
    ))
  })

  it('内嵌面板无记录时展示中性空态', async () => {
    const user = userEvent.setup()
    api.getPersonTimeline.mockResolvedValue(timelinePage([], 0))
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))
    expect(await screen.findByText(/该筛选下暂无印象记录/)).toBeInTheDocument()
  })

  it('清除当前印象需要原因并调用审计接口', async () => {
    const user = userEvent.setup()
    api.clearImpression.mockResolvedValue({ ok: true, operation: { kind: 'people.impression.clear', status: 'succeeded' }, revision: 1 })
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('印象过期需要重看')
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))
    await user.click(screen.getByRole('button', { name: '清除当前印象' }))
    await waitFor(() => expect(api.clearImpression).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: 'bot-a', session_id: 'qq:group:g1' }),
      { user_id: 'u1', reason: '印象过期需要重看' },
    ))
    prompt.mockRestore()
  })

  it('消费关系对象深链并自动打开对应人物详情', async () => {
    const relation = {
      subject_principal_id: 'qq:user:u1',
      person: person(),
      affinity: 12,
      state: 'known',
      revision: 4,
      values: { trust: { effective_value: 25 } },
      evidence: [],
      object_ref: { ref: 'relationship-u1', kind: 'relationship', locator: 'qq:user:u1' },
      calibration: { available: true, reason_code: null },
    }
    api.getRelationships.mockResolvedValue(page([relation]))
    api.getRelationshipHistoricalAudit.mockResolvedValue(page([], 0))

    render(
      <MemoryRouter initialEntries={['/people?bot_id=bot-a&session_id=qq:group:g1&visibility=group&ref=relationship-u1&object_id=qq:user:u1&subject_principal_id=qq:user:u1']}>
        <PeoplePage />
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getAllByText('用户 ID').length).toBeGreaterThan(1))
    expect(screen.getAllByText('u1').length).toBeGreaterThan(1)
    await waitFor(() => expect(api.getRelationships).toHaveBeenCalledWith(expect.objectContaining({
      bot_id: 'bot-a',
      session_id: 'qq:group:g1',
      visibility: 'group',
      user_id: 'u1',
    })))
  })
})

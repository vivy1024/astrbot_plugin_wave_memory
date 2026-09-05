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
  clearImpression: vi.fn(),
}))

vi.mock('@/api/options', () => ({
  getScopeOptions: api.getScopeOptions,
  scopeOptionsFor: () => [],
  groupSessionOptions: () => [],
}))

vi.mock('@/api/people', () => ({
  getPeople: api.getPeople,
  getLegacyPeople: api.getLegacyPeople,
  getRelationships: api.getRelationships,
  getRelationshipHistoricalAudit: api.getRelationshipHistoricalAudit,
  clearImpression: api.clearImpression,
}))

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
    api.getRelationshipHistoricalAudit.mockResolvedValue(
      page([], 0),
    )
  })

  it('列表展示 impression 摘要列', async () => {
    renderPage()
    expect(await screen.findByText('说话谨慎但常帮着整理群聊记录的人。曾纠正过我两次事实错误。')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Bot 印象' })).toBeInTheDocument()
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

  it('打开详情展示完整印象，没有印象时不渲染该区块', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))

    await waitFor(() => expect(screen.getByText('Bot 当前印象')).toBeInTheDocument())
    // 列表摘要列与详情卡片各自渲染一份印象内容
    expect(screen.getAllByText('说话谨慎但常帮着整理群聊记录的人。曾纠正过我两次事实错误。')).toHaveLength(2)
    expect(screen.getByText('由主对话自动生成、随互动更新；历史只读，不进入关系事件')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '清除当前印象' })).toBeInTheDocument()
  })

  it('详情展示只读印象时间线', async () => {
    const user = userEvent.setup()
    api.getPeople.mockResolvedValue(page([person({
      metadata: {
        impression: '愿意核对事实',
        impression_history: [
          { text: '说话谨慎', updated_at: 1_700_000_000 },
          { text: '常纠正事实', cleared_at: 1_700_086_400, cleared_reason: '过期重看' },
        ],
      },
    })]))
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))
    expect(await screen.findByText('印象时间线（新在上，只读）')).toBeInTheDocument()
    expect(screen.getByText('常纠正事实')).toBeInTheDocument()
    expect(screen.getByText('说话谨慎')).toBeInTheDocument()
    expect(screen.getByText(/清除原因：过期重看/)).toBeInTheDocument()
  })

  it('只有当前印象没有历史时仍展示时间线空态', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(api.getPeople).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: '查看 甲 详情' }))
    expect(await screen.findByText('印象时间线（新在上，只读）')).toBeInTheDocument()
    expect(screen.getByText(/尚无演变记录/)).toBeInTheDocument()
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

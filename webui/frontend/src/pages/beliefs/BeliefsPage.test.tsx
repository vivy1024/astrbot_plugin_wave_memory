import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { BeliefGroup, BeliefItem, BeliefsResponse } from '@/api/beliefs'
import { setViewport } from '@/test/setup'
import { BeliefsPage } from './BeliefsPage'

const mocks = vi.hoisted(() => ({
  listBeliefs: vi.fn(),
  vetoBelief: vi.fn(),
  restoreBelief: vi.fn(),
}))

vi.mock('@/api/beliefs', () => ({
  listBeliefs: mocks.listBeliefs,
  vetoBelief: mocks.vetoBelief,
  restoreBelief: mocks.restoreBelief,
}))

const available = { available: true, reason_code: null }
const unavailable = (reason: string) => ({ available: false, reason_code: reason })

function belief(overrides: Partial<BeliefItem>): BeliefItem {
  return {
    id: 21,
    key: 'belief:21',
    legacy: false,
    bot_id: 'bot-demo',
    content: '我讲冷笑话群里大多不接，他们更吃玩梗',
    category: 'group',
    category_label: '关于群',
    status: 'formed',
    status_label: '已形成',
    strength: 0.72,
    support_days: 2,
    support_dates: ['2026-09-21', '2026-09-24'],
    confirm_runs: 2,
    challenge_count: 0,
    triggers: ['冷笑话', '玩梗'],
    experience_phrase: '来自 9 月下旬的两次经历',
    first_seen_at: 1_790_000_000,
    last_confirmed_at: 1_790_200_000,
    revision: 3,
    evidence: [
      { diary_id: 12, date: '2026-09-24', session_id: 'qq:group:g1', group_name: '摸鱼测试群', title: '冷笑话又冷场', polarity: 'support' },
      { diary_id: 11, date: '2026-09-21', session_id: 'qq:group:g1', group_name: '摸鱼测试群', title: '第一次讲冷笑话', polarity: 'support' },
    ],
    history: [
      { id: 1, event_type: 'sprouted', label: '萌芽', from_status: null, to_status: 'sprout', from_label: null, to_label: '萌芽', reason: '第一次冷场', actor: 'reflection', occurred_at: 1_790_000_000, evidence: [] },
      { id: 2, event_type: 'confirmed', label: '被印证', from_status: 'sprout', to_status: 'formed', from_label: '萌芽', to_label: '已形成', reason: '又冷场了', actor: 'reflection', occurred_at: 1_790_200_000, evidence: [] },
    ],
    actions: { veto: available, restore: unavailable('belief_not_vetoed') },
    ...overrides,
  }
}

function group(status: BeliefGroup['status'], label: string, items: BeliefItem[]): BeliefGroup {
  return { status, label, description: `${label}的说明`, count: items.length, items }
}

function response(): BeliefsResponse {
  const vetoed = belief({ id: 22, key: 'belief:22', content: '冷笑话是最好的笑话', status: 'vetoed', status_label: '已否决', revision: 5,
    actions: { veto: unavailable('belief_already_vetoed'), restore: available } })
  const legacy = belief({ id: 7, key: 'legacy:7', legacy: true, content: '测试群友甲偏爱射击游戏', category: null, category_label: null,
    status: 'legacy_archived', status_label: '已归档（旧版人物判断）', legacy_type_label: '人物判断', legacy_status: 'active',
    session_id: 'qq:group:g1', revision: undefined, evidence: [], history: [],
    actions: { veto: unavailable('legacy_readonly'), restore: unavailable('legacy_readonly') } })
  return {
    bot_id: 'bot-demo',
    groups: [
      group('sprout', '萌芽', [belief({ id: 23, key: 'belief:23', content: '直播时我更喜欢盖房子', status: 'sprout', status_label: '萌芽' })]),
      group('formed', '已形成', [belief({})]),
      group('stable', '稳固', []),
      group('shaken', '动摇中', []),
      group('faded', '已淡出', []),
      group('vetoed', '已否决', [vetoed]),
      group('legacy_archived', '已归档（旧版人物判断）', [legacy]),
    ],
    counts: { sprout: 1, formed: 1, stable: 0, shaken: 0, faded: 0, vetoed: 1, legacy_archived: 1 },
    policy: { form_min_days: 2, stable_min_days: 4, decay_days: 30, max_active: 20 },
    reflection: { enabled: true, every_n_days: 1, last_run: { id: 1, run_date: '2026-09-28', status: 'succeeded', attempts: 1, diary_ids: [12], counts: {}, error: '', started_at: 1, finished_at: 2 }, recent_runs: [] },
    capabilities: { veto: available, restore: available, create: unavailable('belief_formed_by_reflection_only'), edit: unavailable('x'), physical_delete: unavailable('y') },
    generated_at: 1_790_300_000,
  }
}

beforeEach(() => {
  setViewport(1280)
  vi.clearAllMocks()
  mocks.listBeliefs.mockResolvedValue(response())
  mocks.vetoBelief.mockResolvedValue({ ok: true, operation: { kind: 'belief.veto', status: 'succeeded' }, revision: 4 })
  mocks.restoreBelief.mockResolvedValue({ ok: true, operation: { kind: 'belief.restore', status: 'succeeded' }, revision: 6 })
})

function renderPage(entry = '/beliefs?bot_id=bot-demo') {
  return render(<MemoryRouter initialEntries={[entry]}><BeliefsPage /></MemoryRouter>)
}

describe('BeliefsPage 信念 v2', () => {
  it('按 Bot 读取并按状态分组，默认落在「已形成」', async () => {
    renderPage()
    expect(await screen.findByText('我讲冷笑话群里大多不接，他们更吃玩梗')).toBeVisible()
    expect(mocks.listBeliefs).toHaveBeenCalledWith('bot-demo')
    for (const label of ['萌芽', '已形成', '稳固', '动摇中', '已淡出', '已否决', '已归档（旧版人物判断）']) {
      expect(screen.getByRole('tab', { name: new RegExp(label.replace(/[（）]/g, '.')) })).toBeInTheDocument()
    }
    expect(screen.getByRole('tab', { name: /已形成/ })).toHaveAttribute('data-state', 'active')
    expect(screen.getByText('每天日记后反思')).toBeVisible()
    expect(screen.getByText('2026-09-28')).toBeVisible()
    expect(screen.getByText('来自 9 月下旬的两次经历')).toBeVisible()
    expect(screen.getByText('冷笑话')).toBeVisible()
  })

  it('展开后显示依据日记与变化历史', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByText('依据日记与变化历史'))
    const evidence = document.querySelector('[data-slot="belief-evidence"]') as HTMLElement
    expect(within(evidence).getByText('冷笑话又冷场')).toBeVisible()
    expect(within(evidence).getByText('日记 #12')).toBeVisible()
    const history = document.querySelector('[data-slot="belief-history"]') as HTMLElement
    expect(within(history).getByText('被印证')).toBeVisible()
    expect(within(history).getByText('萌芽 → 已形成')).toBeVisible()
    expect(within(history).getByText('又冷场了')).toBeVisible()
  })

  it('一键否决带上版本号，完成后重新读取', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: '否决看法 21' }))
    await waitFor(() => expect(mocks.vetoBelief).toHaveBeenCalledWith(expect.objectContaining({ id: 21, revision: 3 }), 'bot-demo'))
    await waitFor(() => expect(mocks.listBeliefs).toHaveBeenCalledTimes(2))
  })

  it('已否决的可以恢复；旧版人物判断只读', async () => {
    const user = userEvent.setup()
    renderPage('/beliefs?bot_id=bot-demo&status=vetoed')
    await user.click(await screen.findByRole('button', { name: '恢复看法 22' }))
    await waitFor(() => expect(mocks.restoreBelief).toHaveBeenCalledWith(expect.objectContaining({ id: 22, revision: 5 }), 'bot-demo'))

    await user.click(screen.getByRole('tab', { name: /已归档/ }))
    const card = await screen.findByText('测试群友甲偏爱射击游戏')
    const article = card.closest('article') as HTMLElement
    expect(article).toHaveAttribute('data-legacy', 'true')
    expect(within(article).getByText('旧版人物判断')).toBeVisible()
    expect(within(article).getByText('原状态：已生效')).toBeVisible()
    expect(within(article).queryByRole('button')).not.toBeInTheDocument()
  })

  it('没有选 Bot 时提示先选 Bot，不发请求', async () => {
    renderPage('/beliefs')
    expect(await screen.findByText('先选一个 Bot')).toBeVisible()
    expect(mocks.listBeliefs).not.toHaveBeenCalled()
  })

  it('窄屏同样是卡片，不出现表格', async () => {
    setViewport(375)
    renderPage()
    expect(await screen.findByText('我讲冷笑话群里大多不接，他们更吃玩梗')).toBeVisible()
    expect(document.querySelector('article[data-slot="belief-card"]')).toBeInTheDocument()
    expect(document.querySelector('table')).not.toBeInTheDocument()
  })
})

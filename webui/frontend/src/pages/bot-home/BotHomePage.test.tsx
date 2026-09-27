import { MemoryRouter } from 'react-router-dom'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { BotHomePayload } from '@/api/botHome'
import { BotHomePage, describeMood } from './BotHomePage'

const api = vi.hoisted(() => ({ getBotHome: vi.fn() }))
vi.mock('@/api/botHome', () => ({ getBotHome: api.getBotHome }))

const SESSION_A = '羽书:group:111'
const SESSION_B = '羽书:group:222'
const placeA = { session_id: SESSION_A, visibility: 'group', group_id: '111', group_name: '3-5层群', label: '3-5层群' }
const placeB = { session_id: SESSION_B, visibility: 'group', group_id: '222', group_name: null, label: '222' }

function payload(overrides: Partial<BotHomePayload> = {}): BotHomePayload {
  const now = Date.now() / 1000
  return {
    bot: { db_id: 'yushu', name: '羽书' },
    window: { days: 1, from_ts: now - 86400, to_ts: now },
    memories: {
      status: 'ready',
      reason_code: null,
      total: 3140,
      quarantined: 87,
      by_source: { chat: 1836, core: 43, noise: 1261 },
      top_groups: [{ ...placeA, count: 2185 }, { ...placeB, count: 821 }],
      top_speakers: [{ ...placeA, sender_id: '10000004', display_name: '纯白喵', count: 148, last_at: now - 60 }],
      highlights: [{
        ...placeA,
        id: 659871,
        sender_id: 'bot',
        sender_name: '',
        is_self: true,
        content: '我是羽书，我记住了',
        timestamp: now - 30,
        importance: 1,
        source: 'core',
        memory_type: 'message',
        reason: 'self',
      }],
    },
    soul: {
      status: 'ready',
      reason_code: null,
      mood: { ...placeA, valence: 0.25, arousal: 0.68, cause: '当前群聊互动很密集', observed_at: now - 10, policy_version: 'scoped-mood/v2' },
      concerns: [{ ...placeA, id: 7, topic: '芝麻姐姐的救命之恩', intensity: 0.75, urgency: 0.75, status: 'active', concern_type: 'social_anchor', last_triggered: now - 100 }],
      concerns_total: 1,
      scope: { session_id: SESSION_A, visibility: 'group' },
    },
    relationships: {
      status: 'ready',
      reason_code: null,
      people_changed: 2,
      truncated: false,
      items: [{
        ...placeA,
        subject_principal_id: '羽书:user:10000003',
        user_id: '10000003',
        display_name: '测试群友乙',
        event_count: 3,
        abs_delta: 4,
        dimensions: { fun: 4, hostility: 1 },
        affinity_before: 18,
        affinity_after: 19,
        state: 'neutral',
        latest_reason: '借题发挥拱火整活',
        latest_event_type: 'joke',
        latest_at: now - 300,
      }],
    },
    learned: {
      status: 'ready',
      reason_code: null,
      facts: {
        status: 'ready', reason_code: null, total: 3, by_status: { pending: 2, active: 1 },
        samples: [{ ...placeA, id: 17364, text: '测试群友甲 烤洋芋的烹饪细节是 使用了喷油壶喷油', status: 'pending', created_at: now - 500 }],
      },
      beliefs: { status: 'ready', reason_code: null, total: 0, by_status: {}, samples: [] },
      jargon: {
        status: 'ready', reason_code: null, total: 1, by_status: { pending: 1 },
        samples: [{ ...placeA, id: 20303, text: '器灵：羽书的外号', status: 'pending', created_at: now - 400 }],
      },
      experiences: {
        status: 'ready', reason_code: null, total: 1, by_status: { recorded: 1 },
        samples: [{ ...placeB, id: 1018, text: '一起看了流星雨', status: 'recorded', created_at: now - 900 }],
      },
    },
    replies: {
      status: 'ready',
      reason_code: null,
      items: [{
        ...placeA,
        trace_id: 'cortico-1790511759',
        timestamp: now - 20,
        latency_ms: 288.6,
        status: 'ok',
        sender_id: '10000001',
        sender_name: '测试',
        message_preview: '羽书你还记得上次说的复活吗',
        source: 'cortico',
        hit_channels: [{ channel: 'memory', item_count: 5, tokens: 90 }, { channel: 'fts5', item_count: 10, tokens: 189 }],
      }],
    },
    meta: { generated_at: now, elapsed_ms: 12.3, section_ms: { memories: 10 } },
    ...overrides,
  }
}

function renderPage(entry = '/bot-home?bot_id=yushu') {
  return render(<MemoryRouter initialEntries={[entry]}><BotHomePage /></MemoryRouter>)
}

function linkParams(element: HTMLElement): { path: string; params: URLSearchParams } {
  const href = element.closest('a')?.getAttribute('href') ?? ''
  const url = new URL(href, 'http://local')
  return { path: url.pathname, params: url.searchParams }
}

beforeEach(() => {
  api.getBotHome.mockReset()
})

describe('BotHomePage', () => {
  it('一句话概括 + 心情 + 四个区块，并用 Bot 作用域请求', async () => {
    api.getBotHome.mockResolvedValue(payload())
    renderPage()

    expect(await screen.findByTestId('bot-home-summary')).toHaveTextContent('羽书 今天记住了 3,140 条，最活跃的是 3-5层群')
    expect(api.getBotHome).toHaveBeenCalledWith(expect.objectContaining({ botId: 'yushu', days: 1 }))
    expect(screen.getByTestId('bot-home-mood')).toHaveTextContent('愉快 · 活跃')
    expect(screen.getByText('当前群聊互动很密集')).toBeVisible()
    expect(screen.getByText('芝麻姐姐的救命之恩')).toBeVisible()
    expect(screen.getByText('今天记住的')).toBeVisible()
    expect(screen.getByText('关系变化')).toBeVisible()
    expect(screen.getByText('新学到的')).toBeVisible()
    expect(screen.getByText('最近的回复')).toBeVisible()
    expect(screen.getByText('和 2 个人的关系有变化，新学到 5 条，最近 1 次回复有记录。')).toBeVisible()
  })

  it('每条都深链接到现有页面并带上 bot_id / session_id / visibility', async () => {
    api.getBotHome.mockResolvedValue(payload())
    renderPage()

    const highlight = linkParams(await screen.findByText('我是羽书，我记住了'))
    expect(highlight.path).toBe('/memories')
    expect(Object.fromEntries(highlight.params)).toEqual({ bot_id: 'yushu', session_id: SESSION_A, visibility: 'group', memory_id: '659871' })

    const group = linkParams(screen.getAllByText('3-5层群').find((node) => node.closest('a')?.getAttribute('href')?.startsWith('/memories'))!)
    expect(Object.fromEntries(group.params)).toEqual({ bot_id: 'yushu', session_id: SESSION_A, visibility: 'group' })

    const speaker = linkParams(screen.getByText('纯白喵'))
    expect(speaker.path).toBe('/people')
    expect(speaker.params.get('search')).toBe('10000004')
    expect(speaker.params.get('session_id')).toBe(SESSION_A)

    const person = linkParams(screen.getByText('测试群友乙'))
    expect(person.path).toBe('/people')
    expect(person.params.get('search')).toBe('10000003')

    const fact = linkParams(screen.getByText('测试群友甲 烤洋芋的烹饪细节是 使用了喷油壶喷油'))
    expect(fact.path).toBe('/facts')
    expect(fact.params.get('visibility')).toBe('group')

    const pendingFacts = linkParams(within(screen.getByTestId('learned-facts')).getByText('待审 2'))
    expect(pendingFacts.params.get('status')).toBe('pending')

    const jargon = linkParams(screen.getByText('器灵：羽书的外号'))
    expect(jargon.path).toBe('/jargon')

    const beliefs = linkParams(within(screen.getByTestId('learned-beliefs')).getByText('信念'))
    expect(beliefs.path).toBe('/beliefs')
    expect(beliefs.params.get('session_id')).toBe(SESSION_A)

    const experience = linkParams(screen.getByText('一起看了流星雨'))
    expect(experience.path).toBe('/knowledge/experiences')
    expect(experience.params.get('session_id')).toBe(SESSION_B)

    const trace = linkParams(screen.getByText(/羽书你还记得上次说的复活吗/))
    expect(trace.path).toBe('/observatory')
    expect(Object.fromEntries(trace.params)).toEqual({ bot_id: 'yushu', session_id: SESSION_A, visibility: 'group', trace_id: 'cortico-1790511759' })

    const concern = linkParams(screen.getByText('芝麻姐姐的救命之恩'))
    expect(concern.path).toBe('/soul')
  })

  it('关系变化显示好感前后与维度增减，敌意上升标成负面', async () => {
    api.getBotHome.mockResolvedValue(payload())
    renderPage()

    expect(await screen.findByText('有趣 +4')).toHaveClass('text-primary')
    expect(screen.getByText('敌意 +1')).toHaveClass('text-destructive')
    expect(screen.getByText(/好感 18/)).toHaveTextContent('好感 18 → 19')
    expect(screen.getByText('最近：借题发挥拱火整活')).toBeVisible()
  })

  it('加载中显示骨架', () => {
    api.getBotHome.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByRole('status', { name: '正在汇总 Bot 主页' })).toBeInTheDocument()
  })

  it('请求失败显示错误并可重试', async () => {
    api.getBotHome.mockRejectedValueOnce(new Error('服务端炸了'))
    api.getBotHome.mockResolvedValueOnce(payload())
    renderPage()

    expect(await screen.findByText('Bot 主页加载失败')).toBeVisible()
    expect(screen.getByText('服务端炸了')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('bot-home-summary')).toBeVisible()
    expect(api.getBotHome).toHaveBeenCalledTimes(2)
  })

  it('空数据时每块都有空状态，不伪造数字', async () => {
    api.getBotHome.mockResolvedValue(payload({
      memories: { status: 'ready', reason_code: null, total: 0, quarantined: 0, by_source: {}, top_groups: [], top_speakers: [], highlights: [] },
      soul: { status: 'ready', reason_code: null, mood: null, concerns: [], concerns_total: 0, scope: null },
      relationships: { status: 'ready', reason_code: null, items: [], people_changed: 0, truncated: false },
      learned: {
        status: 'ready', reason_code: null,
        facts: { status: 'ready', reason_code: null, total: 0, by_status: {}, samples: [] },
        beliefs: { status: 'ready', reason_code: null, total: 0, by_status: {}, samples: [] },
        jargon: { status: 'ready', reason_code: null, total: 0, by_status: {}, samples: [] },
        experiences: { status: 'unavailable', reason_code: 'table_missing', total: null, by_status: {}, samples: [] },
      },
      replies: { status: 'ready', reason_code: null, items: [] },
    }))
    renderPage()

    expect(await screen.findByTestId('bot-home-summary')).toHaveTextContent('羽书 今天还没有记住新的东西')
    expect(screen.getByText('今天还没有新记忆')).toBeVisible()
    expect(screen.getByText('还没有心情记录')).toBeVisible()
    expect(screen.getByText('关系没有变化')).toBeVisible()
    expect(screen.getByText('还没有注入记录')).toBeVisible()
    expect(within(screen.getByTestId('learned-experiences')).getByText('数据源未就绪')).toBeVisible()
    expect(screen.getAllByText('没有新增。')).toHaveLength(3)
  })

  it('单个区块失败只影响它自己', async () => {
    api.getBotHome.mockResolvedValue(payload({ soul: { status: 'error', reason_code: 'soul_read_failed' } }))
    renderPage()

    expect(await screen.findByText('这一块暂时读不到')).toBeVisible()
    expect(screen.getByTestId('bot-home-summary')).toHaveTextContent('3,140')
    expect(screen.getByText('我是羽书，我记住了')).toBeVisible()
  })

  it('没有选 Bot 时提示选择，不发请求', () => {
    renderPage('/bot-home')
    expect(screen.getByText('先选一个 Bot')).toBeVisible()
    expect(api.getBotHome).not.toHaveBeenCalled()
  })

  it('切换时间窗口重新请求并写入 URL', async () => {
    api.getBotHome.mockResolvedValue(payload())
    renderPage()
    await screen.findByTestId('bot-home-summary')

    fireEvent.click(screen.getByRole('button', { name: '近 7 天' }))
    await waitFor(() => expect(api.getBotHome).toHaveBeenLastCalledWith(expect.objectContaining({ botId: 'yushu', days: 7 })))
    expect(screen.getByRole('button', { name: '近 7 天' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('describeMood', () => {
  it('把愉悦度/激活度翻成人话', () => {
    expect(describeMood(0.6, 0.2)).toBe('很开心 · 安静')
    expect(describeMood(0, 0.4)).toBe('平静 · 平稳')
    expect(describeMood(-0.8, 0.7)).toBe('低落 · 活跃')
    expect(describeMood(null, null)).toBe('心情未知')
  })
})

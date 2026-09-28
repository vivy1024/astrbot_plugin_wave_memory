import { forwardRef, useImperativeHandle } from 'react'
import { configure, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RecallReplayEvent, RecallReplayPayload } from '@/api/tagGraph'
import { StagePage } from './StagePage'

configure({ asyncUtilTimeout: 5000 })

const api = vi.hoisted(() => ({ getRecallReplay: vi.fn(), getScopeOptions: vi.fn() }))
const scene = vi.hoisted(() => ({ pulses: 0, dimmed: false }))

vi.mock('@/api/tagGraph', () => ({ getRecallReplay: api.getRecallReplay }))
vi.mock('@/api/options', () => ({ getScopeOptions: api.getScopeOptions }))
vi.mock('@/lib/graph/stage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/graph/stage')>()),
  POLL_MS: 30, IDLE_STEP_MS: 400, IDLE_REST_MS: 60_000, LIVE_FOCUS_MS: 5_000,
}))
vi.mock('@/pages/graph/showcase/ShowcaseScene', () => ({
  ShowcaseScene: forwardRef(function FakeScene(props: { dimmed: boolean }, ref) {
    scene.dimmed = props.dimmed
    useImperativeHandle(ref, () => ({ pulse: () => { scene.pulses += 1 }, focus: () => undefined, resetCamera: () => undefined }))
    return <div data-testid="scene" />
  }),
}))

function tag(id: number, name: string) {
  return {
    id: `tag:${id}`, locator: id, name, type: 'topic', description: '', confidence: 1, metadata: {}, status: 'active', revision: 1,
    memory_count: 3, frequency: 3, source_counts: {}, sources: [], associated_memories: [],
    in_degree: 0, out_degree: 0, in_weight: 0, out_weight: 0, ref: `oref.${id}`, read_only: true as const,
  }
}

function ev(id: string, ts: number, session: string, message: string): RecallReplayEvent {
  return { trace_id: id, timestamp: ts, sender_name: '观众甲', message_preview: message, memory_count: 2, tags: ['tag:1'], memories: [], session_id: session }
}

function payload(events: RecallReplayEvent[], graph = true): RecallReplayPayload {
  return {
    graph: graph ? {
      nodes: [tag(1, '缺氧'), tag(2, '火锅')], edges: [], layers: ['cooccurrence'], available_layers: ['cooccurrence'], layer_counts: {},
      scope: { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group' }, read_only: true, generated_at: 100, warnings: [],
      pulse: { enabled: false, half_life_hours: 72 },
    } : null,
    events,
    window: { from: 0, to: 100, hours: 24 },
    stats: { event_count: events.length, memory_hits: 0, tagged_memory_hits: 0, recalled_tags: 0, recalled_tags_on_graph: null, events_with_tags: 0 },
    read_only: true,
    generated_at: 100,
  }
}

const SCOPE = 'bot_id=yushu&session_id=%E7%BE%BD%E4%B9%A6%3Agroup%3A1'

function renderStage(search: string) {
  return render(<MemoryRouter initialEntries={[`/stage?${search}`]}><StagePage /></MemoryRouter>)
}

beforeEach(() => {
  scene.pulses = 0
  api.getScopeOptions.mockReset().mockResolvedValue({ bots: [{ db_id: 'yushu', name: '羽书' }], sessions: [], channels: [], generated_at: 0, source: { health: 'healthy', reason_code: null } })
  api.getRecallReplay.mockReset()
})

describe('StagePage', () => {
  it('实时：新弹幕点亮星空并显示原话；轮询只取增量、不重建图，覆盖全部会话', async () => {
    api.getRecallReplay
      .mockResolvedValueOnce(payload([ev('old', 90, '羽书:group:1', '旧消息')]))
      .mockResolvedValueOnce(payload([ev('old', 90, '羽书:group:1', '旧消息'), ev('live1', 101, 'bilibili:group:24292304', '主播玩缺氧吗')], false))
      .mockResolvedValue(payload([], false))
    renderStage(SCOPE)
    const caption = await screen.findByText(/主播玩缺氧吗/)
    expect(caption.closest('[data-slot="stage-caption"]')?.getAttribute('data-live')).toBe('1')
    expect(screen.getByText('直播弹幕')).toBeInTheDocument()
    expect(screen.getByText(/羽书想起了 2 条记忆：缺氧/)).toBeInTheDocument()
    expect(scene.pulses).toBe(1)
    expect(scene.dimmed).toBe(true)
    expect(api.getRecallReplay.mock.calls[0][1]).toMatchObject({ events: 'bot', hours: 24 })
    const pollCall = api.getRecallReplay.mock.calls[1][1]
    expect(pollCall).toMatchObject({ events: 'bot', graph: false, since: 70 })
  })

  it('群聊事件默认不上屏原话与昵称', async () => {
    api.getRecallReplay
      .mockResolvedValueOnce(payload([]))
      .mockResolvedValueOnce(payload([ev('g1', 101, '羽书:group:1', '私密的群聊内容')], false))
      .mockResolvedValue(payload([], false))
    renderStage(SCOPE)
    expect(await screen.findByText('群里有人说了一句话')).toBeInTheDocument()
    expect(screen.queryByText(/私密的群聊内容/)).toBeNull()
    expect(screen.queryByText(/观众甲/)).toBeNull()
  })

  it('空闲一段时间后回放最近点亮过的回忆', async () => {
    api.getRecallReplay
      .mockResolvedValueOnce(payload([ev('b1', 90, 'bilibili:group:9', '上午的弹幕')]))
      .mockResolvedValue(payload([], false))
    renderStage(`${SCOPE}&idle=0.2`)
    const caption = await screen.findByText(/上午的弹幕/)
    expect(caption.closest('[data-slot="stage-caption"]')?.getAttribute('data-live')).toBe('0')
    expect(screen.getByText('回放')).toBeInTheDocument()
  })

  it('缺参数时给出用法提示', () => {
    renderStage('bot_id=yushu')
    expect(screen.getByText(/舞台页需要 bot_id 与 session_id/)).toBeInTheDocument()
  })
})

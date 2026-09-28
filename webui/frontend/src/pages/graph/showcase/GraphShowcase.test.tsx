import { forwardRef, useImperativeHandle } from 'react'
import { act, configure, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RecallReplayPayload } from '@/api/tagGraph'
import type { ReplayPulse } from '@/lib/graph/showcase'
import { GraphShowcase } from './GraphShowcase'

configure({ asyncUtilTimeout: 5000 })

const scene = vi.hoisted(() => ({ pulses: [] as ReplayPulse[], focused: [] as string[], props: {} as Record<string, unknown>, select: null as null | ((id: string | null) => void) }))
const api = vi.hoisted(() => ({ getRecallReplay: vi.fn() }))

vi.mock('@/api/tagGraph', () => ({ getRecallReplay: api.getRecallReplay }))
vi.mock('./ShowcaseScene', () => ({
  ShowcaseScene: forwardRef(function FakeScene(props: Record<string, unknown>, ref) {
    scene.props = props
    scene.select = props.onSelectNode as (id: string | null) => void
    useImperativeHandle(ref, () => ({
      pulse: (pulse: ReplayPulse) => scene.pulses.push(pulse),
      focus: (id: string) => scene.focused.push(id),
      resetCamera: () => undefined,
    }))
    return <div data-testid="scene" />
  }),
}))

function tag(id: number, name: string) {
  return {
    id: `tag:${id}`, locator: id, name, type: 'topic', description: '', confidence: 1, metadata: {}, status: 'active', revision: 1,
    memory_count: 3, frequency: 3, source_counts: {}, sources: [], associated_memories: [{ id: 1, content: `${name}的记忆`, sender: 'A', timestamp: 0, importance: 0.5, version: 1, tag_source: 'automatic', relevance: 1 }],
    in_degree: 0, out_degree: 0, in_weight: 0, out_weight: 0, ref: `oref.${id}`, read_only: true as const,
  }
}

const replay: RecallReplayPayload = {
  graph: {
    nodes: [tag(1, '缺氧'), tag(2, '蛊仙')],
    edges: [{ id: 'cooccurrence:1:2', source: 'tag:1', target: 'tag:2', layer: 'cooccurrence', kind: 'directed_cooccurrence', type: 'x', label: '', weight: 0.8, frequency: 1, confidence: 1, latest_ts: 0, source_kind: 'x', read_only: true }],
    layers: ['cooccurrence'], available_layers: ['cooccurrence'], layer_counts: {},
    scope: { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group' }, read_only: true, generated_at: 0, warnings: [], pulse: { enabled: false, half_life_hours: 72 },
  },
  events: [
    { trace_id: 't1', timestamp: 1_790_000_000, sender_name: 'Alice', message_preview: '缺氧怎么开局', memory_count: 4, tags: ['tag:1', 'tag:2'], memories: [{ id: 9, channel: 'memory', preview: '之前聊过缺氧', tags: ['tag:1'] }] },
    { trace_id: 't2', timestamp: 1_790_000_100, sender_name: 'Bob', message_preview: '在吗', memory_count: 2, tags: [], memories: [] },
  ],
  window: { from: 0, to: 1, hours: 24 },
  stats: { event_count: 2, memory_hits: 6, tagged_memory_hits: 4, recalled_tags: 2, recalled_tags_on_graph: 2, events_with_tags: 1 },
  read_only: true,
  generated_at: 0,
}

const SCOPE = { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group' as const }

beforeEach(() => {
  scene.pulses = []
  scene.focused = []
  api.getRecallReplay.mockReset().mockResolvedValue(replay)
})

describe('GraphShowcase', () => {
  it('星空模式：按窗口取回忆数据，显示统计，不压暗、自动环绕', async () => {
    const onHoursChange = vi.fn()
    render(<GraphShowcase scope={SCOPE} botName="羽书" hours={24} onHoursChange={onHoursChange} onOpenIn2D={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('showcase-stats').textContent).toContain('2 次回复'))
    expect(api.getRecallReplay).toHaveBeenCalledWith(SCOPE, expect.objectContaining({ hours: 24 }))
    expect(screen.getByText('羽书 的记忆星空')).toBeInTheDocument()
    expect(scene.props.dimmed).toBe(false)
    expect(scene.props.autoRotate).toBe(true)
    await userEvent.setup().click(screen.getByRole('radio', { name: '3 天' }))
    expect(onHoursChange).toHaveBeenCalledWith(72)
  })

  it('回忆回放：逐条点亮真实注入想起的标签，可逐条前进，没有标签的事件如实说明', async () => {
    const user = userEvent.setup()
    render(<GraphShowcase scope={SCOPE} botName="羽书" hours={24} onHoursChange={vi.fn()} onOpenIn2D={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('radio', { name: '回忆回放' })).toBeEnabled())
    await user.click(screen.getByRole('radio', { name: '回忆回放' }))
    expect(scene.props.dimmed).toBe(true)
    await user.click(screen.getByRole('button', { name: '暂停' }))
    await user.click(screen.getByRole('button', { name: '下一条' }))
    const card = await screen.findByTestId('replay-event')
    expect(card.textContent).toContain('Alice：缺氧怎么开局')
    expect(card.textContent).toContain('羽书想起了 4 条记忆，点亮 2 个标签：缺氧、蛊仙')
    expect(scene.pulses.at(-1)).toMatchObject({ lit: ['tag:1', 'tag:2'] })
    expect(scene.pulses.at(-1)!.links).toHaveLength(1)
    expect(screen.getByTestId('replay-position').textContent).toBe('1 / 2')

    await user.click(screen.getByRole('button', { name: '下一条' }))
    expect(screen.getByTestId('replay-event').textContent).toContain('没有标签')
    expect(screen.getByRole('button', { name: '重播' })).toBeInTheDocument()
  })

  it('点节点：镜头飞过去、停止环绕，卡片可跳到平面图', async () => {
    const onOpenIn2D = vi.fn()
    render(<GraphShowcase scope={SCOPE} botName="羽书" hours={24} onHoursChange={vi.fn()} onOpenIn2D={onOpenIn2D} />)
    await screen.findByTestId('scene')
    act(() => scene.select?.('tag:2'))
    expect(scene.focused).toEqual(['tag:2'])
    expect(scene.props.autoRotate).toBe(false)
    expect(screen.getByText('蛊仙的记忆')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: /在平面图中查看/ }))
    expect(onOpenIn2D).toHaveBeenCalledWith('tag:2')
  })
})

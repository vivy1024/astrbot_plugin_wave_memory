import { MemoryRouter } from 'react-router-dom'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { TagGraphPage } from './TagGraphPage'

const api = vi.hoisted(() => ({ getTagGraph: vi.fn(), getTagGraphDetail: vi.fn(), findTagGraphPath: vi.fn() }))
let isMobile = false
let reducedMotion = false

vi.mock('@/api/tagGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/tagGraph')>(),
  getTagGraph: (...args: unknown[]) => api.getTagGraph(...args),
  getTagGraphDetail: (...args: unknown[]) => api.getTagGraphDetail(...args),
  findTagGraphPath: (...args: unknown[]) => api.findTagGraphPath(...args),
}))
vi.mock('@/api/options', () => ({
  getScopeOptions: vi.fn(async () => ({ bots: [], sessions: [] })),
  scopeOptionsFor: vi.fn(() => []),
  groupSessionOptions: vi.fn(() => []),
}))
vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => isMobile }))
vi.mock('@/components/shared', () => ({
  ScopeSelect: ({ label }: { label: string }) => <div data-testid={`scope-${label}`}>{label}</div>,
  ObjectDeepLink: ({ children }: { children: unknown }) => <a href="#memory">{String(children)}</a>,
}))

const nodes = [
  {
    id: 'tag:1', locator: 1, name: 'Alpha', type: 'topic', description: 'Alpha desc', confidence: 0.9,
    metadata: {}, status: 'active', revision: 1, memory_count: 2, frequency: 2,
    source_counts: { automatic: 2 }, sources: ['automatic'],
    associated_memories: [{ id: 10, content: '真实 Alpha 记忆', sender: 'Alice', timestamp: 100, importance: 0.8, source: 'chat', version: 1, tag_source: 'automatic', relevance: 0.9, ref: 'm-alpha', object_ref: { ref: 'm-alpha', kind: 'memory', locator: 10 } }],
    in_degree: 0, out_degree: 1, in_weight: 0, out_weight: 0.8, ref: 'oref-alpha', object_ref: { ref: 'oref-alpha', kind: 'tag', locator: 1 }, read_only: true as const,
  },
  {
    id: 'tag:2', locator: 2, name: 'Beta', type: 'person', description: '', confidence: 0.8,
    metadata: {}, status: 'active', revision: 1, memory_count: 1, frequency: 1,
    source_counts: { manual: 1 }, sources: ['manual'], associated_memories: [],
    in_degree: 1, out_degree: 0, in_weight: 0.8, out_weight: 0, ref: 'oref-beta', object_ref: { ref: 'oref-beta', kind: 'tag', locator: 2 }, read_only: true as const,
  },
]
const edges = [{ id: 'cooccurrence:1:2', source: 'tag:1', target: 'tag:2', layer: 'cooccurrence' as const, kind: 'directed_cooccurrence' as const, type: 'ordinal_cooccurrence', label: '序位共现', weight: 0.8, frequency: 2, confidence: 0.75, latest_ts: 100, source_kind: 'effective_memory_tags', pulse_energy: 0.6, pulse_decay: 0.75, read_only: true as const }]
const graph = { nodes, edges, layers: ['cooccurrence', 'relations'] as const, available_layers: ['cooccurrence', 'relations'] as const, layer_counts: { cooccurrence: { nodes: 2, edges: 1 }, relations: { nodes: 0, edges: 0 } }, scope: { bot_id: 'bot-a', session_id: 'qq:group:g1', visibility: 'group' as const }, read_only: true as const, generated_at: 100, warnings: [], pulse: { enabled: true, half_life_hours: 72 } }

beforeEach(() => {
  isMobile = false
  reducedMotion = false
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: reducedMotion, addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
  api.getTagGraph.mockReset().mockResolvedValue(graph)
  api.getTagGraphDetail.mockReset().mockResolvedValue({ item: nodes[0], scope: graph.scope, read_only: true })
  api.findTagGraphPath.mockReset().mockResolvedValue({ found: true, path: ['tag:1', 'tag:2'], nodes, edges, layers: ['cooccurrence', 'relations'], scope: graph.scope, read_only: true })
})

describe('TagGraphPage', () => {
  it('从 URL 深链选择 Tag，并把当前可见图层传给 ObjectRef 路径查询', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/tags/graph?bot_id=bot-a&session_id=qq%3Agroup%3Ag1&visibility=group&ref=oref-alpha&pulse=1']}><TagGraphPage /></MemoryRouter>)

    expect(await screen.findByText('Alpha desc')).toBeVisible()
    expect(screen.getByText('真实 Alpha 记忆')).toBeVisible()
    expect(screen.queryByRole('button', { name: /删除|重命名|改类型/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '设为起点' }))
    await user.click(screen.getByRole('button', { name: '选择标签 Beta' }))
    await user.click(screen.getByRole('button', { name: '设为终点' }))
    await user.click(screen.getByRole('button', { name: '查询当前可见图层' }))

    await waitFor(() => expect(api.findTagGraphPath).toHaveBeenLastCalledWith(
      graph.scope,
      expect.objectContaining({ source_ref: 'oref-alpha', target_ref: 'oref-beta', layers: ['cooccurrence', 'relations'] }),
      expect.any(AbortSignal),
    ))

    await user.click(screen.getByRole('button', { name: '显式关系' }))
    await user.click(screen.getByRole('button', { name: '查询当前可见图层' }))
    await waitFor(() => expect(api.findTagGraphPath).toHaveBeenLastCalledWith(
      graph.scope,
      expect.objectContaining({ layers: ['cooccurrence'] }),
      expect.any(AbortSignal),
    ))
  })

  it('移动端降级为可访问列表而不是 SVG 云图', async () => {
    isMobile = true
    const view = render(<MemoryRouter initialEntries={['/tags/graph?bot_id=bot-a&session_id=qq%3Agroup%3Ag1']}><TagGraphPage /></MemoryRouter>)
    expect(await screen.findByLabelText('标签关系图移动端列表')).toBeVisible()
    expect(view.container.querySelector('[data-tag-graph-mode="list"]')).toBeInTheDocument()
    expect(view.container.querySelector('[data-tag-graph-mode="svg"]')).not.toBeInTheDocument()
    expect(view.container.querySelector('canvas')).not.toBeInTheDocument()
    expect(api.getTagGraph).toHaveBeenCalledWith(graph.scope, expect.objectContaining({
      maxNodes: 150, layers: ['cooccurrence', 'relations'], includePulse: false, signal: expect.any(AbortSignal),
    }))
    await userEvent.setup().click(screen.getByRole('button', { name: /Alpha/ }))
    expect(await screen.findByText('Alpha desc')).toBeVisible()
  })

  it.each([false, true])('Canvas 按减少动态效果偏好绘制脉冲：%s', async (reduced) => {
    reducedMotion = reduced
    const context = {
      save: vi.fn(), restore: vi.fn(), scale: vi.fn(), clearRect: vi.fn(),
      translate: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(),
      moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), fill: vi.fn(),
      arc: vi.fn(), fillText: vi.fn(), createRadialGradient: vi.fn(() => ({ addColorStop: vi.fn() })),
    }
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const frames = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(1)
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    const view = render(<MemoryRouter initialEntries={['/tags/graph?bot_id=bot-a&session_id=qq%3Agroup%3Ag1&pulse=1']}><TagGraphPage /></MemoryRouter>)
    try {
      expect(await screen.findByLabelText('Tag 关系图画布')).toBeVisible()
      if (reduced) expect(screen.getByText('已遵循减少动态效果偏好')).toBeVisible()
      else expect(screen.queryByText('已遵循减少动态效果偏好')).not.toBeInTheDocument()
      expect(view.container.querySelector('animate')).not.toBeInTheDocument()
      const frame = frames.mock.calls.at(-1)?.[0]
      expect(frame).toBeDefined()
      act(() => frame!(0))
      // 每个节点画光晕和核心；只有普通模式额外绘制边上的移动脉冲。
      expect(context.arc).toHaveBeenCalledTimes(nodes.length * 2 + (reduced ? 0 : edges.length))
      expect(context.lineTo).toHaveBeenCalledTimes(edges.length)
    } finally {
      view.unmount()
      getContext.mockRestore()
      frames.mockRestore()
      cancel.mockRestore()
    }
  })
})

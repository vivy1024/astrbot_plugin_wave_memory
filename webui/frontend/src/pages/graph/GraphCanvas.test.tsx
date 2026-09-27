import { act, render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { GRAPH_CHROME, nodeColor } from '@/lib/graph-palette'
import { kgFixture, NOW, tagFixture } from '@/lib/graph/fixtures'
import { kgPayloadToGraph } from '@/lib/graph/kg-adapter'
import { tagPayloadToGraph } from '@/lib/graph/tag-adapter'
import { buildView, pairKey } from '@/lib/graph/views'
import { FakeSigma, FakeSupervisor, resetFakes } from '@/test/fake-sigma'
import { GraphCanvas } from './GraphCanvas'

vi.mock('sigma', async () => (await import('@/test/fake-sigma')).sigmaModule)
vi.mock('graphology-layout-forceatlas2/worker', async () => (await import('@/test/fake-sigma')).workerModule)

const empty = new Set<string>()

function kgView() {
  return buildView(kgPayloadToGraph(kgFixture()), { view: 'all', now: NOW, windowHours: 168, maxNodes: 300 }).graph
}

function renderCanvas(overrides: Partial<Parameters<typeof GraphCanvas>[0]> = {}) {
  const props = {
    graph: kgView(),
    theme: 'light' as const,
    glow: false,
    selectedNode: null,
    pathNodes: empty,
    pathPairs: empty,
    onSelectNode: vi.fn(),
    onLayoutStatus: vi.fn(),
    ...overrides,
  }
  const view = render(<GraphCanvas {...props} />)
  return { ...view, props, rerenderWith: (next: Partial<typeof props>) => view.rerender(<GraphCanvas {...props} {...next} />) }
}

describe('GraphCanvas', () => {
  beforeEach(() => resetFakes())

  it('只创建一个 sigma 渲染器，并启动一次 FA2 worker 布局（节点程序统一为 circle）', () => {
    const { props } = renderCanvas()
    expect(FakeSigma.instances).toHaveLength(1)
    const sigma = FakeSigma.instances[0]
    expect(sigma.settings.labelSize).toBeGreaterThanOrEqual(12)
    expect(FakeSupervisor.instances).toHaveLength(1)
    expect(FakeSupervisor.instances[0].running).toBe(true)
    expect(FakeSupervisor.instances[0].params.getEdgeWeight).toBe('weight')
    expect(props.onLayoutStatus).toHaveBeenCalledWith(expect.objectContaining({ running: true }))
    // 业务类型（person / topic）不能漏给 sigma 当节点程序名
    expect(sigma.node('entity:羽书').type).toBe('circle')
    expect(sigma.node('entity:羽书').color).toBe(nodeColor('person', 'light'))
  })

  it('布局能量归零后自动停止并销毁 worker', async () => {
    const { props } = renderCanvas()
    const supervisor = FakeSupervisor.instances[0]
    act(() => { for (let i = 0; i < 200 && supervisor.running; i += 1) supervisor.step() })
    await act(async () => { await Promise.resolve() })
    expect(supervisor.killed).toBe(true)
    expect(props.onLayoutStatus).toHaveBeenLastCalledWith(expect.objectContaining({ running: false, reason: 'converged' }))
  })

  it('hover 只改 reducer 状态并 refresh(skipIndexation)，不重建渲染器', () => {
    renderCanvas()
    const sigma = FakeSigma.instances[0]
    sigma.refresh.mockClear()
    act(() => sigma.emit('enterNode', { node: 'entity:测试群友甲' }))
    expect(sigma.refresh).toHaveBeenCalledWith({ skipIndexation: true })
    expect(FakeSigma.instances).toHaveLength(1)
    // 自己高亮，邻居常显标签，其他节点淡出
    expect(sigma.node('entity:测试群友甲')).toMatchObject({ highlighted: true, forceLabel: true })
    expect(sigma.node('entity:烤洋芋').forceLabel).toBe(true)
    expect(sigma.node('entity:空气炸锅').color).toBe(GRAPH_CHROME.light.dimNode)
    expect(sigma.edge('tagrel:4').color).toBe(GRAPH_CHROME.light.dimEdge)
    act(() => sigma.emit('leaveNode', { node: 'entity:测试群友甲' }))
    expect(sigma.node('entity:空气炸锅').color).toBe(nodeColor('entity', 'light'))
  })

  it('点击节点 / 空白处回调选中', () => {
    const { props } = renderCanvas()
    const sigma = FakeSigma.instances[0]
    act(() => sigma.emit('clickNode', { node: 'entity:羽书' }))
    expect(props.onSelectNode).toHaveBeenCalledWith('entity:羽书')
    act(() => sigma.emit('clickStage'))
    expect(props.onSelectNode).toHaveBeenLastCalledWith(null)
  })

  it('换视图用 setGraph + 重新布局；换主题 / 光晕 / 路径只 refresh', async () => {
    const { rerenderWith } = renderCanvas()
    const sigma = FakeSigma.instances[0]
    const tags = buildView(tagPayloadToGraph(tagFixture()), { view: 'all', now: NOW, windowHours: 168, maxNodes: 300 }).graph
    rerenderWith({ graph: tags })
    await act(async () => { await Promise.resolve() })
    expect(FakeSigma.instances).toHaveLength(1)
    expect(sigma.setGraphCalls).toBe(1)
    expect(FakeSupervisor.instances).toHaveLength(2)
    expect(FakeSupervisor.instances[0].killed).toBe(true)

    sigma.refresh.mockClear()
    rerenderWith({ graph: tags, theme: 'dark' })
    expect(sigma.setSetting).toHaveBeenCalledWith('labelColor', { color: GRAPH_CHROME.dark.label })
    expect(sigma.refresh).toHaveBeenCalled()
    expect(sigma.node('tag:3').color).toBe(nodeColor('topic', 'dark'))

    rerenderWith({ graph: tags, theme: 'dark', glow: true })
    expect(sigma.node('tag:3').size).toBeGreaterThan(tags.getNodeAttribute('tag:3', 'size'))
    // 光晕 = 按源节点类型着色的实色连线（不用 rgba：sigma 预乘 alpha 会让它发白）
    expect(sigma.edge('cooccurrence:4:3').color).toMatch(/^#[0-9a-f]{6}$/)
    expect(sigma.edge('cooccurrence:4:3').color).not.toBe(GRAPH_CHROME.dark.edge)

    rerenderWith({ graph: tags, theme: 'dark', pathNodes: new Set(['tag:4', 'tag:3']), pathPairs: new Set([pairKey('tag:4', 'tag:3')]) })
    expect(sigma.edge('cooccurrence:4:3')).toMatchObject({ color: GRAPH_CHROME.dark.path, zIndex: 3 })
    expect(sigma.node('tag:3').forceLabel).toBe(true)
    expect(sigma.node('tag:1').color).toBe(GRAPH_CHROME.dark.dimNode)
    expect(FakeSigma.instances).toHaveLength(1)
  })

  it('卸载时销毁渲染器与 worker', async () => {
    const { unmount } = renderCanvas()
    unmount()
    await Promise.resolve()
    expect(FakeSigma.instances[0].kill).toHaveBeenCalled()
    expect(FakeSupervisor.instances[0].killed).toBe(true)
  })
})

describe('mixHex', () => {
  it('把前景色按比例混进背景，输出不透明 hex', async () => {
    const { mixHex } = await import('./graph-reducers')
    expect(mixHex('#ffffff', '#000000', 0.5)).toBe('#808080')
    expect(mixHex('#2a78d6', '#ffffff', 1)).toBe('#2a78d6')
    expect(mixHex('#2a78d6', '#ffffff', 0)).toBe('#ffffff')
  })
})

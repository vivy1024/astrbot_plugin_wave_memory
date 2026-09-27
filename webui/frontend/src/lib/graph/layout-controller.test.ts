import { describe, expect, it, vi } from 'vitest'

import { kgFixture } from './fixtures'
import { kgPayloadToGraph } from './kg-adapter'
import { defaultLayoutLimits, forceAtlasSettings, LayoutController, type LayoutStatus, type LayoutSupervisor } from './layout-controller'
import { seedPositions } from './seed-layout'
import type { RelationGraph } from './types'

/** 假 worker：每次 step() 模拟 supervisor 收到一轮 FA2 结果并批量写回坐标。 */
class FakeSupervisor implements LayoutSupervisor {
  running = false
  killed = false
  constructor(private readonly graph: RelationGraph, private readonly move: (iteration: number) => number) {}
  private iteration = 0
  start() { this.running = true }
  stop() { this.running = false }
  kill() { this.killed = true; this.running = false }
  isRunning() { return this.running }
  step() {
    if (!this.running) return
    this.iteration += 1
    const delta = this.move(this.iteration)
    this.graph.updateEachNodeAttributes((_id, attrs) => ({ ...attrs, x: attrs.x + delta, y: attrs.y - delta }))
  }
}

function setup(move: (iteration: number) => number, limits = {}, clock = () => 0) {
  const { graph } = kgPayloadToGraph(kgFixture())
  seedPositions(graph)
  let supervisor: FakeSupervisor | null = null
  const statuses: LayoutStatus[] = []
  const controller = new LayoutController(graph, (target) => {
    supervisor = new FakeSupervisor(target, move)
    return supervisor
  }, (status) => statuses.push(status), limits, clock)
  controller.start()
  return { controller, statuses, supervisor: () => supervisor!, graph }
}

/** 复现真实 FA2LayoutSupervisor 的时序：写坐标（触发图事件）之后还会访问 matrices。 */
class ReentrantSupervisor implements LayoutSupervisor {
  running = false
  matrices: { nodes: number } | null = { nodes: 0 }
  constructor(private readonly graph: RelationGraph) {}
  start() { this.running = true }
  stop() { this.running = false }
  kill() { this.matrices = null }
  isRunning() { return this.running }
  handleMessage() {
    if (!this.running) return
    this.graph.updateEachNodeAttributes((_id, attrs) => attrs)
    this.matrices!.nodes += 1
  }
}

describe('LayoutController', () => {
  it('在 supervisor 的消息回调里收敛时不会提前销毁 matrices', async () => {
    const { graph } = kgPayloadToGraph(kgFixture())
    seedPositions(graph)
    let supervisor: ReentrantSupervisor | null = null
    const controller = new LayoutController(graph, (target) => (supervisor = new ReentrantSupervisor(target)), () => {}, { minIterations: 1, stableIterations: 1 })
    controller.start()
    expect(() => { for (let i = 0; i < 5; i += 1) supervisor!.handleMessage() }).not.toThrow()
    await Promise.resolve()
    expect(supervisor!.matrices).toBeNull()
  })

  it('位移（能量）连续低于阈值后判定收敛，并停止、销毁 worker', async () => {
    // 前 30 步大幅移动，之后几乎不动
    const { controller, statuses, supervisor } = setup((i) => (i <= 30 ? 20 : 0), { minIterations: 10, stableIterations: 5, maxIterations: 500 })
    expect(controller.isRunning).toBe(true)
    for (let i = 0; i < 100 && controller.isRunning; i += 1) supervisor().step()
    const last = statuses.at(-1)!
    expect(last).toMatchObject({ running: false, reason: 'converged' })
    expect(last.iterations).toBe(35)
    expect(supervisor().running).toBe(false)
    // kill 延后到当前调用栈之后（真实 supervisor 正处在 handleMessage 里）
    await Promise.resolve()
    expect(supervisor().killed).toBe(true)
    // 停止后再收到的更新不会被计数
    supervisor().step()
    expect(statuses.at(-1)!.iterations).toBe(35)
  })

  it('能量缓慢衰减时按「相对峰值」判定收敛，而不是在头几步被绝对阈值误判', () => {
    // 位移按 0.95^i 衰减：约 30 步后降到峰值的 20% 以下，再连续 15 步
    const { statuses, supervisor, controller } = setup((i) => 20 * 0.95 ** i, { minIterations: 10, maxIterations: 1000 })
    for (let i = 0; i < 1000 && controller.isRunning; i += 1) supervisor().step()
    const last = statuses.at(-1)!
    expect(last.reason).toBe('converged')
    expect(last.iterations).toBeGreaterThan(35)
    expect(last.iterations).toBeLessThan(80)
  })

  it('一直在动时到迭代上限停止', () => {
    const { statuses, supervisor, controller } = setup(() => 50, { maxIterations: 25 })
    for (let i = 0; i < 100 && controller.isRunning; i += 1) supervisor().step()
    expect(statuses.at(-1)).toMatchObject({ running: false, reason: 'max-iterations', iterations: 25 })
  })

  it('超过时间上限停止', () => {
    let now = 0
    const { statuses, supervisor, controller } = setup(() => 50, { maxDurationMs: 1000, maxIterations: 10_000 }, () => now)
    for (let i = 0; i < 100 && controller.isRunning; i += 1) { now += 100; supervisor().step() }
    expect(statuses.at(-1)).toMatchObject({ running: false, reason: 'timeout', iterations: 10 })
  })

  it('手动停止与空图', async () => {
    const { controller, statuses, supervisor } = setup(() => 5)
    controller.stop()
    expect(statuses.at(-1)).toMatchObject({ running: false, reason: 'stopped' })
    await Promise.resolve()
    expect(supervisor().killed).toBe(true)

    const { graph } = kgPayloadToGraph({ ...kgFixture(), edges: [] })
    const factory = vi.fn()
    const onStatus = vi.fn()
    new LayoutController(graph, factory, onStatus).start()
    expect(factory).not.toHaveBeenCalled()
    expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ running: false, reason: 'empty' }))
  })

  it('迭代上限随规模增长并封顶；大图打开 Barnes-Hut', () => {
    expect(defaultLayoutLimits(100).maxIterations).toBe(350)
    expect(defaultLayoutLimits(1000).maxIterations).toBe(800)
    expect(defaultLayoutLimits(5000).maxIterations).toBe(800)
    expect(forceAtlasSettings(100).barnesHutOptimize).toBe(false)
    expect(forceAtlasSettings(1000).barnesHutOptimize).toBe(true)
  })
})

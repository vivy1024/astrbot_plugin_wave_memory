import type { RelationGraph } from './types'

/** FA2LayoutSupervisor 的最小接口（便于在测试里注入假 worker）。 */
export interface LayoutSupervisor {
  start(): void
  stop(): void
  kill(): void
  isRunning(): boolean
}

export type LayoutSupervisorFactory = (graph: RelationGraph, settings: Record<string, unknown>) => LayoutSupervisor

export type LayoutStopReason = 'converged' | 'max-iterations' | 'timeout' | 'stopped' | 'empty'

export interface LayoutStatus {
  running: boolean
  iterations: number
  elapsedMs: number
  reason?: LayoutStopReason
}

export interface LayoutLimits {
  /** 迭代下限：避免初始几步位移小被误判收敛。 */
  minIterations: number
  maxIterations: number
  /** 墙钟上限（毫秒）：保护低端机器；触发时布局结果可能与上次略有不同。 */
  maxDurationMs: number
  /**
   * 能量 = 平均单步位移 / 包围盒边长。FA2 在真实数据上的能量衰减很慢（1000 节点约 1600 步才降到峰值的 30%），
   * 绝对阈值会在头几步就误判收敛，所以用「相对峰值」：能量 < 峰值 × relativeThreshold 视为已稳定。
   */
  relativeThreshold: number
  /** 绝对下限：能量低于它（几乎不动）直接视为稳定，覆盖没有明显峰值的小图。 */
  absoluteThreshold: number
  /** 需要连续多少步低于阈值才算收敛。 */
  stableIterations: number
}

/** 迭代上限随规模增长：300 节点约 450 步，1000 节点 800 步封顶。 */
export function defaultLayoutLimits(order: number): LayoutLimits {
  return {
    minIterations: 60,
    maxIterations: Math.max(300, Math.min(800, Math.round(300 + order * 0.5))),
    maxDurationMs: 8_000,
    relativeThreshold: 0.2,
    absoluteThreshold: 2e-5,
    stableIterations: 15,
  }
}

/**
 * 与 graphology-layout-forceatlas2 的 inferSettings 同思路；另外：
 * - outboundAttractionDistribution：缓解超级枢纽把所有点吸成一团；
 * - adjustSizes：按节点半径防重叠（标签图里同一条记忆的 3 个标签互相全连，不开会叠成一坨）；
 * - edgeWeightInfluence 0.6：权重仍起作用，但不至于把强边两端压成一个点。
 */
export function forceAtlasSettings(order: number): Record<string, unknown> {
  return {
    barnesHutOptimize: order > 250,
    barnesHutTheta: 0.6,
    strongGravityMode: true,
    gravity: 0.08,
    scalingRatio: 12,
    // inferSettings 用 1 + ln(n)；这里减半多一点，让大图在时间上限内走完主要形变
    slowDown: 1 + 0.4 * Math.log(Math.max(order, 1)),
    outboundAttractionDistribution: true,
    adjustSizes: true,
    edgeWeightInfluence: 0.6,
    linLogMode: false,
  }
}

/**
 * 管理一次 ForceAtlas2 Web Worker 布局，并在收敛时自动停止、销毁 worker。
 *
 * 监听 graph 的 eachNodeAttributesUpdated（supervisor 每收到一轮 worker 结果就批量写回一次坐标），
 * 每轮计算能量（平均位移 / 包围盒）：连续 stableIterations 轮低于「峰值 × relativeThreshold」
 * 或绝对下限 ⇒ converged；
 * 超过 maxIterations ⇒ max-iterations；超过 maxDurationMs ⇒ timeout。
 * 停止后不再有任何 worker 消息，sigma 也就不再重绘，静止时 CPU 接近 0。
 */
export class LayoutController {
  private supervisor: LayoutSupervisor | null = null
  private previous: Float64Array | null = null
  private ids: string[] = []
  private iterations = 0
  private stable = 0
  private peak = 0
  private startedAt = 0
  private finished = false
  private readonly limits: LayoutLimits
  private readonly graph: RelationGraph
  private readonly factory: LayoutSupervisorFactory
  private readonly onStatus: (status: LayoutStatus) => void
  private readonly clock: () => number
  private readonly onUpdate = () => this.tick()

  constructor(
    graph: RelationGraph,
    factory: LayoutSupervisorFactory,
    onStatus: (status: LayoutStatus) => void,
    limits?: Partial<LayoutLimits>,
    clock: () => number = () => performance.now(),
  ) {
    this.graph = graph
    this.factory = factory
    this.onStatus = onStatus
    this.clock = clock
    this.limits = { ...defaultLayoutLimits(graph.order), ...limits }
  }

  start(): void {
    if (this.supervisor || this.finished) return
    if (this.graph.order < 2 || this.graph.size === 0) {
      this.finish('empty')
      return
    }
    this.ids = this.graph.nodes()
    this.previous = this.snapshot()
    this.startedAt = this.clock()
    this.graph.on('eachNodeAttributesUpdated', this.onUpdate)
    this.supervisor = this.factory(this.graph, forceAtlasSettings(this.graph.order))
    this.supervisor.start()
    this.onStatus({ running: true, iterations: 0, elapsedMs: 0 })
  }

  /** 用户手动停止或组件卸载。 */
  stop(reason: LayoutStopReason = 'stopped'): void {
    if (this.finished) return
    this.finish(reason)
  }

  get isRunning(): boolean {
    return Boolean(this.supervisor) && !this.finished
  }

  private snapshot(): Float64Array {
    const values = new Float64Array(this.ids.length * 2)
    this.ids.forEach((id, index) => {
      const attrs = this.graph.getNodeAttributes(id)
      values[index * 2] = attrs.x
      values[index * 2 + 1] = attrs.y
    })
    return values
  }

  private tick(): void {
    if (this.finished || !this.previous) return
    this.iterations += 1
    const current = this.snapshot()
    let moved = 0
    let minX = Infinity
    let maxX = -Infinity
    let minY = Infinity
    let maxY = -Infinity
    for (let index = 0; index < this.ids.length; index += 1) {
      const x = current[index * 2]
      const y = current[index * 2 + 1]
      moved += Math.hypot(x - this.previous[index * 2], y - this.previous[index * 2 + 1])
      if (x < minX) minX = x
      if (x > maxX) maxX = x
      if (y < minY) minY = y
      if (y > maxY) maxY = y
    }
    this.previous = current
    const extent = Math.max(maxX - minX, maxY - minY, 1e-6)
    const energy = moved / Math.max(1, this.ids.length) / extent
    // 前 5 步是从种子位置起跳的加速段，不计入峰值
    if (this.iterations > 5) this.peak = Math.max(this.peak, energy)
    const calm = energy < this.limits.absoluteThreshold || (this.peak > 0 && energy < this.peak * this.limits.relativeThreshold)
    this.stable = calm ? this.stable + 1 : 0
    const elapsedMs = this.clock() - this.startedAt
    if (this.iterations >= this.limits.minIterations && this.stable >= this.limits.stableIterations) {
      this.finish('converged')
    } else if (this.iterations >= this.limits.maxIterations) {
      this.finish('max-iterations')
    } else if (elapsedMs >= this.limits.maxDurationMs) {
      this.finish('timeout')
    } else if (this.iterations % 10 === 0) {
      this.onStatus({ running: true, iterations: this.iterations, elapsedMs })
    }
  }

  private finish(reason: LayoutStopReason): void {
    this.finished = true
    this.graph.removeListener('eachNodeAttributesUpdated', this.onUpdate)
    const supervisor = this.supervisor
    this.supervisor = null
    if (supervisor) {
      // finish() 可能正处在 supervisor.handleMessage → assignLayoutChanges → 图事件 的同步调用栈里；
      // 此时立刻 kill() 会把 matrices 置空，handleMessage 随后写 matrices.nodes 就会抛错。
      // 先 stop()（后续消息被忽略），等当前调用栈结束再 kill() 销毁 worker。
      supervisor.stop()
      queueMicrotask(() => supervisor.kill())
    }
    this.onStatus({
      running: false,
      iterations: this.iterations,
      elapsedMs: this.startedAt ? this.clock() - this.startedAt : 0,
      reason,
    })
  }
}

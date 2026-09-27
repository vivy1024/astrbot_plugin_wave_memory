import { vi } from 'vitest'

/**
 * jsdom 没有 WebGL：用一个记录调用的假 sigma 与假 FA2 worker 替身。
 * 用法：在测试文件里
 *   vi.mock('sigma', async () => (await import('@/test/fake-sigma')).sigmaModule)
 *   vi.mock('graphology-layout-forceatlas2/worker', async () => (await import('@/test/fake-sigma')).workerModule)
 */
type Listener = (payload: { node?: string }) => void

export class FakeSigma {
  static instances: FakeSigma[] = []
  graph: any
  container: HTMLElement
  settings: Record<string, any>
  listeners = new Map<string, Listener[]>()
  refresh = vi.fn()
  kill = vi.fn()
  resize = vi.fn()
  setSetting = vi.fn((key: string, value: unknown) => { this.settings[key] = value; return this })
  camera = { setState: vi.fn(), animate: vi.fn(), animatedZoom: vi.fn(), animatedUnzoom: vi.fn(), animatedReset: vi.fn() }
  setGraphCalls = 0

  constructor(graph: any, container: HTMLElement, settings: Record<string, any>) {
    this.graph = graph
    this.container = container
    this.settings = settings
    FakeSigma.instances.push(this)
  }

  on(event: string, listener: Listener) {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener])
    return this
  }

  emit(event: string, payload: { node?: string } = {}) {
    for (const listener of this.listeners.get(event) ?? []) listener(payload)
  }

  getGraph() { return this.graph }
  setGraph(graph: any) { this.graph = graph; this.setGraphCalls += 1 }
  getCamera() { return this.camera }
  getNodeDisplayData(id: string) {
    return this.graph.hasNode(id) ? { x: 0.5, y: 0.5, size: 4 } : undefined
  }

  /** 按当前 reducer 计算某个节点的显示数据。 */
  node(id: string) {
    return this.settings.nodeReducer(id, this.graph.getNodeAttributes(id))
  }

  edge(key: string) {
    return this.settings.edgeReducer(key, this.graph.getEdgeAttributes(key))
  }
}

export class FakeSupervisor {
  static instances: FakeSupervisor[] = []
  running = false
  killed = false
  graph: any
  params: any
  constructor(graph: any, params: any) {
    this.graph = graph
    this.params = params
    FakeSupervisor.instances.push(this)
  }
  start() { this.running = true }
  stop() { this.running = false }
  kill() { this.killed = true; this.running = false }
  isRunning() { return this.running }
  /** 模拟一轮 worker 结果：坐标不变 ⇒ 能量为 0。 */
  step() {
    if (!this.running) return
    this.graph.updateEachNodeAttributes((_id: string, attrs: any) => attrs)
  }
}

export function resetFakes() {
  FakeSigma.instances = []
  FakeSupervisor.instances = []
}

export const sigmaModule = { default: FakeSigma }
export const workerModule = { default: FakeSupervisor }

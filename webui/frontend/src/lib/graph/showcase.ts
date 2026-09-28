/**
 * 3D 展示模式（星空 / 回忆回放）的纯数据层：不碰 three.js，便于单测。
 *
 * - 星空：标签图按"被想起的次数"优先取点；节点大小看关联记忆数，亮度看窗口内被想起的次数。
 * - 回放：每个事件 = 一次真实注入。点亮这次想起的记忆所带的标签；这些标签之间已有的关联边上
 *   发出一次脉冲；它们的邻居微微发亮。边与节点都来自图谱数据，不虚构连接。
 */
import type { RecallReplayEvent, RecallReplayPayload } from '@/api/tagGraph'
import { nodeColor, normalizeNodeType } from '@/lib/graph-palette'
import { pairKey } from './views'

export interface ShowcaseNode {
  id: string
  name: string
  type: string
  color: string
  /** 球体半径（场景单位）。 */
  size: number
  memoryCount: number
  /** 窗口内被想起的次数。 */
  recallCount: number
  /** 0–1：星空模式下的基础亮度（被想起越多越亮）。 */
  glow: number
  /** 最近的关联记忆（节点卡片用）。 */
  memories: string[]
}

export interface ShowcaseLink {
  id: string
  source: string
  target: string
  weight: number
}

export interface ShowcaseData {
  nodes: ShowcaseNode[]
  nodeById: Map<string, ShowcaseNode>
  links: ShowcaseLink[]
  /** pairKey → 该对节点之间保留的那条边。 */
  linkByPair: Map<string, ShowcaseLink>
  neighbors: Map<string, Set<string>>
  /** 常显文字的节点。 */
  labelIds: Set<string>
}

export interface ShowcaseOptions {
  /** 每个节点最多保留几条最强的边；不设上限时几百个点会连成一团。 */
  maxLinksPerNode?: number
  labelCount?: number
}

const MIN_SIZE = 1.6
const MAX_SIZE = 7

export function buildShowcaseData(payload: RecallReplayPayload, options: ShowcaseOptions = {}): ShowcaseData {
  const maxLinks = options.maxLinksPerNode ?? 5
  const labelCount = options.labelCount ?? 14

  const recall = new Map<string, number>()
  for (const event of payload.events) for (const id of new Set(event.tags)) recall.set(id, (recall.get(id) ?? 0) + 1)

  const graph = payload.graph ?? { nodes: [], edges: [] }
  const raw = graph.nodes
  const maxMemories = Math.max(1, ...raw.map((node) => node.memory_count || 0))
  const maxRecall = Math.max(1, ...recall.values())
  const nodes: ShowcaseNode[] = raw.map((node) => {
    const type = normalizeNodeType(node.type)
    const recallCount = recall.get(node.id) ?? 0
    return {
      id: node.id,
      name: node.name,
      type,
      color: nodeColor(type, 'dark'),
      size: MIN_SIZE + (MAX_SIZE - MIN_SIZE) * Math.sqrt((node.memory_count || 0) / maxMemories),
      memoryCount: node.memory_count || 0,
      recallCount,
      glow: recallCount ? 0.35 + 0.65 * Math.sqrt(recallCount / maxRecall) : 0.2,
      memories: (node.associated_memories ?? []).slice(0, 3).map((memory) => memory.content),
    }
  })
  const ids = new Set(nodes.map((node) => node.id))

  // 同一对节点只留权重最大的一条边（方向对展示没有意义）
  const strongest = new Map<string, ShowcaseLink>()
  for (const edge of graph.edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target) || edge.source === edge.target) continue
    const key = pairKey(edge.source, edge.target)
    const current = strongest.get(key)
    if (!current || edge.weight > current.weight) {
      strongest.set(key, { id: key, source: edge.source, target: edge.target, weight: edge.weight })
    }
  }
  const byNode = new Map<string, ShowcaseLink[]>()
  for (const link of strongest.values()) {
    for (const end of [link.source, link.target]) {
      const list = byNode.get(end) ?? []
      list.push(link)
      byNode.set(end, list)
    }
  }
  const kept = new Set<string>()
  for (const list of byNode.values()) {
    list.sort((a, b) => b.weight - a.weight || (a.id < b.id ? -1 : 1))
    for (const link of list.slice(0, maxLinks)) kept.add(link.id)
  }
  const links = [...strongest.values()].filter((link) => kept.has(link.id)).sort((a, b) => (a.id < b.id ? -1 : 1))
  const linkByPair = new Map(links.map((link) => [link.id, link]))
  const neighbors = new Map<string, Set<string>>()
  for (const link of links) {
    for (const [a, b] of [[link.source, link.target], [link.target, link.source]]) {
      const set = neighbors.get(a) ?? new Set<string>()
      set.add(b)
      neighbors.set(a, set)
    }
  }

  const labelIds = new Set(
    [...nodes]
      .sort((a, b) => b.recallCount - a.recallCount || b.memoryCount - a.memoryCount || (a.id < b.id ? -1 : 1))
      .slice(0, labelCount)
      .map((node) => node.id),
  )
  return { nodes, nodeById: new Map(nodes.map((node) => [node.id, node])), links, linkByPair, neighbors, labelIds }
}

export interface ReplayPulse {
  /** 这次被想起的节点。 */
  lit: string[]
  /** 被想起的节点之间已有的边：脉冲沿这些边传导。 */
  links: ShowcaseLink[]
  /** 被想起节点的邻居（未被想起）：微微发亮。 */
  halo: string[]
  /** 显示文字的被想起节点（最相关的几个；线上一次常点亮 20 多个，全显示会糊成一片）。 */
  labels: string[]
}

const MAX_HALO = 40
/** 一次最多沿多少条边发脉冲（取最强的）。 */
export const MAX_PULSE_LINKS = 30
export const MAX_PULSE_LABELS = 6

export function eventPulse(event: RecallReplayEvent, data: ShowcaseData): ReplayPulse {
  const lit = [...new Set(event.tags)].filter((id) => data.nodeById.has(id))
  const litSet = new Set(lit)
  const links: ShowcaseLink[] = []
  for (let i = 0; i < lit.length; i += 1) {
    for (let j = i + 1; j < lit.length; j += 1) {
      const link = data.linkByPair.get(pairKey(lit[i], lit[j]))
      if (link) links.push(link)
    }
  }
  links.sort((a, b) => b.weight - a.weight || (a.id < b.id ? -1 : 1))
  links.splice(MAX_PULSE_LINKS)
  const halo: string[] = []
  for (const id of lit) {
    for (const neighbor of data.neighbors.get(id) ?? []) {
      if (halo.length >= MAX_HALO) break
      if (!litSet.has(neighbor) && !halo.includes(neighbor)) halo.push(neighbor)
    }
  }
  // 后端按记忆排名、标签序位给出 tags，靠前的最相关
  return { lit, links, halo, labels: lit.slice(0, MAX_PULSE_LABELS) }
}

/** 按半衰期衰减的亮度；低于 0.01 视为熄灭。 */
export function decayIntensity(value: number, elapsedMs: number, halfLifeMs: number): number {
  const next = value * 0.5 ** (Math.max(0, elapsedMs) / Math.max(1, halfLifeMs))
  return next < 0.01 ? 0 : next
}

export const REPLAY_SPEEDS = [0.5, 1, 2, 4] as const
/** 1 倍速下每个事件停留的时间；没有点亮任何节点的事件只停留 40%。 */
export const REPLAY_STEP_MS = 2200

export function replayDelay(event: RecallReplayEvent | undefined, speed: number): number {
  const base = event && event.tags.length ? REPLAY_STEP_MS : REPLAY_STEP_MS * 0.4
  return base / Math.max(0.1, speed)
}

export const SHOWCASE_WINDOWS_HOURS = [24, 72, 168] as const

export function formatReplayTime(seconds: number): string {
  const date = new Date(seconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

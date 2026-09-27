import { MultiDirectedGraph } from 'graphology'

import type { TagGraphEdge, TagGraphNode, TagGraphPayload } from '@/api/tagGraph'
import { nodeColor, normalizeNodeType, type GraphTheme } from '@/lib/graph-palette'
import type { GraphBundle, GraphEdgeAttributes, GraphNodeAttributes, RelationGraph } from './types'

function positive(value: unknown): number | undefined {
  const num = Number(value)
  return Number.isFinite(num) && num > 0 ? num : undefined
}

function finiteOr(value: unknown, fallback: number): number {
  const num = Number(value)
  return Number.isFinite(num) ? num : fallback
}

/**
 * /api/tag-graph → graphology。
 *
 * 节点时间字段优先用新版后端的 created_at / last_seen_ts / recent_memory_count；
 * 旧后端没有这些字段时，lastSeenAt 退回到「最近 5 条关联记忆」与关联边 latest_ts 的最大值，
 * createdAt 保持缺省（「新出现」视图会明确提示不可用，而不是拿别的字段冒充）。
 */
export function tagPayloadToGraph(payload: TagGraphPayload, theme: GraphTheme = 'light'): GraphBundle<TagGraphNode, TagGraphEdge> {
  const graph: RelationGraph = new MultiDirectedGraph<GraphNodeAttributes, GraphEdgeAttributes>({ allowSelfLoops: false })
  const rawNodes = new Map<string, TagGraphNode>()
  const rawEdges = new Map<string, TagGraphEdge>()
  let anyCreated = false
  let anyLastSeen = false
  let anyRecent = false

  for (const node of payload.nodes ?? []) {
    const id = String(node?.id ?? '').trim()
    if (!id || graph.hasNode(id)) continue
    const type = normalizeNodeType(node.type)
    const createdAt = positive(node.created_at)
    const serverLastSeen = positive(node.last_seen_ts)
    const memoryLastSeen = (node.associated_memories ?? []).reduce<number | undefined>((acc, memory) => {
      const ts = positive(memory?.timestamp)
      return ts === undefined ? acc : Math.max(acc ?? 0, ts)
    }, undefined)
    const recentCount = typeof node.recent_memory_count === 'number' && Number.isFinite(node.recent_memory_count) ? node.recent_memory_count : undefined
    anyCreated ||= createdAt !== undefined
    anyRecent ||= recentCount !== undefined
    graph.addNode(id, {
      label: String(node.name ?? '').trim() || id,
      type,
      size: 4,
      color: nodeColor(type, theme),
      degree: 0,
      importance: Math.max(0, finiteOr(node.memory_count, 0)),
      createdAt,
      lastSeenAt: serverLastSeen ?? memoryLastSeen,
      recentCount,
      x: 0,
      y: 0,
    })
    rawNodes.set(id, node)
  }

  let anyEdgeTime = false
  for (const edge of payload.edges ?? []) {
    const id = String(edge?.id ?? '').trim()
    const source = String(edge?.source ?? '')
    const target = String(edge?.target ?? '')
    if (!id || graph.hasEdge(id) || source === target || !graph.hasNode(source) || !graph.hasNode(target)) continue
    const updatedAt = positive(edge.latest_ts)
    anyEdgeTime ||= updatedAt !== undefined
    const kind = edge.layer === 'relations' ? 'relation' : 'cooccurrence'
    graph.addEdgeWithKey(id, source, target, {
      weight: finiteOr(edge.weight, 0),
      label: String(edge.label ?? ''),
      kind,
      confidence: finiteOr(edge.confidence, 0),
      updatedAt,
      size: 1,
      color: '',
      type: kind === 'relation' ? 'arrow' : 'line',
    })
    rawEdges.set(id, edge)
  }

  graph.forEachNode((id, attrs) => {
    let lastSeen = attrs.lastSeenAt
    if (attrs.recentCount === undefined) {
      // 旧后端：把关联边的最近共现时间也算作「最近出现」。
      graph.forEachEdge(id, (_edge, edgeAttrs) => {
        if (edgeAttrs.updatedAt !== undefined) lastSeen = Math.max(lastSeen ?? 0, edgeAttrs.updatedAt)
      })
    }
    anyLastSeen ||= lastSeen !== undefined
    graph.mergeNodeAttributes(id, { degree: graph.degree(id), lastSeenAt: lastSeen })
  })

  return {
    layer: 'tags',
    graph,
    rawNodes,
    rawEdges,
    total: Number(payload.tag_total ?? graph.order) || graph.order,
    timeFields: {
      nodeCreated: anyCreated,
      nodeCreatedExact: anyCreated,
      nodeLastSeen: anyLastSeen,
      nodeRecentCount: anyRecent,
      edgeTime: anyEdgeTime,
    },
    recentWindowHours: positive(payload.recent_window_hours),
    legendOrder: payload.legend?.types ?? [],
    generatedAt: positive(payload.generated_at),
  }
}

/**
 * 标签路径在服务端的完整图（最多 10000 个标签）上计算，路径上的节点可能不在当前 max_nodes 截取里。
 * 把路径节点 / 边并入一份新的 bundle（不修改原对象），视图才能把它们画出来。
 */
export function mergeTagPath(
  bundle: GraphBundle<TagGraphNode, TagGraphEdge>,
  path: { nodes: TagGraphNode[]; edges: TagGraphEdge[] },
  theme: GraphTheme = 'light',
): GraphBundle<TagGraphNode, TagGraphEdge> {
  const missingNodes = path.nodes.filter((node) => !bundle.graph.hasNode(node.id))
  const missingEdges = path.edges.filter((edge) => !bundle.graph.hasEdge(edge.id))
  if (!missingNodes.length && !missingEdges.length) return bundle
  const extra = tagPayloadToGraph({ ...emptyPayload(), nodes: [...missingNodes, ...path.nodes.filter((node) => bundle.graph.hasNode(node.id))], edges: missingEdges }, theme)
  const graph = bundle.graph.copy() as RelationGraph
  const rawNodes = new Map(bundle.rawNodes)
  const rawEdges = new Map(bundle.rawEdges)
  extra.graph.forEachNode((id, attrs) => {
    if (graph.hasNode(id)) return
    graph.addNode(id, attrs)
    rawNodes.set(id, extra.rawNodes.get(id)!)
  })
  extra.graph.forEachEdge((key, attrs, source, target) => {
    if (graph.hasEdge(key) || !graph.hasNode(source) || !graph.hasNode(target)) return
    graph.addEdgeWithKey(key, source, target, attrs)
    rawEdges.set(key, extra.rawEdges.get(key)!)
  })
  graph.forEachNode((id) => graph.setNodeAttribute(id, 'degree', graph.degree(id)))
  return { ...bundle, graph, rawNodes, rawEdges }
}

function emptyPayload(): TagGraphPayload {
  return {
    nodes: [], edges: [], layers: [], available_layers: [], layer_counts: {},
    scope: { bot_id: '', session_id: '', visibility: 'group' }, read_only: true, generated_at: 0, warnings: [],
    pulse: { enabled: false, half_life_hours: 72 },
  }
}

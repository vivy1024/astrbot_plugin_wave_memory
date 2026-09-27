import { MultiDirectedGraph } from 'graphology'

import type { KgFullPayload, KgGraphEdge, KgGraphNode } from '@/api/kg'
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

/** 语义关系（事实、标签显式关系）画箭头；其余画线。 */
function edgeProgram(kind: string): 'arrow' | 'line' {
  return kind === 'fact' || kind === 'tag_relation' ? 'arrow' : 'line'
}

/**
 * /api/kg/full → graphology。
 *
 * 知识图谱节点本身没有时间字段；节点的 createdAt / lastSeenAt 由关联边推导：
 * createdAt = 最早的 created_ts（新后端）或 ts（旧后端，此时 nodeCreatedExact=false），
 * lastSeenAt = 最晚的 ts（事实 / 关系的 updated_at）。
 */
export function kgPayloadToGraph(payload: KgFullPayload, theme: GraphTheme = 'light'): GraphBundle<KgGraphNode, KgGraphEdge> {
  const graph: RelationGraph = new MultiDirectedGraph<GraphNodeAttributes, GraphEdgeAttributes>({ allowSelfLoops: false })
  const rawNodes = new Map<string, KgGraphNode>()
  const rawEdges = new Map<string, KgGraphEdge>()

  for (const node of payload.nodes ?? []) {
    const id = String(node?.id ?? '').trim()
    if (!id || graph.hasNode(id)) continue
    const type = normalizeNodeType(node.type)
    graph.addNode(id, {
      label: String(node.name ?? '').trim() || id,
      type,
      size: 4,
      color: nodeColor(type, theme),
      degree: 0,
      importance: 0,
      x: 0,
      y: 0,
    })
    rawNodes.set(id, node)
  }

  let anyCreated = false
  let anyTime = false
  for (const edge of payload.edges ?? []) {
    const id = String(edge?.id ?? '').trim()
    const source = String(edge?.s ?? '')
    const target = String(edge?.t ?? '')
    if (!id || graph.hasEdge(id) || source === target || !graph.hasNode(source) || !graph.hasNode(target)) continue
    const createdAt = positive(edge.created_ts)
    const updatedAt = positive(edge.ts)
    anyCreated ||= createdAt !== undefined
    anyTime ||= updatedAt !== undefined || createdAt !== undefined
    const kind = String(edge.kind ?? 'fact')
    graph.addEdgeWithKey(id, source, target, {
      weight: finiteOr(edge.weight ?? edge.w, 1),
      label: String(edge.l ?? ''),
      kind,
      confidence: finiteOr(edge.confidence, finiteOr(edge.weight, 1)),
      createdAt,
      updatedAt,
      size: 1,
      color: '',
      type: edgeProgram(kind),
    })
    rawEdges.set(id, edge)
  }

  graph.forEachNode((id) => {
    let first: number | undefined
    let last: number | undefined
    graph.forEachEdge(id, (_edge, attrs) => {
      const born = attrs.createdAt ?? attrs.updatedAt
      if (born !== undefined) first = first === undefined ? born : Math.min(first, born)
      if (attrs.updatedAt !== undefined) last = last === undefined ? attrs.updatedAt : Math.max(last, attrs.updatedAt)
    })
    const degree = graph.degree(id)
    graph.mergeNodeAttributes(id, { degree, importance: degree, createdAt: first, lastSeenAt: last })
  })

  return {
    layer: 'kg',
    graph,
    rawNodes,
    rawEdges,
    total: Number(payload.node_total ?? graph.order) || graph.order,
    timeFields: {
      nodeCreated: anyTime,
      nodeCreatedExact: anyCreated,
      nodeLastSeen: anyTime,
      nodeRecentCount: false,
      edgeTime: anyTime,
    },
    legendOrder: [],
    generatedAt: positive(payload.generated_at),
  }
}

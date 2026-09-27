import type { RelationGraph } from './types'

/**
 * 在已取回的图上做 BFS 找最短路径（directed=true 时只沿边的方向走）。
 * 同一层里优先走权重更高的边，结果可重复。返回节点序列与每一步使用的边 key。
 */
export function findLocalPath(graph: RelationGraph, from: string, to: string, options: { directed: boolean; maxDepth: number }): { nodes: string[]; edges: string[] } | null {
  if (!graph.hasNode(from) || !graph.hasNode(to)) return null
  if (from === to) return { nodes: [from], edges: [] }
  const parent = new Map<string, { node: string; edge: string } | null>([[from, null]])
  let frontier = [from]
  for (let depth = 0; depth < options.maxDepth && frontier.length && !parent.has(to); depth += 1) {
    const next: string[] = []
    for (const node of frontier) {
      const keys = options.directed ? graph.outEdges(node) : graph.edges(node)
      const ranked = [...keys].sort((a, b) => (graph.getEdgeAttribute(b, 'weight') - graph.getEdgeAttribute(a, 'weight')) || (a < b ? -1 : 1))
      for (const key of ranked) {
        const other = graph.opposite(node, key)
        if (parent.has(other)) continue
        parent.set(other, { node, edge: key })
        next.push(other)
      }
    }
    frontier = next
  }
  if (!parent.has(to)) return null
  const nodes: string[] = []
  const edges: string[] = []
  let current: string | null = to
  while (current) {
    nodes.push(current)
    const step: { node: string; edge: string } | null | undefined = parent.get(current)
    if (!step) break
    edges.push(step.edge)
    current = step.node
  }
  return { nodes: nodes.reverse(), edges: edges.reverse() }
}

import type { EdgeDisplayData, NodeDisplayData } from 'sigma/types'

import { GRAPH_CHROME, nodeColor, type GraphTheme } from '@/lib/graph-palette'
import type { GraphEdgeAttributes, GraphNodeAttributes, RelationGraph } from '@/lib/graph/types'
import { pairKey } from '@/lib/graph/views'

export type NodeData = GraphNodeAttributes & Partial<NodeDisplayData>
export type EdgeData = GraphEdgeAttributes & Partial<EdgeDisplayData>

const MAX_LABEL = 16

export interface ReducerState {
  theme: GraphTheme
  glow: boolean
  hovered: string | null
  hoveredNeighbors: Set<string>
  selected: string | null
  selectedNeighbors: Set<string>
  pathNodes: ReadonlySet<string>
  pathPairs: ReadonlySet<string>
}

/**
 * sigma 的 WebGL 按预乘 alpha 混合，直接给 rgba() 会被算成「偏亮的叠加色」（浅色主题下几乎看不见）。
 * 所以半透明效果一律预先和背景色混成实色 hex。t = 前景色占比。
 */
export function mixHex(foreground: string, background: string, t: number): string {
  const parse = (hex: string) => {
    const value = hex.replace('#', '')
    return value.length === 6 ? [0, 2, 4].map((index) => Number.parseInt(value.slice(index, index + 2), 16)) : null
  }
  const fg = parse(foreground)
  const bg = parse(background)
  if (!fg || !bg) return foreground
  const ratio = Math.max(0, Math.min(1, t))
  return `#${fg.map((channel, index) => Math.round(channel * ratio + bg[index] * (1 - ratio)).toString(16).padStart(2, '0')).join('')}`
}

export function truncate(label: string, max = MAX_LABEL): string {
  const chars = Array.from(label)
  return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : label
}

export function neighborsOf(graph: RelationGraph, id: string | null): Set<string> {
  if (!id || !graph.hasNode(id)) return new Set()
  return new Set(graph.neighbors(id))
}

/** 纯函数形式的 reducer，便于单测；hover / 选中 / 路径只改显示数据，不重建渲染器。 */
export function reduceNode(graph: RelationGraph, state: ReducerState, node: string, data: NodeData): Partial<NodeDisplayData> {
  const chrome = GRAPH_CHROME[state.theme]
  const base = nodeColor(data.type, state.theme)
  const out: Partial<NodeDisplayData> = {
    ...data,
    // 节点属性里的 type 是业务类型（topic / person…），sigma 用 type 选节点程序，这里统一画圆。
    type: 'circle',
    label: truncate(data.label),
    color: data.context ? mixHex(base, chrome.background, 0.4) : base,
    size: state.glow ? data.size * 1.18 : data.size,
    highlighted: false,
  }
  const focus = state.hovered ?? state.selected
  const focusNeighbors = state.hovered ? state.hoveredNeighbors : state.selectedNeighbors
  const pathActive = state.pathNodes.size > 0
  if (pathActive) {
    if (state.pathNodes.has(node)) {
      out.forceLabel = true
      out.zIndex = 3
      out.size = Math.max(out.size ?? 4, 7)
    } else if (!(focus && (node === focus || focusNeighbors.has(node)))) {
      out.color = chrome.dimNode
      out.forceLabel = false
      out.label = null
      out.zIndex = 0
    }
  }
  if (focus && graph.hasNode(focus)) {
    if (node === focus) {
      out.highlighted = true
      out.forceLabel = true
      out.zIndex = 4
      out.size = Math.max(out.size ?? 4, 8)
      out.label = truncate(data.label, 40)
    } else if (focusNeighbors.has(node)) {
      out.forceLabel = true
      out.zIndex = Math.max(out.zIndex ?? 0, 2)
      if (!pathActive || state.pathNodes.has(node)) out.color = data.context ? mixHex(base, chrome.background, 0.6) : base
    } else if (!state.pathNodes.has(node)) {
      out.color = chrome.dimNode
      out.forceLabel = false
      out.label = null
      out.zIndex = 0
    }
  }
  return out
}

export function reduceEdge(graph: RelationGraph, state: ReducerState, edge: string, data: EdgeData): Partial<EdgeDisplayData> {
  const chrome = GRAPH_CHROME[state.theme]
  const [source, target] = graph.extremities(edge)
  const out: Partial<EdgeDisplayData> = {
    ...data,
    type: data.type ?? 'line',
    label: data.label,
    color: state.glow ? mixHex(nodeColor(graph.getNodeAttribute(source, 'type'), state.theme), chrome.background, 0.55) : chrome.edge,
    size: state.glow ? data.size * 1.25 : data.size,
    zIndex: 0,
  }
  const focus = state.hovered ?? state.selected
  if (state.pathNodes.size > 0) {
    if (state.pathPairs.has(pairKey(source, target))) {
      out.color = chrome.path
      out.size = Math.max(out.size ?? 1, 3)
      out.zIndex = 3
      return out
    }
    out.color = chrome.dimEdge
  }
  if (focus && graph.hasNode(focus)) {
    if (source === focus || target === focus) {
      out.color = state.glow ? nodeColor(graph.getNodeAttribute(focus, 'type'), state.theme) : chrome.edgeStrong
      out.size = (out.size ?? 1) + 0.6
      out.zIndex = 2
    } else {
      out.color = chrome.dimEdge
    }
  }
  return out
}


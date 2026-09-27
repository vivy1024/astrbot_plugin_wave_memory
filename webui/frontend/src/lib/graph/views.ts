import { MultiDirectedGraph } from 'graphology'

import { seedPositions } from './seed-layout'
import type {
  GraphBundle,
  GraphEdgeAttributes,
  GraphLayerKind,
  GraphNodeAttributes,
  GraphViewKind,
  RelationGraph,
} from './types'

export const GRAPH_VIEWS: ReadonlyArray<{ id: GraphViewKind; label: string }> = [
  { id: 'strongest', label: '最强关系' },
  { id: 'new', label: '新出现' },
  { id: 'current', label: '当前话题' },
  { id: 'all', label: '全部' },
]

export const WINDOW_OPTIONS_HOURS = [24, 72, 168, 720] as const

export interface ViewOptions {
  view: GraphViewKind
  /** 当前时间（秒）。 */
  now: number
  /** 「新出现」「当前话题」的时间窗口（小时）。 */
  windowHours: number
  maxNodes: number
  /** 低于该权重的边不参与任何视图。 */
  minWeight?: number
  /** 允许的类型；空 / 缺省 = 全部。 */
  types?: ReadonlySet<string> | null
  /** 「最强关系」保留的边数上限。 */
  topEdges?: number
  /** 必须出现在图里的节点（搜索聚焦 / 当前选中），会带上最强的若干邻居。 */
  pinned?: readonly string[]
  /** 路径高亮：节点与完整图里的边 key，会被强制加入视图。 */
  path?: { nodes: readonly string[]; edges: readonly string[] } | null
}

export interface ViewSummary {
  view: GraphViewKind
  /** 顶部一句话：这张图在告诉你什么。 */
  sentence: string
  nodeCount: number
  edgeCount: number
  /** 口径说明 / 回退说明。 */
  note?: string
  /** 数据源缺少该视图需要的字段时的原因（此时图为空）。 */
  unavailable?: string
}

export interface ViewResult {
  graph: RelationGraph
  summary: ViewSummary
}

const PINNED_NEIGHBORS = 10
const CURRENT_MIN_ACTIVE = 3
const CURRENT_FALLBACK = 40

export function formatWindow(hours: number): string {
  if (hours >= 24 && hours % 24 === 0) return `${hours / 24} 天`
  return `${hours} 小时`
}

function noun(layer: GraphLayerKind): string {
  return layer === 'kg' ? '人物/实体' : '标签'
}

function compareIds(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

function nodeRank(graph: RelationGraph) {
  return (a: string, b: string) => {
    const left = graph.getNodeAttributes(a)
    const right = graph.getNodeAttributes(b)
    return (right.importance - left.importance) || (right.degree - left.degree) || compareIds(a, b)
  }
}

function edgeRank(graph: RelationGraph) {
  return (a: string, b: string) => {
    const left = graph.getEdgeAttributes(a)
    const right = graph.getEdgeAttributes(b)
    const leftHub = graph.degree(graph.source(a)) + graph.degree(graph.target(a))
    const rightHub = graph.degree(graph.source(b)) + graph.degree(graph.target(b))
    return (right.weight - left.weight) || (right.confidence - left.confidence) || (rightHub - leftHub) || compareIds(a, b)
  }
}

/** 与方向无关的节点对键，用于合并往返边与匹配路径。 */
export function pairKey(source: string, target: string): string {
  return source < target ? `${source}\u0000${target}` : `${target}\u0000${source}`
}

interface Selection {
  nodes: string[]
  edges: string[]
  context?: Set<string>
  sizeBy?: Map<string, number>
  labelTop: number
  forceLabels?: Iterable<string>
}

/** 由完整图中选出的节点 / 边组装视图图：合并同一对节点的往返边、按视图计算大小、挑常显标签、确定性铺初始坐标。 */
function assemble(full: RelationGraph, selection: Selection): RelationGraph {
  const out: RelationGraph = new MultiDirectedGraph<GraphNodeAttributes, GraphEdgeAttributes>({ allowSelfLoops: false })
  const values = new Map<string, number>()
  for (const id of selection.nodes) {
    if (out.hasNode(id) || !full.hasNode(id)) continue
    const attrs = full.getNodeAttributes(id)
    out.addNode(id, { ...attrs, x: 0, y: 0, forceLabel: false, zIndex: 0, context: selection.context?.has(id) ?? false })
    values.set(id, Math.max(0, selection.sizeBy?.get(id) ?? attrs.importance))
  }

  const seenPairs = new Map<string, string>()
  const ranked = [...new Set(selection.edges)].filter((key) => full.hasEdge(key)).sort(edgeRank(full))
  let maxWeight = 0
  for (const key of ranked) maxWeight = Math.max(maxWeight, full.getEdgeAttribute(key, 'weight'))
  // 边越多越细：大图里 3px 的边会把节点完全盖住
  const [edgeMin, edgeSpan] = ranked.length > 400 ? [0.4, 1.2] : [0.6, 2.4]
  for (const key of ranked) {
    const source = full.source(key)
    const target = full.target(key)
    if (!out.hasNode(source) || !out.hasNode(target)) continue
    const pair = pairKey(source, target)
    const existing = seenPairs.get(pair)
    if (existing) {
      if (out.source(existing) !== source) out.setEdgeAttribute(existing, 'reciprocal', true)
      continue
    }
    const attrs = full.getEdgeAttributes(key)
    const ratio = maxWeight > 0 ? Math.max(0, attrs.weight) / maxWeight : 0.5
    out.addEdgeWithKey(key, source, target, { ...attrs, size: edgeMin + edgeSpan * ratio, reciprocal: false })
    seenPairs.set(pair, key)
  }

  // 大小 = 视图内的相对值（min–max 归一化后开方），节点越多上限越小，避免大图糊成一片
  const all = [...values.values()]
  const maxValue = all.length ? Math.max(...all) : 0
  const minValue = all.length ? Math.min(...all) : 0
  const minSize = out.order > 400 ? 2 : 2.5
  const maxSize = out.order > 600 ? 8 : out.order > 200 ? 10 : 13
  out.forEachNode((id, attrs) => {
    const value = values.get(id) ?? 0
    const ratio = maxValue > minValue ? (value - minValue) / (maxValue - minValue) : 0.5
    const base = minSize + (maxSize - minSize) * Math.sqrt(ratio)
    out.mergeNodeAttributes(id, { size: attrs.context ? Math.max(2, base * 0.65) : base })
  })

  const labelled = new Set<string>(selection.forceLabels ?? [])
  const byValue = [...values.keys()].filter((id) => out.hasNode(id) && !out.getNodeAttribute(id, 'context'))
  byValue.sort((a, b) => ((values.get(b) ?? 0) - (values.get(a) ?? 0)) || (out.degree(b) - out.degree(a)) || compareIds(a, b))
  for (const id of byValue.slice(0, selection.labelTop)) labelled.add(id)
  for (const id of labelled) {
    if (out.hasNode(id)) out.mergeNodeAttributes(id, { forceLabel: true, zIndex: 1 })
  }

  seedPositions(out)
  return out
}

function labelBudget(count: number): number {
  return Math.max(4, Math.min(12, Math.round(count * 0.04)))
}

/** 搜索聚焦 / 路径：把指定节点（和它最强的邻居）补进视图，不受类型与数量限制。 */
function augment(full: RelationGraph, selection: Selection, options: ViewOptions, allowed: (id: string) => boolean): Selection {
  const pinned = (options.pinned ?? []).filter((id) => full.hasNode(id))
  const pathNodes = (options.path?.nodes ?? []).filter((id) => full.hasNode(id))
  const pathEdges = (options.path?.edges ?? []).filter((key) => full.hasEdge(key))
  if (!pinned.length && !pathNodes.length) return selection
  const nodes = new Set(selection.nodes)
  const edges = new Set(selection.edges)
  const minWeight = options.minWeight ?? 0
  for (const id of pinned) {
    nodes.add(id)
    const incident = full.edges(id).filter((key) => full.getEdgeAttribute(key, 'weight') >= minWeight && allowed(full.opposite(id, key)))
    incident.sort(edgeRank(full))
    let added = 0
    for (const key of incident) {
      const other = full.opposite(id, key)
      if (!nodes.has(other)) {
        if (added >= PINNED_NEIGHBORS) continue
        added += 1
        nodes.add(other)
      }
      edges.add(key)
    }
  }
  for (const id of pathNodes) nodes.add(id)
  for (const key of pathEdges) edges.add(key)
  const context = selection.context ? new Set([...selection.context].filter((id) => !pinned.includes(id) && !pathNodes.includes(id))) : undefined
  return { ...selection, nodes: [...nodes], edges: [...edges], context, forceLabels: [...(selection.forceLabels ?? []), ...pinned, ...pathNodes] }
}

function finish(full: RelationGraph, selection: Selection, options: ViewOptions, allowed: (id: string) => boolean, summary: Omit<ViewSummary, 'nodeCount' | 'edgeCount'>): ViewResult {
  const graph = assemble(full, augment(full, selection, options, allowed))
  return { graph, summary: { ...summary, nodeCount: graph.order, edgeCount: graph.size } }
}

/**
 * 纯函数：从 adapter 输出的完整图里按视图规则挑出要画的子图。
 * 所有排序都带 id 兜底，结果与输入顺序无关、可重复。
 */
export function buildView(bundle: GraphBundle, options: ViewOptions): ViewResult {
  const full = bundle.graph
  const layer = bundle.layer
  const what = noun(layer)
  const maxNodes = Math.max(1, Math.floor(options.maxNodes))
  const minWeight = options.minWeight ?? 0
  const types = options.types && options.types.size ? options.types : null
  const allowed = (id: string) => !types || types.has(full.getNodeAttribute(id, 'type'))
  const edgeOk = (key: string) => full.getEdgeAttribute(key, 'weight') >= minWeight && allowed(full.source(key)) && allowed(full.target(key))
  const windowText = formatWindow(options.windowHours)
  const cutoff = options.now - options.windowHours * 3600
  const rank = nodeRank(full)

  if (options.view === 'all') {
    const nodes = full.nodes().filter(allowed).sort(rank).slice(0, maxNodes)
    const set = new Set(nodes)
    const edges = full.edges().filter((key) => edgeOk(key) && set.has(full.source(key)) && set.has(full.target(key)))
    const result = finish(full, { nodes, edges, labelTop: labelBudget(nodes.length) }, options, allowed, { view: 'all', sentence: '' })
    const totalText = bundle.total > result.summary.nodeCount ? `（本群共 ${bundle.total} 个）` : ''
    result.summary.sentence = `按重要度排出的前 ${result.summary.nodeCount} 个${what}${totalText}，以及它们之间的全部 ${result.summary.edgeCount} 条关系。`
    return result
  }

  if (options.view === 'strongest') {
    const topEdges = Math.max(1, Math.floor(options.topEdges ?? Math.min(120, maxNodes)))
    const candidates = full.edges().filter(edgeOk).sort(edgeRank(full))
    const nodes = new Set<string>()
    const edges: string[] = []
    const pairs = new Set<string>()
    // 每个节点最多贡献多少条边：真实数据里权重大量并列（事实 0.85 / 关系 1.0），
    // 不设上限的话「最强关系」会被一两个超级枢纽的星形占满，看不出结构。
    const perNodeCap = Math.max(3, Math.ceil(topEdges / 10))
    const used = new Map<string, number>()
    for (const key of candidates) {
      if (edges.length >= topEdges) break
      const source = full.source(key)
      const target = full.target(key)
      const pair = pairKey(source, target)
      if (pairs.has(pair)) continue
      if ((used.get(source) ?? 0) >= perNodeCap || (used.get(target) ?? 0) >= perNodeCap) continue
      const extra = (nodes.has(source) ? 0 : 1) + (nodes.has(target) ? 0 : 1)
      if (nodes.size + extra > maxNodes) continue
      nodes.add(source)
      nodes.add(target)
      used.set(source, (used.get(source) ?? 0) + 1)
      used.set(target, (used.get(target) ?? 0) + 1)
      pairs.add(pair)
      edges.push(key)
    }
    // 同一对节点的反向边也带上，assemble 会合并成一条并标记 reciprocal。
    const reverse = candidates.filter((key) => pairs.has(pairKey(full.source(key), full.target(key))))
    const result = finish(full, { nodes: [...nodes], edges: [...edges, ...reverse], labelTop: labelBudget(nodes.size) }, options, allowed, {
      view: 'strongest',
      sentence: '',
      note: layer === 'kg' ? '强度 = 事实 / 关系的权重，其次看置信度；同分时优先连接更多的枢纽。' : '强度 = 序位共现权重（已归一化到 0–1），显式关系按其权重参与排序。',
    })
    result.summary.sentence = result.summary.edgeCount
      ? `${what}之间最强的 ${result.summary.edgeCount} 条关系，涉及 ${result.summary.nodeCount} 个${what}：线越粗关系越强，点越大越重要。`
      : `当前筛选下没有满足条件的关系，可以降低最小权重或放开类型过滤。`
    return result
  }

  if (options.view === 'new') {
    if (layer === 'tags' && !bundle.timeFields.nodeCreated) {
      return finish(full, { nodes: [], edges: [], labelTop: 0 }, options, allowed, {
        view: 'new',
        sentence: '暂时无法判断哪些标签是新出现的。',
        unavailable: '当前后端没有返回标签创建时间（created_at），需要升级 WaveMemory 后端后才能使用「新出现」。',
      })
    }
    if (layer === 'kg' && !bundle.timeFields.edgeTime) {
      return finish(full, { nodes: [], edges: [], labelTop: 0 }, options, allowed, {
        view: 'new',
        sentence: '暂时无法判断哪些关系是新出现的。',
        unavailable: '当前数据没有任何时间字段（ts / created_ts）。',
      })
    }
    const born = (id: string) => full.getNodeAttribute(id, 'createdAt')
    const isNewNode = (id: string) => {
      const value = born(id)
      return value !== undefined && value >= cutoff
    }
    const nodes = new Set<string>()
    const edges: string[] = []
    const context = new Set<string>()
    let newNodeCount = 0
    let newEdgeCount = 0
    if (layer === 'kg') {
      const edgeTime = (key: string) => full.getEdgeAttribute(key, 'createdAt') ?? full.getEdgeAttribute(key, 'updatedAt') ?? 0
      const fresh = full.edges().filter((key) => edgeOk(key) && edgeTime(key) >= cutoff)
      fresh.sort((a, b) => (edgeTime(b) - edgeTime(a)) || edgeRank(full)(a, b))
      for (const key of fresh) {
        const source = full.source(key)
        const target = full.target(key)
        const extra = (nodes.has(source) ? 0 : 1) + (nodes.has(target) ? 0 : 1)
        if (nodes.size + extra > maxNodes) continue
        nodes.add(source)
        nodes.add(target)
        edges.push(key)
      }
      newEdgeCount = edges.length
    } else {
      const fresh = full.nodes().filter((id) => allowed(id) && isNewNode(id))
      fresh.sort((a, b) => ((born(b) ?? 0) - (born(a) ?? 0)) || rank(a, b))
      for (const id of fresh.slice(0, Math.max(1, Math.ceil(maxNodes * 0.6)))) nodes.add(id)
      const touching = full.edges().filter((key) => edgeOk(key) && (nodes.has(full.source(key)) || nodes.has(full.target(key))))
      touching.sort(edgeRank(full))
      for (const key of touching) {
        const source = full.source(key)
        const target = full.target(key)
        const extra = (nodes.has(source) ? 0 : 1) + (nodes.has(target) ? 0 : 1)
        if (nodes.size + extra > maxNodes) continue
        nodes.add(source)
        nodes.add(target)
        edges.push(key)
      }
    }
    // 知识图谱里「新」的是关系：端点照常显示，第一次出现的节点画得更大；
    // 标签图里「新」的是节点：与之共现的旧标签作为淡色上下文。
    const sizeBy = new Map<string, number>()
    for (const id of nodes) {
      const fresh = isNewNode(id)
      if (fresh) newNodeCount += 1
      else if (layer === 'tags') context.add(id)
      sizeBy.set(id, fresh ? 2 : 1)
    }
    const freshLabels = [...nodes].filter((id) => !context.has(id)).sort(rank).slice(0, 30)
    const note = layer === 'kg' && !bundle.timeFields.nodeCreatedExact
      ? '当前后端未返回关系的创建时间（created_ts），按最近更新时间近似「新出现」。'
      : layer === 'kg' ? '新 = 关系的创建时间落在窗口内；节点的创建时间取它最早一条关系。' : '新 = 标签的创建时间（created_at）落在窗口内。'
    const result = finish(full, { nodes: [...nodes], edges, context, sizeBy: layer === 'kg' ? sizeBy : undefined, labelTop: 0, forceLabels: freshLabels }, options, allowed, { view: 'new', sentence: '', note })
    if (!nodes.size) {
      result.summary.sentence = `最近 ${windowText}没有新出现的${layer === 'kg' ? '关系' : '标签'}，可以把时间窗口放宽。`
    } else if (layer === 'kg') {
      result.summary.sentence = newNodeCount
        ? `最近 ${windowText}新出现的 ${newEdgeCount} 条关系，其中 ${newNodeCount} 个${what}是第一次出现（画成大点）。`
        : `最近 ${windowText}新出现的 ${newEdgeCount} 条关系，连接的都是已有的${what}。`
    } else {
      result.summary.sentence = `最近 ${windowText}新出现的 ${newNodeCount} 个标签；淡色小点是与它们共现的已有标签。`
    }
    return result
  }

  // current：最近活跃
  const edgeActive = (key: string) => (full.getEdgeAttribute(key, 'updatedAt') ?? 0) >= cutoff
  const score = new Map<string, number>()
  if (layer === 'kg') {
    full.forEachNode((id) => {
      let count = 0
      full.forEachEdge(id, (key) => { if (edgeActive(key)) count += 1 })
      if (count) score.set(id, count)
    })
  } else {
    full.forEachNode((id, attrs) => {
      const value = bundle.timeFields.nodeRecentCount ? attrs.recentCount ?? 0 : (attrs.lastSeenAt ?? 0) >= cutoff ? 1 : 0
      if (value > 0) score.set(id, value)
    })
  }
  if (!bundle.timeFields.nodeLastSeen && !bundle.timeFields.edgeTime) {
    return finish(full, { nodes: [], edges: [], labelTop: 0 }, options, allowed, {
      view: 'current',
      sentence: '暂时无法判断最近在聊什么。',
      unavailable: '当前数据没有最近出现时间。',
    })
  }
  const lastSeen = (id: string) => full.getNodeAttribute(id, 'lastSeenAt') ?? 0
  const cap = Math.min(maxNodes, 150)
  let active = [...score.keys()].filter(allowed)
  active.sort((a, b) => ((score.get(b) ?? 0) - (score.get(a) ?? 0)) || (lastSeen(b) - lastSeen(a)) || rank(a, b))
  active = active.slice(0, cap)
  let note = layer === 'kg'
    ? `活跃度 = 最近 ${windowText}内新增或更新的关系条数。`
    : bundle.timeFields.nodeRecentCount
      ? `活跃度 = 最近 ${windowText}内打上该标签的记忆条数。`
      : `当前后端未返回窗口内记忆数，按最近出现时间是否落在 ${windowText}内判断。`
  let sizeBy: Map<string, number> | undefined = score
  if (active.length < CURRENT_MIN_ACTIVE) {
    const fallback = full.nodes().filter((id) => allowed(id) && lastSeen(id) > 0)
    fallback.sort((a, b) => (lastSeen(b) - lastSeen(a)) || rank(a, b))
    active = fallback.slice(0, Math.min(CURRENT_FALLBACK, maxNodes))
    note = `最近 ${windowText}内活跃的${what}不足 ${CURRENT_MIN_ACTIVE} 个，改为展示最后出现时间最近的 ${active.length} 个。`
    sizeBy = undefined
  }
  const set = new Set(active)
  const edges = full.edges().filter((key) => edgeOk(key) && set.has(full.source(key)) && set.has(full.target(key)))
  const result = finish(full, { nodes: active, edges, sizeBy, labelTop: labelBudget(active.length) }, options, allowed, { view: 'current', sentence: '', note })
  result.summary.sentence = result.summary.nodeCount
    ? sizeBy
      ? `最近 ${windowText}最活跃的 ${result.summary.nodeCount} 个${what}：点越大，这段时间被提到得越多。`
      : `最近出现过的 ${result.summary.nodeCount} 个${what}，按最后出现时间排列。`
    : `最近 ${windowText}没有活跃的${what}。`
  return result
}

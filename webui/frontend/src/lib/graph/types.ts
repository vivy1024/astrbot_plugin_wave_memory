import type { MultiDirectedGraph } from 'graphology'

export type GraphLayerKind = 'kg' | 'tags'
export type GraphViewKind = 'strongest' | 'new' | 'current' | 'all'

/** 两个数据层统一后的节点属性（sigma 直接读取 label / size / color / x / y / forceLabel / zIndex）。 */
export interface GraphNodeAttributes {
  label: string
  /** 规范化后的类型键（小写），决定颜色。 */
  type: string
  size: number
  color: string
  /** 在完整数据中的连边数。 */
  degree: number
  /** 重要度：标签 = 关联记忆条数；知识图谱 = degree。 */
  importance: number
  /** 创建时间（秒）。标签取 scoped_tags.created_at；知识图谱取关联边最早的 created_ts / ts。 */
  createdAt?: number
  /** 最近出现时间（秒）。 */
  lastSeenAt?: number
  /** 活跃窗口内的记忆 / 关系数（后端给出时才有）。 */
  recentCount?: number
  x: number
  y: number
  forceLabel?: boolean
  zIndex?: number
  /** 只为给新节点提供上下文而带进来的旧节点（「新出现」视图）。 */
  context?: boolean
}

export type GraphEdgeKind = 'fact' | 'tag_relation' | 'cooccurrence' | 'relation' | string

export interface GraphEdgeAttributes {
  weight: number
  label: string
  kind: GraphEdgeKind
  confidence: number
  /** 创建时间（秒），可缺省。 */
  createdAt?: number
  /** 最近更新 / 最近共现时间（秒），可缺省。 */
  updatedAt?: number
  size: number
  color: string
  /** sigma 边程序：语义关系画箭头，共现画线。 */
  type?: 'line' | 'arrow'
  /** 视图里同一对节点存在反向边（已合并显示）。 */
  reciprocal?: boolean
  zIndex?: number
}

export type RelationGraph = MultiDirectedGraph<GraphNodeAttributes, GraphEdgeAttributes>

/** adapter 输出：统一图 + 原始 payload 查找表 + 可用时间字段说明。 */
export interface GraphBundle<RawNode = unknown, RawEdge = unknown> {
  layer: GraphLayerKind
  graph: RelationGraph
  rawNodes: Map<string, RawNode>
  rawEdges: Map<string, RawEdge>
  /** 数据源里的节点总量（标签：本群标签总数；知识图谱：投影节点数）。 */
  total: number
  timeFields: {
    /** 节点有真实创建时间。 */
    nodeCreated: boolean
    /** 节点创建时间是否精确（false = 由边的更新时间近似）。 */
    nodeCreatedExact: boolean
    /** 节点有最近出现时间。 */
    nodeLastSeen: boolean
    /** 节点有后端统计的窗口活跃数。 */
    nodeRecentCount: boolean
    /** 边有时间戳。 */
    edgeTime: boolean
  }
  /** 后端回显的活跃窗口（小时），用于解释 recentCount。 */
  recentWindowHours?: number
  /** 图例顺序（标签图 Tag_Settings.graph_legend_types）。 */
  legendOrder: string[]
  generatedAt?: number
}

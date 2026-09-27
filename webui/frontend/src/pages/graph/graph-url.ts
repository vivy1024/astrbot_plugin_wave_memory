import type { GraphLayerKind, GraphViewKind } from '@/lib/graph/types'
import { WINDOW_OPTIONS_HOURS } from '@/lib/graph/views'

export const MAX_NODE_OPTIONS = [100, 300, 600, 1000] as const
export const MIN_WEIGHT_OPTIONS = [0, 0.2, 0.4, 0.6, 0.8] as const
export const DEFAULT_WINDOW_HOURS = 168

export interface GraphUrlState {
  layer: GraphLayerKind
  view: GraphViewKind
  windowHours: number
  maxNodes: number
  minWeight: number
  /** 被隐藏的类型（空 = 全部显示）。 */
  hiddenTypes: string[]
  glow: boolean
  node: string | null
  pathFrom: string | null
  pathTo: string | null
  /** 旧 /tags/graph 深链的 ObjectRef（ref / source_ref / target_ref），按标签 ref 反查节点。 */
  legacyRef: string | null
  legacySourceRef: string | null
  legacyTargetRef: string | null
}

const VIEWS: readonly GraphViewKind[] = ['strongest', 'new', 'current', 'all']

function pick<T extends number>(value: string | null, options: readonly T[], fallback: T): T {
  const num = Number(value)
  return (options as readonly number[]).includes(num) ? (num as T) : fallback
}

export function parseGraphUrl(params: URLSearchParams, isMobile: boolean): GraphUrlState {
  const layerParam = params.get('layer')
  const viewParam = params.get('view') as GraphViewKind | null
  return {
    layer: layerParam === 'tags' ? 'tags' : 'kg',
    view: viewParam && VIEWS.includes(viewParam) ? viewParam : 'strongest',
    windowHours: pick(params.get('window'), WINDOW_OPTIONS_HOURS, DEFAULT_WINDOW_HOURS),
    maxNodes: pick(params.get('max_nodes'), MAX_NODE_OPTIONS, isMobile ? 100 : 300),
    minWeight: pick(params.get('min_weight'), MIN_WEIGHT_OPTIONS, 0),
    hiddenTypes: (params.get('hide_types') ?? '').split(',').map((item) => item.trim().toLowerCase()).filter(Boolean),
    glow: params.get('glow') === '1',
    node: params.get('node') || null,
    pathFrom: params.get('path_from') || null,
    pathTo: params.get('path_to') || null,
    legacyRef: params.get('ref') || null,
    legacySourceRef: params.get('source_ref') || null,
    legacyTargetRef: params.get('target_ref') || null,
  }
}

/** 切换数据层时，节点 / 路径 / 类型过滤都属于旧数据层，一并清空。 */
export const LAYER_DEPENDENT_KEYS = ['node', 'path_from', 'path_to', 'hide_types', 'ref', 'source_ref', 'target_ref'] as const

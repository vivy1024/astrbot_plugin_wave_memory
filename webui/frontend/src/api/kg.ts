import { fetchJson } from './client'
import type { ObjectRefDescriptor } from '@/components/shared/types'

/** 知识图谱（/api/kg/*）只接受完整的群作用域。 */
export interface KgScope {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export interface KgGraphNode {
  /** 形如 entity:名字 / memory:123 / bot:yushu。 */
  id: string
  name: string
  type: string
  layer: string
  degree: number
  read_only?: boolean
  [key: string]: unknown
}

export interface KgProvenance {
  evidence?: string
  source_quote?: string
  source?: string
  proposed_at?: number
  [key: string]: unknown
}

export interface KgGraphEdge {
  id: string
  /** source / target 节点 id。 */
  s: string
  t: string
  /** 关系文字（谓词 / 关系类型）。 */
  l: string
  st?: string
  tt?: string
  w?: number
  weight: number
  confidence?: number
  /** updated_at（没有则为 created_at），秒。 */
  ts?: number
  /** created_at，秒；新版后端才有。 */
  created_ts?: number
  layer: string
  kind: string
  status?: string | null
  fact_id?: number
  relation_id?: number
  source_memory_id?: number | null
  provenance?: KgProvenance
  object_ref?: ObjectRefDescriptor
  [key: string]: unknown
}

export interface KgFullPayload {
  nodes: KgGraphNode[]
  edges: KgGraphEdge[]
  total?: number
  node_total?: number
  layers: string[]
  layer_counts?: Record<string, { nodes: number; edges: number }>
  warnings?: Array<{ layer: string; reason: string }>
  generated_at?: number
  read_only: boolean
  reason_code?: string
}

export interface KgEntityFact {
  id: number
  subject: string
  predicate: string
  object: string
  confidence: number
  status: string
  source_memory_id?: number | null
  revision: number
  object_ref?: ObjectRefDescriptor
}

export interface KgEntityRelation {
  id: number
  source: string
  target: string
  type?: string
  relation_type?: string
  weight?: number
  confidence?: number
  [key: string]: unknown
}

export interface KgEntityMemory {
  id: number
  content: string
  sender: string
  ts: number
  revision?: number
}

export interface KgEntityPayload {
  name: string
  person: { name: string; msg_count: number } | null
  facts: KgEntityFact[]
  relations: KgEntityRelation[]
  memories: KgEntityMemory[]
  read_only: boolean
}

export type KgTimelineEvent =
  | { type: 'fact'; ts: number; subject: string; predicate: string; object: string; source_id?: number | null }
  | ({ type: 'memory' } & KgEntityMemory)

export interface KgTimelinePayload {
  name: string
  events: KgTimelineEvent[]
  read_only: boolean
}

export interface KgPathPayload {
  /** 节点 id 序列（与 /api/kg/full 的节点 id 同一命名空间）。 */
  path: string[]
  edges: Array<{ source: string; target: string; label: string }>
  nodes: Array<{ id: number; name: string; type: string; degree: number }>
  read_only: boolean
}

function scopeQuery(scope: KgScope): URLSearchParams {
  return new URLSearchParams({ bot_id: scope.bot_id, session_id: scope.session_id, visibility: scope.visibility })
}

export function getKgFull(scope: KgScope, options: { layers?: string[]; minConfidence?: number; signal?: AbortSignal } = {}): Promise<KgFullPayload> {
  const params = scopeQuery(scope)
  params.set('layers', (options.layers?.length ? options.layers : ['facts']).join(','))
  if (options.minConfidence !== undefined) params.set('min_confidence', String(options.minConfidence))
  return fetchJson<KgFullPayload>(`/api/kg/full?${params.toString()}`, { signal: options.signal, timeoutMs: 30_000 })
}

export function getKgEntity(scope: KgScope, name: string, options: { limit?: number; signal?: AbortSignal } = {}): Promise<KgEntityPayload> {
  const params = scopeQuery(scope)
  params.set('limit', String(options.limit ?? 15))
  return fetchJson<KgEntityPayload>(`/api/kg/entity/${encodeURIComponent(name)}?${params.toString()}`, { signal: options.signal })
}

export function getKgEntityTimeline(scope: KgScope, name: string, options: { limit?: number; signal?: AbortSignal } = {}): Promise<KgTimelinePayload> {
  const params = scopeQuery(scope)
  params.set('limit', String(options.limit ?? 25))
  return fetchJson<KgTimelinePayload>(`/api/kg/entity/${encodeURIComponent(name)}/timeline?${params.toString()}`, { signal: options.signal })
}

/** 只读 BFS：服务端在当前群的事实投影上找无向最短路径（POST 仅用于传参，不写库）。 */
export function findKgPath(scope: KgScope, body: { from: string; to: string; max_depth?: number }, signal?: AbortSignal): Promise<KgPathPayload> {
  return fetchJson<KgPathPayload>(`/api/kg/path?${scopeQuery(scope).toString()}`, {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
  })
}

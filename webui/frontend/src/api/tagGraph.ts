import { fetchJson } from './client'
import type { ObjectRefDescriptor } from '@/components/shared/types'

export type TagGraphLayer = 'cooccurrence' | 'relations'

export interface TagGraphScope {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export interface TagGraphMemory {
  id: number
  content: string
  sender: string
  timestamp: number
  importance: number
  source?: string | null
  version: number
  tag_source: string
  relevance: number
  ref?: string
  object_ref?: ObjectRefDescriptor
}

export interface TagGraphNode {
  id: string
  locator: number
  name: string
  type: string
  description: string
  confidence: number
  metadata: Record<string, unknown>
  status: string
  revision: number
  memory_count: number
  frequency: number
  source_counts: Record<string, number>
  sources: string[]
  associated_memories: TagGraphMemory[]
  in_degree: number
  out_degree: number
  in_weight: number
  out_weight: number
  ref: string
  object_ref?: ObjectRefDescriptor
  read_only: true
}

export interface TagGraphEdge {
  id: string
  source: string
  target: string
  layer: TagGraphLayer
  kind: 'directed_cooccurrence' | 'tag_relation'
  type: string
  label: string
  weight: number
  frequency: number
  confidence: number
  latest_ts: number
  source_kind: string
  source_counts?: Record<string, number>
  metadata?: Record<string, unknown>
  pulse_energy?: number
  pulse_decay?: number
  read_only: true
}

export interface TagGraphPayload {
  nodes: TagGraphNode[]
  edges: TagGraphEdge[]
  layers: TagGraphLayer[]
  available_layers: TagGraphLayer[]
  layer_counts: Partial<Record<TagGraphLayer, { nodes: number; edges: number }>>
  scope: TagGraphScope
  read_only: true
  generated_at: number
  warnings: Array<{ layer: string; reason: string }>
  pulse: { enabled: boolean; half_life_hours: number }
}

export interface TagGraphPathPayload {
  found: boolean
  path: string[]
  nodes: TagGraphNode[]
  edges: TagGraphEdge[]
  layers: TagGraphLayer[]
  scope: TagGraphScope
  read_only: true
}

export interface GetTagGraphOptions {
  layers?: TagGraphLayer[]
  minConfidence?: number
  maxNodes?: number
  includePulse?: boolean
  pulseHalfLifeHours?: number
  signal?: AbortSignal
}

function scopeQuery(scope: TagGraphScope): URLSearchParams {
  return new URLSearchParams({ bot_id: scope.bot_id, session_id: scope.session_id, visibility: scope.visibility })
}

export function getTagGraph(scope: TagGraphScope, options: GetTagGraphOptions = {}): Promise<TagGraphPayload> {
  const params = scopeQuery(scope)
  if (options.layers) params.set('layers', options.layers.join(','))
  if (options.minConfidence !== undefined) params.set('min_confidence', String(options.minConfidence))
  if (options.maxNodes !== undefined) params.set('max_nodes', String(options.maxNodes))
  if (options.includePulse) params.set('include_pulse', '1')
  if (options.pulseHalfLifeHours !== undefined) params.set('pulse_half_life_hours', String(options.pulseHalfLifeHours))
  return fetchJson<TagGraphPayload>(`/api/tag-graph?${params.toString()}`, { signal: options.signal })
}

export function getTagGraphDetail(scope: TagGraphScope, ref: string, layers?: TagGraphLayer[], signal?: AbortSignal): Promise<{ item: TagGraphNode; scope: TagGraphScope; read_only: true }> {
  const params = scopeQuery(scope)
  params.set('ref', ref)
  if (layers) params.set('layers', layers.join(','))
  return fetchJson(`/api/tag-graph/tag?${params.toString()}`, { signal })
}

export function findTagGraphPath(
  scope: TagGraphScope,
  payload: { source_ref: string; target_ref: string; layers: TagGraphLayer[]; max_depth?: number },
  signal?: AbortSignal,
): Promise<TagGraphPathPayload> {
  const params = scopeQuery(scope)
  return fetchJson<TagGraphPathPayload>(`/api/tag-graph/path?${params.toString()}`, {
    method: 'POST',
    body: JSON.stringify(payload),
    signal,
  })
}

export function updateTagCommand(
  scope: TagGraphScope,
  payload: {
    object_ref: ObjectRefDescriptor
    revision: number
    patch: { name?: string; tag_type?: string; description?: string; aliases?: string[] }
    idempotency_key?: string
  },
  signal?: AbortSignal,
): Promise<{ ok: boolean; revision: number; status: string; object_ref?: ObjectRefDescriptor }> {
  const params = scopeQuery(scope)
  return fetchJson(`/api/kg/commands/tags/update?${params.toString()}`, {
    method: 'POST',
    body: JSON.stringify(payload),
    signal,
  })
}

import { fetchJson, getStoredToken, toApiPath } from './client'
import type { ObjectRefDescriptor, PageResponse, PageSize } from '@/components/shared/types'

export interface MemoryTag { id?: number | null; name: string; type?: string; tag_type?: string; source?: 'automatic' | 'manual'; position?: number; relevance?: number; [key: string]: unknown }
export interface MemoryTagCorrection {
  correction_id: string
  operation: 'add' | 'remove' | 'replace'
  requested_tags: string[]
  before: string[]
  tags: string[]
  revision: number
  status: 'active' | 'undone'
  created_at: number
  reason: string
  ref: string
  object_ref?: ObjectRefDescriptor
}
export interface MemoryTagState { automatic: MemoryTag[]; effective: MemoryTag[]; manual: MemoryTagCorrection | null }
export interface MemoryItem {
  id: number
  content: string
  sender_id: string
  sender_name?: string
  group_id?: string
  bot_id: string
  session_id: string
  visibility: string
  source?: string
  timestamp: number
  importance?: number
  access_count?: number
  has_vector: boolean
  tags?: MemoryTag[]
  version: number
  ref: string
  detail_url: string
  mutation_url: string
  object_ref?: ObjectRefDescriptor
}
export type MemoryDetail = MemoryItem
export interface SenderItem { name: string; count: number }
export interface MemoryScope { bot_id: string; session_id: string; visibility: 'group' }
export interface MemoriesFilters extends MemoryScope {
  limit: PageSize
  offset: number
  source?: string
  /** Exact sender identifier; the backend's legacy `sender` filter means sender_name. */
  sender_id?: string
  sender?: string
  has_tags?: string
  has_vector?: string
  search?: string
}
export type MemoriesResponse = PageResponse<MemoryItem>
export interface MemoryMutationResult { ok: boolean; operation: { kind: string; status: string; id?: string }; revision: number | string | null; item?: MemoryItem }
export interface MemoryTagMutationResult extends Omit<MemoryMutationResult, 'item'> { item?: { memory: MemoryItem; tags: MemoryTagState } }
export interface MemoryRefInput { id: number; ref: string }
export interface SimilarMemoryItem { id: number; content: string; source: string; similarity: number }

function query(filters: object): string {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== '') params.set(key, String(value)) })
  return params.toString()
}

function scopedActionUrl(item: Pick<MemoryItem, 'mutation_url'>, suffix: string): string {
  const [path, search = ''] = item.mutation_url.split('?', 2)
  return `${path}${suffix}${search ? `?${search}` : ''}`
}

function batchUrl(action: string, scope: MemoryScope): string {
  return `/api/memories/batch/${action}?${query(scope)}`
}

export function listMemories(filters: MemoriesFilters): Promise<MemoriesResponse> { return fetchJson(`/api/memories?${query(filters)}`) }
export function listSenders(scope: MemoryScope): Promise<{ senders: SenderItem[]; source: { status: string; reason_code: string | null } }> { return fetchJson(`/api/memories/senders?${query(scope)}`) }
export function getMemoryDetail(detailUrl: string): Promise<{ item: MemoryDetail }> { return fetchJson(detailUrl) }
export function updateMemory(mutationUrl: string, content: string, importance: number): Promise<MemoryMutationResult> { return fetchJson(mutationUrl, { method: 'PUT', body: JSON.stringify({ content, importance }) }) }
export function deleteMemory(mutationUrl: string): Promise<MemoryMutationResult> { return fetchJson(mutationUrl, { method: 'DELETE' }) }
export function reEmbedMemory(item: Pick<MemoryItem, 'mutation_url'>): Promise<Partial<MemoryMutationResult> & { accepted?: boolean; status?: string; error?: string }> { return fetchJson(scopedActionUrl(item, '/re-embed'), { method: 'POST' }) }
export function getSimilarMemories(item: Pick<MemoryItem, 'mutation_url'>): Promise<{ items: SimilarMemoryItem[]; reason?: string }> { return fetchJson(scopedActionUrl(item, '/similar')) }
export function getMemoryTagState(item: Pick<MemoryItem, 'mutation_url'>): Promise<{ item: MemoryTagState }> { return fetchJson(scopedActionUrl(item, '/tags')) }
export function correctMemoryTags(item: Pick<MemoryItem, 'mutation_url'>, operation: 'add' | 'remove' | 'replace', tags: string[], reason: string): Promise<MemoryTagMutationResult> { return fetchJson(scopedActionUrl(item, '/tags/correction'), { method: 'POST', body: JSON.stringify({ operation, tags, reason }) }) }
export function undoMemoryTagCorrection(item: Pick<MemoryItem, 'mutation_url'>, correctionRef: string, reason: string): Promise<MemoryTagMutationResult> { return fetchJson(scopedActionUrl(item, '/tags/correction/undo'), { method: 'POST', body: JSON.stringify({ correction_ref: correctionRef, reason }) }) }
export function batchDeleteMemories(scope: MemoryScope, refs: MemoryRefInput[]): Promise<{ ok: boolean; deleted: number }> { return fetchJson(batchUrl('delete', scope), { method: 'POST', body: JSON.stringify({ refs }) }) }
export function memoryBatchStreamUrl(action: 're-embed' | 'extract-tags', scope: MemoryScope): string { return batchUrl(action, scope) }

export interface StreamProgress { progress: number; processed?: number; total: number; selected?: number; tagged?: number; imported?: number; skipped?: number; errors?: number; remaining?: number; partial?: boolean; done?: boolean; error?: string; message?: string }
export interface StreamOptions { signal?: AbortSignal; payload?: Record<string, unknown> }
export async function runPostStream(path: string, subjects: number[] | MemoryRefInput[], onProgress: (state: StreamProgress) => void, options: StreamOptions = {}): Promise<StreamProgress | null> {
  const token = getStoredToken()
  const usesRefs = subjects.length > 0 && typeof subjects[0] === 'object'
  const selection = usesRefs ? { refs: subjects } : { ids: subjects }
  const response = await fetch(toApiPath(path), { method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify({ ...selection, ...options.payload }), signal: options.signal })
  if (!response.ok) throw new Error(`HTTP stream request failed: ${response.status}`)
  const reader = response.body?.getReader()
  if (!reader) throw new Error('ReadableStream is not supported')
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let last: StreamProgress | null = null
  const consume = (line: string) => {
    if (!line.trim().startsWith('data:')) return
    const payload = JSON.parse(line.trim().slice(5)) as StreamProgress
    last = payload
    onProgress(payload)
    if (payload.error) throw new Error(payload.error)
  }
  while (true) {
    const chunk = await reader.read()
    if (chunk.done) break
    buffer += decoder.decode(chunk.value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    lines.forEach(consume)
  }
  if (buffer.trim()) consume(buffer)
  return last
}

// ---- 高级检索调试（只读）----

export type QueryStageName = 'epa' | 'pyramid' | 'spike' | 'geodesic'

export interface QueryDebugStageState {
  enabled: boolean
  available?: boolean
  reason_code?: string
  [key: string]: unknown
}

export interface QueryDebugPayload {
  epa?: QueryDebugStageState
  pyramid?: QueryDebugStageState
  spike?: QueryDebugStageState
  geodesic?: QueryDebugStageState
  scoring?: Record<string, unknown>
  vector_search?: Record<string, unknown>
  highlights?: Record<string, unknown>
  final?: { result_count?: number; ids?: unknown[]; reason_code?: string; cold?: unknown }
  warnings?: Array<{ stage: string; reason_code: string; reason?: string }>
  trace_meta?: { readonly?: boolean; touch?: boolean; truncated?: boolean }
  [key: string]: unknown
}

export interface QueryDebugResultItem {
  id?: number
  content?: string
  sender?: string
  similarity?: number
  source?: string
  timestamp?: number
  [key: string]: unknown
}

export interface QueryDebugResponse {
  results: QueryDebugResultItem[]
  timing: { embedding_ms?: number; total_ms?: number; [key: string]: unknown }
  debug: QueryDebugPayload
  readonly: true
  touch: false
}

export type QueryDebugParams = Partial<Record<
  'pyramid_max_levels' | 'pyramid_top_k' | 'spike_max_hops' | 'spike_firing_threshold' | 'geodesic_alpha',
  number
>>

export interface QueryDebugRequest {
  text: string
  topK?: number
  /** 作用域必须显式带上：服务端只从 query string / 请求头解析 Scope，不读 body。 */
  scope: MemoryScope
  /** 缺省表示沿用服务端配置；显式 false 才会关掉对应阶段。 */
  stages?: Partial<Record<QueryStageName, boolean>>
  /** 仅这 5 个键会被服务端接受，其余在 QueryOptions 白名单外会被丢弃。 */
  params?: QueryDebugParams
  signal?: AbortSignal
}

export function runQueryDebug(request: QueryDebugRequest): Promise<QueryDebugResponse> {
  const { text, topK = 5, scope, stages, params, signal } = request
  return fetchJson<QueryDebugResponse>(`/api/query?${query(scope)}`, {
    method: 'POST',
    body: JSON.stringify({ text, top_k: topK, stages, params, debug: true }),
    signal,
  })
}

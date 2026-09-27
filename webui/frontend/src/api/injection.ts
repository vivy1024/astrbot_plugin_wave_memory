import { fetchJson } from './client'
import type { PageResponse, PageSize } from '@/components/shared/types'

export interface TraceFilters {
  from_ts?: number | string
  to_ts?: number | string
  sender_id?: string
  bot_id?: string
  bot_profile_id?: string
  channel?: string
  status?: string
  has_error?: boolean | string
  scope?: string
  chat_type?: string
  config_revision?: string
  /** 调用来源：astrbot（消息钩子）或 cortico 等 Runtime 调用方 */
  source?: string
  limit?: PageSize
  offset?: number
}

export interface TraceSessionDto {
  id: string
  kind: string
  label: string
  platform_id?: string
  conversation_id?: string
}

export interface InjectionTraceSummary {
  trace_id: string
  timestamp: number
  status: string
  session?: TraceSessionDto | null
  session_id?: string
  sender_id?: string
  bot_id?: string
  bot_profile_id?: string
  mode?: string
  config_revision?: string | number | null
  source?: string
  preview?: string
  final_text_preview?: string
  total_tokens?: number
  latency_ms?: number
  has_error?: boolean
  channels?: Array<Record<string, unknown>>
  detail_url?: string
  [key: string]: unknown
}

export type TraceListPayload = PageResponse<InjectionTraceSummary>

export interface TraceDetailPayload {
  trace_id?: string
  request?: Record<string, unknown>
  budget?: Record<string, unknown>
  channels?: Array<Record<string, unknown>>
  hits?: Array<Record<string, unknown>>
  filtered?: Array<Record<string, unknown>>
  final_text?: string
  final_injection_text?: string
  warnings?: unknown
  errors?: unknown
  error?: string
  [key: string]: unknown
}

function toSearchParams(filters: TraceFilters): string {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  return params.toString()
}

export function listInjectionTraces(filters: TraceFilters = {}, signal?: AbortSignal): Promise<TraceListPayload> {
  const query = toSearchParams(filters)
  return fetchJson<TraceListPayload>(`/api/observatory/traces${query ? `?${query}` : ''}`, { signal })
}

export function getInjectionTrace(traceId: string, signal?: AbortSignal): Promise<TraceDetailPayload> {
  return fetchJson<TraceDetailPayload>(`/api/observatory/traces/${encodeURIComponent(traceId)}`, { signal })
}

export interface MemoryTraceChannelHit {
  channel: string
  score?: number | null
  similarity?: number | null
  rank?: number
  item_count?: number
  source?: string | null
}

export interface MemoryTraceUsage {
  trace_id: string
  timestamp: number
  group_id?: string | null
  session_id?: string | null
  sender_id?: string | null
  sender_name?: string | null
  bot_id?: string | null
  bot_profile_id?: string | null
  message_preview?: string | null
  final_text_preview?: string | null
  status?: string
  source?: string
  channel: string
  score?: number | null
  channels: MemoryTraceChannelHit[]
  detail_url?: string
}

export interface MemoryTracesPayload {
  memory_id: number
  bot_id: string
  items: MemoryTraceUsage[]
  count: number
  limit: number
  channels: string[]
  elapsed_ms?: number
}

/** 最近把这条记忆作为命中条目注入的回复（只看该 Bot 的 trace）。 */
export function listMemoryTraces(memoryId: number, botId: string, limit = 10, signal?: AbortSignal): Promise<MemoryTracesPayload> {
  const params = new URLSearchParams({ bot_id: botId, limit: String(limit) })
  return fetchJson<MemoryTracesPayload>(`/api/observatory/memories/${encodeURIComponent(String(memoryId))}/traces?${params.toString()}`, { signal })
}

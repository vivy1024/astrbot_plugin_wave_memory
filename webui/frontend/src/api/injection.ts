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

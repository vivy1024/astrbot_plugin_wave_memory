import { fetchJson } from './client'

export interface ServiceHealth {
  name: string
  status: string
  reason?: string
  dependency?: string
  role?: 'core' | 'derived' | 'optional' | string
  severity?: 'ok' | 'degraded' | 'disabled' | 'critical' | string
}

export interface SystemHealthSummary {
  overall?: 'healthy' | 'degraded' | 'critical' | string
  label?: string
  total?: number
  ok_count?: number
  critical_count?: number
  degraded_count?: number
  optional_off_count?: number
}

export interface RegistryBotItem {
  qq_id: string
  name: string
  db_id: string
  aliases: string[]
}

export interface SystemPayload {
  /** 插件真实版本，来自 metadata.yaml；解析失败时为空串。 */
  plugin_version?: string
  memories?: { total?: number; with_vector?: number; with_tags?: number }
  tags?: { total?: number; structured?: number; type_distribution?: Record<string, number> }
  coverage?: { vector_pct?: number; tag_pct?: number }
  cooccurrence?: { nodes?: number; edges?: number }
  db_size_mb?: number
  epa?: { initialized?: boolean; reason?: string }
  services_health?: ServiceHealth[]
  services_summary?: SystemHealthSummary
  lifecycle?: {
    facts?: number
    persons?: number
    user_profiles?: number
    active_users?: number
    top_users?: Array<{ name?: string; interactions?: number }>
    active_moods?: Array<{ group_id?: string; type?: string; intensity?: number; desc?: string }>
    unspoken_desire?: { topic?: string; motive?: string }
  }
  todos?: {
    untagged_count: number
    pending_fewshot: number
    has_errors: boolean
  }
  registry_bots?: RegistryBotItem[]
}

export interface ErrorPayload {
  errors?: Array<Record<string, unknown>>
  total?: number
}

export interface InjectionMetricSeriesPoint {
  bucket?: number
  ts?: number
  time?: string
  [key: string]: unknown
}

export interface InjectionMetricRankingItem {
  key?: string
  name?: string
  total_tokens?: number
  avg_tokens?: number
  count?: number
  share_pct?: number
  [key: string]: unknown
}

export interface InjectionMetricWindow {
  sample_count?: number
  duration_seconds?: number
  duration_days?: number
  total_tokens_sum?: number
  avg_tokens_per_sample?: number
  avg_tokens_per_day?: number
  p95_tokens_per_sample?: number
  max_tokens_per_sample?: number
}

export interface InjectionMetricsPayload {
  range?: string
  from?: number
  to?: number
  bucket_seconds?: number
  count?: number
  summary?: Record<string, unknown>
  window?: InjectionMetricWindow
  series?: InjectionMetricSeriesPoint[]
  ranking?: InjectionMetricRankingItem[]
  error?: string
}

export function getSystemStatus(signal?: AbortSignal): Promise<SystemPayload> {
  return fetchJson<SystemPayload>('/api/system', { signal })
}

export function getRecentErrors(signal?: AbortSignal): Promise<ErrorPayload> {
  return fetchJson<ErrorPayload>('/api/errors', { signal })
}

export function getInjectionMetrics(range = '7d', signal?: AbortSignal): Promise<InjectionMetricsPayload> {
  return fetchJson<InjectionMetricsPayload>(`/api/metrics/injection?range=${encodeURIComponent(range)}`, { signal })
}

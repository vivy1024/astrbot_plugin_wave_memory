import { fetchJson } from './client'

/** 回退链来源：Bot 覆盖 / 任务配置 / 所属默认 / 旧配置 / 未配置 */
export type RouteSource = 'bot' | 'task' | 'default' | 'legacy' | 'none'

export interface RouteLayer {
  source: RouteSource
  providers: string[]
  key: string
  label: string
}

/** 链上每个 provider 在 AstrBot 里是否存在；known=null 表示没能取到 provider 列表 */
export interface ChainEntry {
  id: string
  known: boolean | null
}

export interface LlmCallStats {
  calls: number
  succeeded: number
  failed: number
  provider_errors: number
  last_provider: string
  last_success_at: number | null
  last_error: string
  last_error_provider: string
  last_error_at: number | null
  missing_providers: string[]
}

export interface BotRoute {
  bot_id: string
  name: string
  chain: string[]
  primary: string
  source: RouteSource
  source_label: string
  source_key: string
  chain_status: ChainEntry[]
}

export interface LlmTaskRoute {
  id: string
  title: string
  family: 'tag' | 'meta'
  family_title: string
  description: string
  consumers: string[]
  bot_overridable: boolean
  reserved: boolean
  /** 系统配置里的键，如 Model_Routing_Settings.diary */
  config_key: string
  configured: string[]
  invalid: string | null
  chain: string[]
  primary: string
  source: RouteSource
  source_label: string
  source_key: string
  layers: RouteLayer[]
  chain_status: ChainEntry[]
  missing: string[]
  default_chain: string[]
  default_key: string
  default_label: string
  available: boolean
  /** 调用方是否在运行；null = 未登记 */
  active: boolean | null
  active_note: string
  /** hot = 保存即生效；restart = 调用方启动时没创建，改了要重启 */
  apply_mode: 'hot' | 'restart'
  stats: LlmCallStats
  bots: BotRoute[]
}

export interface LlmFamily {
  title: string
  keys: string[]
  description: string
  chain: string[]
  chain_status: ChainEntry[]
  apply_mode: 'restart'
}

export interface LlmRoutingProvider {
  id: string
  model: string
  type: string
}

export interface LlmRoutingPayload {
  available: boolean
  error?: string
  section?: string
  title?: string
  reloaded_at?: number | null
  meta_thinking_active?: boolean
  tasks: LlmTaskRoute[]
  families: Record<string, LlmFamily>
  piggyback: Array<{ id: string; title: string; rides_on: string }>
  invalid?: Record<string, string>
  providers_checked?: boolean
  embedding: {
    key: string
    provider_id: string
    saved: string
    dimension: number | null
    apply_mode: 'restart'
    note: string
  }
  providers: LlmRoutingProvider[]
  providers_error: string | null
}

export function getLlmRouting(signal?: AbortSignal) {
  return fetchJson<LlmRoutingPayload>('/api/config/llm-routing', { signal })
}

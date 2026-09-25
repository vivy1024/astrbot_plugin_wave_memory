import { fetchJson } from './client'

export interface ProviderPayload {
  id: string
  name: string
  model: string
  [key: string]: unknown
}

/** service：保存后只重建对应后台服务即生效（做梦、记忆淘汰等），不用重启 AstrBot */
export type ConfigApplyMode = 'hot' | 'restart' | 'next_run' | 'service' | 'unknown'

export interface ConfigValueState {
  min?: number
  max?: number
  minimum?: number
  maximum?: number
  default: unknown
  saved: unknown
  saved_present?: boolean
  effective: unknown
  value: unknown
  source: string
  effective_source: string
  apply_mode: ConfigApplyMode
  effective_since?: string | number | null
  restart_required: boolean
  restart_requirement: 'required' | 'not_required'
  error?: string | null
}

export interface ConfigItem extends ConfigValueState {
  key: string
  type: string
  description: string
  hint: string
  special: string
}

export interface ConfigGroup extends Partial<ConfigValueState> {
  key: string
  kind: 'object' | 'scalar'
  type?: string
  description: string
  hint: string
  special?: string
  items?: ConfigItem[]
}

export interface ConfigSchemaPayload {
  groups: ConfigGroup[]
  warnings?: Array<{ key: string; message: string }>
}

export interface HotParam {
  key: string
  type: 'float' | 'int' | 'string'
  min: number
  max: number
  default: number
  current: number
  description: string
  saved?: number | null
  effective?: number | null
  source?: string
  effective_source?: string
  apply_mode?: ConfigApplyMode
  effective_since?: string | number | null
  restart_required?: boolean
  restart_requirement?: 'required' | 'not_required'
  error?: string | null
}

export interface HotConfigPayload {
  params: HotParam[]
  config: Record<string, unknown>
}

export interface WaveConfigPayload {
  runtime?: Record<string, unknown>
  embedding_provider_id?: string
  embedding_dimension?: number
  tag_llm_provider_id?: string
  query?: Record<string, unknown>
  tags?: Record<string, unknown>
  storage?: Record<string, unknown>
  filter?: Record<string, unknown>
  lifecycle?: Record<string, unknown>
  performance?: Record<string, unknown>
  webui?: Record<string, unknown>
  [key: string]: unknown
}

export interface FullConfigSavePayload {
  [key: string]: unknown
}

export interface FullConfigResponse {
  ok: boolean
  saved?: boolean
  changed: string[]
  changed_fields?: string[]
  message: string
  errors?: string[]
  error?: string
  restart_required?: boolean
  restart_fields?: string[]
  apply_modes?: {
    hot: string[]
    restart: string[]
    next_run: string[]
    service?: Record<string, { action: 'rebuilt' | 'created' | 'disabled' | 'failed' | string; fields: string[]; error?: string }>
  }
  effective_since?: Record<string, string | number | null>
  schema?: ConfigSchemaPayload
}

export interface HotConfigSaveResponse {
  ok: boolean
  updated: string[]
  saved?: string[]
  runtime_only?: string[]
  errors: string[]
  warnings?: string[]
  message?: string
  params?: HotParam[]
}

export function getConfigSchema(): Promise<ConfigSchemaPayload> {
  return fetchJson<ConfigSchemaPayload>('/api/config/schema')
}

export function getFullConfig(): Promise<WaveConfigPayload> {
  return fetchJson<WaveConfigPayload>('/api/config')
}

export function getHotConfig(): Promise<HotConfigPayload> {
  return fetchJson<HotConfigPayload>('/api/config/hot')
}

export function saveHotConfig(payload: Record<string, unknown>): Promise<HotConfigSaveResponse> {
  return fetchJson<HotConfigSaveResponse>('/api/config/hot', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function saveFullConfig(payload: FullConfigSavePayload): Promise<FullConfigResponse> {
  return fetchJson<FullConfigResponse>('/api/config/full', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listProviders(): Promise<{ providers: ProviderPayload[] }> {
  return fetchJson<{ providers: ProviderPayload[] }>('/api/providers')
}

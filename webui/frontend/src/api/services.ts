import { fetchJson } from './client'

export interface ServiceTaskDto {
  name: string
  owner: string
  state: string
  healthy: boolean
  started_at: number | null
  ended_at: number | null
  last_error: string | null
}

export interface ServiceDto {
  name: string
  title: string
  description: string
  created: boolean
  running: boolean | null
  stoppable: boolean
  generation: number
  tasks: ServiceTaskDto[]
  history: { action: string; ok: boolean; detail: string; at: number }[]
}

export interface ToolDto {
  name: string
  description: string
  group: string
  writes: boolean
  enabled: boolean
  runtime_exposed: boolean
  stats: { calls: number; errors: number; last_latency_ms: number }
}

export interface ServicesPayload {
  services: ServiceDto[]
  services_available: boolean
  tools: ToolDto[]
  tool_registry: { registered: number; built: number; build_errors: Record<string, string>; extension_errors: Record<string, string> } | null
  channels: string[]
}

export function getServices(signal?: AbortSignal) {
  return fetchJson<ServicesPayload>('/api/services', { signal })
}

export function controlService(name: string, action: 'start' | 'stop' | 'restart') {
  return fetchJson<{ ok: boolean; item: ServiceDto }>(`/api/services/${encodeURIComponent(name)}/${action}`, { method: 'POST' })
}

export function setToolEnabled(name: string, enabled: boolean) {
  return fetchJson<{ ok: boolean; name: string; enabled: boolean }>(`/api/tools/${encodeURIComponent(name)}/enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
}

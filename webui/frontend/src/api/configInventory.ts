import { fetchJson } from './client'

export type ConfigLayer = 'builtin' | 'static' | 'bot' | 'override'

export interface ConfigInventoryRow {
  key: string
  layer: ConfigLayer
  title: string
  scope: string
  default: unknown
  saved: unknown
  effective: unknown
  source: string
  apply_mode: 'hot' | 'restart' | 'next_run' | 'unknown' | string
  changed: boolean
  warning: string | null
}

export interface ConfigInventoryPayload {
  revision: string
  precedence: ConfigLayer[]
  counts: Partial<Record<ConfigLayer, number>>
  items: ConfigInventoryRow[]
  suspects: { key: string; message: string }[]
}

export function getConfigInventory(signal?: AbortSignal) {
  return fetchJson<ConfigInventoryPayload>('/api/config/inventory', { signal })
}

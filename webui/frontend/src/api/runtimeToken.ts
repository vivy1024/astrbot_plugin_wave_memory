import { fetchJson } from './client'

/** 令牌来源：环境变量（最高，管理台只读）/ 管理台设置 / 公开默认值 */
export type RuntimeTokenSource = 'env' | 'database' | 'default'

export interface RuntimeTokenUsage {
  accepted: number
  last_accepted_at: number | null
  last_accepted_from: string
  last_accepted_path: string
  rejected: number
  last_rejected_at: number | null
  last_rejected_from: string
  last_rejected_path: string
}

export interface RuntimeTokenPayload {
  token: string
  source: RuntimeTokenSource
  is_default: boolean
  env_var: string
  env_set: boolean
  can_edit: boolean
  min_length: number
  updated_at: number | null
  usage: RuntimeTokenUsage
}

const PATH = '/api/config/runtime-token'

export function getRuntimeToken(signal?: AbortSignal): Promise<RuntimeTokenPayload> {
  return fetchJson<RuntimeTokenPayload>(PATH, { signal })
}

export type RuntimeTokenChange = { action: 'generate' } | { action: 'reset' } | { token: string }

export function changeRuntimeToken(change: RuntimeTokenChange): Promise<RuntimeTokenPayload & { ok: true }> {
  return fetchJson<RuntimeTokenPayload & { ok: true }>(PATH, { method: 'POST', body: JSON.stringify(change) })
}

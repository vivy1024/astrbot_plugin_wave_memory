import { fetchJson } from './client'

/** 信念 v2：Bot 本人从经历里形成的看法（按 Bot，不按群）。legacy_archived 是只读的旧版人物判断。 */
export type BeliefStatus = 'sprout' | 'formed' | 'stable' | 'shaken' | 'faded' | 'vetoed' | 'legacy_archived'
export type BeliefCategory = 'self' | 'group' | 'thing' | 'world'

export interface BeliefActionAvailability {
  available: boolean
  reason_code: string | null
}

/** 依据日记：链接到具体日记（经历时间线节点编号 + 日期 + 群）。 */
export interface BeliefDiaryRef {
  diary_id: number
  date: string
  session_id: string
  group_name: string
  title: string
  polarity?: 'support' | 'challenge'
}

export interface BeliefHistoryItem {
  id: number
  event_type: string
  label: string
  from_status: BeliefStatus | null
  to_status: BeliefStatus | null
  from_label: string | null
  to_label: string | null
  reason: string
  actor: string
  occurred_at: number
  evidence: BeliefDiaryRef[]
}

export interface BeliefItem {
  id: number
  key: string
  legacy: boolean
  bot_id: string
  content: string
  category: BeliefCategory | null
  category_label: string | null
  status: BeliefStatus
  status_label: string
  strength?: number
  support_days?: number
  support_dates?: string[]
  confirm_runs?: number
  challenge_count?: number
  triggers?: string[]
  experience_phrase?: string | null
  first_seen_at?: number | null
  last_confirmed_at?: number | null
  status_changed_at?: number | null
  updated_at?: number | null
  created_at?: number | null
  revision?: number
  legacy_type?: string
  legacy_type_label?: string
  legacy_status?: string | null
  session_id?: string | null
  evidence: BeliefDiaryRef[]
  history: BeliefHistoryItem[]
  actions: Record<'veto' | 'restore', BeliefActionAvailability>
}

export interface BeliefGroup {
  status: BeliefStatus
  label: string
  description: string
  count: number
  items: BeliefItem[]
}

export interface BeliefReflectionRun {
  id: number
  run_date: string
  status: string
  attempts: number
  diary_ids: number[]
  counts: Record<string, number>
  error: string
  started_at: number
  finished_at: number | null
}

export interface BeliefsResponse {
  bot_id: string
  groups: BeliefGroup[]
  counts: Partial<Record<BeliefStatus, number>>
  policy: { form_min_days: number; stable_min_days: number; decay_days: number; max_active: number }
  reflection: {
    enabled: boolean
    every_n_days: number | null
    last_run: BeliefReflectionRun | null
    recent_runs: BeliefReflectionRun[]
  }
  capabilities: Record<'veto' | 'restore' | 'create' | 'edit' | 'physical_delete', BeliefActionAvailability>
  generated_at: number
}

export interface MutationResult {
  ok: boolean
  operation: { kind: string; status: string; id?: string }
  revision: number | string | null
  item?: BeliefItem
}

export function listBeliefs(botId: string, init?: { signal?: AbortSignal }): Promise<BeliefsResponse> {
  const params = new URLSearchParams({ bot_id: botId })
  return fetchJson<BeliefsResponse>(`/api/beliefs?${params.toString()}`, { signal: init?.signal })
}

function transitionBelief(item: BeliefItem, action: 'veto' | 'restore', botId: string, reason = ''): Promise<MutationResult> {
  if (item.legacy || item.revision === undefined) throw new Error('旧版信念只读，不能在这里修改')
  return fetchJson<MutationResult>(`/api/beliefs/${item.id}/${action}`, {
    method: 'POST',
    // 带上读到的版本号：别处改过（比如反思刚更新）时服务端返回 409，不会覆盖
    body: JSON.stringify({ bot_id: botId, revision: item.revision, reason }),
  })
}

export const vetoBelief = (item: BeliefItem, botId: string, reason = '') => transitionBelief(item, 'veto', botId, reason)
export const restoreBelief = (item: BeliefItem, botId: string) => transitionBelief(item, 'restore', botId)

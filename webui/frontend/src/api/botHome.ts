import { fetchJson } from './client'

/** 区块状态：ready 有数据（可能为空）；unavailable 数据源缺失；error 读取失败。 */
export type BotHomeSectionStatus = 'ready' | 'unavailable' | 'error'

export interface BotHomeSectionBase {
  status: BotHomeSectionStatus
  reason_code: string | null
}

/** 后端把会话描述成可展示/可深链接的地点。 */
export interface BotHomePlace {
  session_id: string | null
  visibility: string | null
  group_id: string | null
  group_name: string | null
  label: string
}

export interface BotHomeGroupCount extends BotHomePlace {
  count: number
}

export interface BotHomeSpeaker extends BotHomePlace {
  sender_id: string
  display_name: string
  count: number
  last_at: number | null
}

export interface BotHomeHighlight extends BotHomePlace {
  id: number
  sender_id: string
  sender_name: string
  is_self: boolean
  content: string
  timestamp: number
  importance: number | null
  source: string | null
  memory_type: string | null
  reason: 'self' | 'core' | 'important'
}

export interface BotHomeMemories extends BotHomeSectionBase {
  total?: number
  quarantined?: number
  by_source?: Record<string, number>
  top_groups?: BotHomeGroupCount[]
  top_speakers?: BotHomeSpeaker[]
  highlights?: BotHomeHighlight[]
}

export interface BotHomeMood extends BotHomePlace {
  valence: number | null
  arousal: number | null
  cause: string
  observed_at: number | null
  policy_version: string | null
}

export interface BotHomeConcern extends BotHomePlace {
  id: number
  topic: string
  intensity: number | null
  urgency: number | null
  status: string
  concern_type: string
  last_triggered: number | null
}

export interface BotHomeSoul extends BotHomeSectionBase {
  mood?: BotHomeMood | null
  concerns?: BotHomeConcern[]
  concerns_total?: number
  scope?: { session_id: string | null; visibility: string } | null
}

export interface BotHomeRelationship extends BotHomePlace {
  subject_principal_id: string
  user_id: string
  display_name: string
  event_count: number
  abs_delta: number
  dimensions: Record<string, number>
  affinity_before: number | null
  affinity_after: number | null
  state: string | null
  latest_reason: string
  latest_event_type: string
  latest_at: number | null
}

export interface BotHomeRelationships extends BotHomeSectionBase {
  items?: BotHomeRelationship[]
  people_changed?: number
  truncated?: boolean
}

export interface BotHomeLearnedSample extends BotHomePlace {
  id: number
  text: string
  status: string
  created_at: number | null
}

export interface BotHomeLearnedBucket extends BotHomeSectionBase {
  total: number | null
  by_status: Record<string, number>
  samples: BotHomeLearnedSample[]
}

export type BotHomeLearnedKind = 'facts' | 'beliefs' | 'jargon' | 'experiences'

export interface BotHomeLearned extends BotHomeSectionBase {
  facts?: BotHomeLearnedBucket
  beliefs?: BotHomeLearnedBucket
  jargon?: BotHomeLearnedBucket
  experiences?: BotHomeLearnedBucket
}

export interface BotHomeHitChannel {
  channel: string
  item_count: number
  tokens: number
}

export interface BotHomeReply extends BotHomePlace {
  trace_id: string
  timestamp: number
  latency_ms: number
  status: string
  sender_id: string | null
  sender_name: string
  message_preview: string
  source: string | null
  hit_channels: BotHomeHitChannel[]
}

export interface BotHomeReplies extends BotHomeSectionBase {
  items?: BotHomeReply[]
}

export interface BotHomePayload {
  bot: { db_id: string; name: string }
  window: { days: number; from_ts: number; to_ts: number }
  memories: BotHomeMemories
  soul: BotHomeSoul
  relationships: BotHomeRelationships
  learned: BotHomeLearned
  replies: BotHomeReplies
  meta: { generated_at: number; elapsed_ms: number; section_ms: Record<string, number> }
}

export interface GetBotHomeParams {
  botId: string
  days?: number
  signal?: AbortSignal
}

export function getBotHome({ botId, days = 1, signal }: GetBotHomeParams): Promise<BotHomePayload> {
  const query = new URLSearchParams({ bot_id: botId, days: String(days) })
  return fetchJson<BotHomePayload>(`/api/bot-home?${query.toString()}`, { signal })
}

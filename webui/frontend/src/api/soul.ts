import { fetchJson } from './client'
import type { EvidenceRef, ObjectRefDescriptor, PageResponse } from '@/components/shared/types'

export interface SoulScopeSelection {
  bot_id: string
  session_id: string
  visibility: 'group'
  subject_principal_id?: string
}

export interface SoulRecord {
  id: string | number
  summary?: string
  topic?: string
  event_summary?: string
  event_type?: string
  emotional_weight?: number | null
  timestamp?: number | null
  last_triggered?: number | null
  status?: 'active' | 'dormant' | 'progressing' | 'resolved' | 'expired' | 'archived' | string | null
  concern_type?: string | null
  intensity?: number | null
  urgency?: number | null
  last_progress_at?: number | null
  expected_resolution_at?: number | null
  resolution_note?: string | null
  origin_memory_id?: number | string | null
  origin_episode_id?: number | string | null
  revision?: number | string | null
  policy_version?: string | null
  evidence: EvidenceRef[]
  object_ref?: ObjectRefDescriptor | null
}

export interface RelationshipHistorySnapshot {
  dimension: string
  automatic_value: number | null
  manual_adjustment: number | null
  manual_override: number | null
  effective_value: number | null
  relationship_revision?: number | null
  updated_at?: number | null
}

export interface RelationshipHistoryItem {
  id: string
  event_id: string | number
  kind: 'automatic' | 'manual' | string
  event_type: string | null
  action: string | null
  dimension: string
  delta: number | null
  reason: string | null
  source_episode_id: number | string | null
  source_memory_id: number | string | null
  revision: number | null
  timestamp: number | null
  operation_id: string | null
  actor: string | null
  value_layer: string | null
  before: RelationshipHistorySnapshot | null
  after: RelationshipHistorySnapshot | null
  evidence: EvidenceRef[]
}

export interface RelationshipCalibrationPayload {
  object_ref: string | { ref: string }
  revision: number
  action: 'adjust' | 'override' | 'clear_override' | 'restore_auto'
  dimension: string
  delta?: number
  value?: number
  reason: string
  evidence: unknown[]
}

export function calibrateSoulRelationship(scope: SoulScopeSelection, payload: RelationshipCalibrationPayload): Promise<{ ok: boolean; operation: { kind: string; status: string; id?: string }; revision: number; item?: Record<string, unknown> }> {
  return fetchJson(`/api/people/relationships/commands/calibrate?${scopeQuery(scope)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export interface SoulStatePayload {
  scope: SoulScopeSelection & { kind: 'SoulScope'; platform_id: string; conversation_id: string }
  source: { health: 'healthy' | 'empty' | 'unavailable' | 'error'; reason_code: string | null }
  mood: {
    value: string | null
    state: 'known' | 'unknown'
    components: Record<string, number> | null
    policy_version: string | null
    revision: number | string | null
    evidence: EvidenceRef[]
  }
  concerns: PageResponse<SoulRecord>
  timeline: PageResponse<SoulRecord> & { revision?: number | string | null }
  relationship_history: PageResponse<RelationshipHistoryItem> & { revision?: number | string | null }
  /** Side-channel only; never mixed into relationship_history / affinity. */
  historical_audit?: {
    available: boolean
    total: number
    by_type: Array<{ event_type: string; count: number }>
    recent: Array<Record<string, unknown>>
    readonly: boolean
    affects_affinity: boolean
    source_table?: string
    reason_code?: string | null
  }
  soul_context: {
    status: 'available' | 'unknown' | 'unavailable' | string
    reason_code: string | null
    timezone: string | null
    circadian: Record<string, unknown> | string | null
    energy: number | string | null
    sleepiness: number | string | null
    source?: string
    mind_state?: {
      composure?: number
      social_drive?: number
      stance?: string
      stance_label?: string
      inner_drivers?: Record<string, number>
      narrative?: string
      group_focus?: {
        topic?: string
        last_speaker?: string
        silence_messages?: number
        msgs_per_minute?: number
        engaged_active?: boolean
        cooling_active?: boolean
        ignored_streak?: number
      }
      word?: string
      category?: string
      causes?: Array<{ text?: string; source?: string }>
      fatigue_reasons?: string[]
    }
  }
  relationship: {
    affinity: number | null
    state: 'known' | 'unknown' | string
    revision: number | string | null
    evidence: EvidenceRef[]
    /** historical_audit_summary texts; read-only, never affects affinity */
    evidence_summaries?: string[]
    people_ref: (ObjectRefDescriptor & { kind: 'relationship' }) | null
    dimensions?: Record<string, number> | null
    values?: Record<string, { dimension: string; automatic_value: number; manual_adjustment: number | null; manual_override: number | null; effective_value: number; relationship_revision: number; evidence: unknown[] }> | null
    calibration?: { available: boolean; reason_code: string | null }
  }
  capabilities: {
    mutate: { available: boolean; reason_code: string | null }
    runtime_refresh: { available: boolean; reason_code: string | null }
  }
  runtime_refresh: { status: string; operation: unknown; reason_code: string | null; refreshed_at?: number }
}

function scopeQuery(scope: SoulScopeSelection, extra: Record<string, string> = {}): string {
  return new URLSearchParams({ bot_id: scope.bot_id, session_id: scope.session_id, visibility: scope.visibility, ...(scope.subject_principal_id ? { subject_principal_id: scope.subject_principal_id } : {}), ...extra }).toString()
}

export function getSoulState(
  scope: SoulScopeSelection,
  limit: 25 | 50 | 100,
  offset: number,
  signal?: AbortSignal,
  timeRange: { from_ts?: number; to_ts?: number } = {},
): Promise<SoulStatePayload> {
  const extra = {
    limit: String(limit),
    offset: String(offset),
    ...(timeRange.from_ts !== undefined ? { from_ts: String(timeRange.from_ts) } : {}),
    ...(timeRange.to_ts !== undefined ? { to_ts: String(timeRange.to_ts) } : {}),
  }
  return fetchJson<SoulStatePayload>(`/api/soul/state?${scopeQuery(scope, extra)}`, { signal })
}

/**
 * 触发 Selves/Mood/Concern 只读重算（自省），不调用 LLM、不写入事件。
 * 幂等安全，用于 Soul 页「强制自省」入口。
 */
export async function refreshSoulState(
  scope: SoulScopeSelection,
  timeRange: { from_ts?: number; to_ts?: number } = {},
  signal?: AbortSignal,
): Promise<SoulStatePayload> {
  const extra = {
    limit: '25',
    offset: '0',
    ...(timeRange.from_ts !== undefined ? { from_ts: String(timeRange.from_ts) } : {}),
    ...(timeRange.to_ts !== undefined ? { to_ts: String(timeRange.to_ts) } : {}),
  }
  return fetchJson<SoulStatePayload>(`/api/soul/state/refresh?${scopeQuery(scope, extra)}`, {
    method: 'POST',
    signal,
  })
}

export interface BodyHormones {
  dopamine: number
  serotonin: number
  oxytocin: number
  cortisol: number
  melatonin: number
  boredom: number
}

export interface BodyEventSample {
  kind: string
  at: number
  deltas: Partial<Record<keyof BodyHormones, number>>
}

export interface BodySnapshot {
  bot_id: string
  enabled: boolean
  reason_code?: string
  hormones?: BodyHormones
  mood_bias?: number
  arousal_bias?: number
  sleepiness?: number
  asleep?: boolean
  proactive_drive?: number
  reply_length_factor?: number
  flags?: Record<string, boolean>
  events?: BodyEventSample[]
}

/** Bot 的身体（激素）快照：一个 Bot 一副身体，跨所有群、私聊与平台共用。 */
export function getBodySnapshot(botId: string, signal?: AbortSignal): Promise<BodySnapshot> {
  return fetchJson<BodySnapshot>(`/api/soul/body?${new URLSearchParams({ bot_id: botId }).toString()}`, { signal })
}

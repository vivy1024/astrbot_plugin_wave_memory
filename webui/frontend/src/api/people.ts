import { fetchJson } from './client'
import type { ObjectRefDescriptor, PageResponse, PageSize } from '@/components/shared/types'

export interface PeopleQuery {
  limit?: PageSize | 500
  offset?: number
  search?: string
  bot_id: string
  session_id: string
  visibility: 'group'
  user_id?: string
  subject_principal_id?: string
  relationship_state?: 'all' | 'known' | 'unknown'
  min_affinity?: number | string
  max_affinity?: number | string
  min_interactions?: number | string
  alias_filter?: 'all' | 'has' | 'none'
  sort_by?: 'name' | 'interactions' | 'affinity'
  sort_order?: 'asc' | 'desc'
}

export interface PersonScope {
  user_id: string
  group_id: string
  bot_id: string
}

export interface PersonItem {
  id?: number
  user_id: string
  group_id: string
  bot_id: string
  nickname?: string | null
  display_name: string
  aliases: unknown[]
  interaction_count?: number
  scope: PersonScope
  scope_key: string
  metadata: Record<string, unknown>
  registry_metadata: Record<string, unknown>
  person_registry: Record<string, unknown>
  affinity: number | null
  affinity_status: 'unavailable' | 'available' | string
  affinity_reason_code: string
}

export interface RelationshipValue {
  dimension: string
  automatic_value: number
  manual_adjustment: number | null
  manual_override: number | null
  effective_value: number
  relationship_revision: number
  evidence: unknown[]
}

export interface HistoricalAuditSummary {
  available: boolean
  total: number
  by_type: Array<{ event_type: string; count: number }>
  recent: Array<{
    event_type: string
    dimension: string
    delta: number | string | null
    reason: string
    occurred_at?: number | null
    legacy_event_id?: string
  }>
  readonly: boolean
  affects_affinity: boolean
  source_table?: string
  reason_code?: string
}

export interface RelationshipItem {
  subject_principal_id: string
  person: PersonItem
  affinity: number | null
  state: 'known' | 'unknown' | string
  revision: number | null
  values: Record<string, RelationshipValue> | null
  evidence: unknown[]
  /** Extracted historical_audit_summary texts; read-only, does not affect affinity. */
  evidence_summaries?: string[]
  object_ref: (ObjectRefDescriptor & { kind: 'relationship' }) | null
  calibration: { available: boolean; reason_code: string | null }
  historical_audit?: HistoricalAuditSummary
}

export interface RelationshipQuery extends PeopleQuery {
  user_id?: string
  include_historical_audit?: boolean | 0 | 1 | 'true' | 'false'
}

export interface HistoricalAuditQuery extends PeopleQuery {
  subject_principal_id?: string
  user_id?: string
}

export interface HistoricalAuditEventItem {
  id: number
  legacy_event_id: string
  bot_id: string
  session_id: string
  visibility: string
  group_id: string
  subject_principal_id: string
  event_type: string
  dimension: string
  delta: number
  reason: string
  occurred_at: number | null
  source_episode_id: number | null
  source_memory_id: number | null
  created_at: number | null
  readonly: true
  affects_affinity: false
  source: 'scoped_soul_relationship_legacy_events'
}

export interface HistoricalAuditPage extends PageResponse<HistoricalAuditEventItem> {
  scope: Record<string, unknown>
  subject_principal_id: string
  summary: HistoricalAuditSummary
  readonly: true
  affects_affinity: false
  historical_audit: true
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

export interface RelationshipCalibrationResponse {
  ok: boolean
  operation: { kind: string; status: string; id?: string }
  revision: number
  item?: Record<string, unknown>
}

export function getRelationships(query: RelationshipQuery, signal?: AbortSignal): Promise<PageResponse<RelationshipItem>> {
  return fetchJson<PageResponse<RelationshipItem>>(`/api/people/relationships${queryString(query)}`, { signal })
}

export function getRelationshipHistoricalAudit(
  query: HistoricalAuditQuery,
  signal?: AbortSignal,
): Promise<HistoricalAuditPage> {
  return fetchJson<HistoricalAuditPage>(
    `/api/people/relationships/historical-audit${queryString(query)}`,
    { signal },
  )
}

export function calibrateRelationship(query: PeopleQuery, payload: RelationshipCalibrationPayload): Promise<RelationshipCalibrationResponse> {
  return fetchJson<RelationshipCalibrationResponse>(`/api/people/relationships/commands/calibrate${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export interface ClearImpressionPayload {
  user_id: string
  reason: string
}

export function clearImpression(query: PeopleQuery, payload: ClearImpressionPayload): Promise<RelationshipCalibrationResponse> {
  return fetchJson<RelationshipCalibrationResponse>(`/api/people/commands/clear-impression${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload) })
}

function queryString(query: object): string {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  const serialized = params.toString()
  return serialized ? `?${serialized}` : ''
}

export function getPeople(query: PeopleQuery): Promise<PageResponse<PersonItem>> {
  return fetchJson<PageResponse<PersonItem>>(`/api/people${queryString(query)}`)
}

export interface PersonTimelineQuery {
  bot_id: string
  session_id: string
  visibility: 'group'
  user_id?: string
  kind?: 'impression' | 'affinity' | 'person_fact' | ''
  search?: string
  limit?: PageSize
  offset?: number
}

export interface PersonTimelineEventItem {
  id: number
  user_id: string
  group_id: string
  bot_id: string
  kind: 'impression' | 'affinity' | 'person_fact' | string
  summary: string
  detail: string
  source_quote?: string | null
  source_memory_id?: number | null
  source_quote_inferred?: boolean
  subject: string
  predicate: string
  object: string
  confidence: number | null
  occurred_at: number | null
  created_at: number | null
  event_type: string
  dimension: string
  delta: number | string | null
  readonly: true
  timeline: 'impression'
}

export interface PersonTimelinePage extends PageResponse<PersonTimelineEventItem> {
  scope?: Record<string, unknown>
  timeline: 'impression'
  readonly: true
  user_id?: string | null
  kind?: string | null
  search?: string | null
}

export function getPersonTimeline(query: PersonTimelineQuery, signal?: AbortSignal): Promise<PersonTimelinePage> {
  return fetchJson<PersonTimelinePage>(`/api/people/timeline${queryString(query)}`, { signal })
}

/** 关系小传：Bot 以自己的视角为这个人写的一段小传（只由公开场合的记录生成）。 */
export interface RelationshipPortraitItem {
  bot_id: string
  user_id: string
  content: string
  version: number
  status: 'auto' | 'edited' | string
  source_max_event_id: number
  source_event_count: number
  provider: string
  edited_by: string
  generated_at: number | null
  reviewed_through_event_id: number
  reviewed_at: number | null
  pending_update: boolean
  pending_since: number | null
  last_error: string
  last_error_at: number | null
  last_attempt_at: number | null
  created_at: number
  updated_at: number
}

export interface RelationshipPortraitView {
  user_id: string
  user_ids: string[]
  owner_user_id: string
  version: number
  item: RelationshipPortraitItem | null
  last_error: string | null
  last_error_at: number | null
  history_count: number
  timeline: {
    non_private_total: number
    max_event_id: number
    new_since_portrait: number
    new_relational_since_portrait: number
  }
  settings: {
    auto_enabled: boolean
    injection_enabled: boolean
    service_available: boolean
    max_chars: number
    min_events: number
  }
}

export interface RelationshipPortraitHistoryItem {
  id: number
  bot_id: string
  user_id: string
  version: number
  content: string
  status: 'auto' | 'edited' | string
  source_max_event_id: number
  source_event_count: number
  provider: string
  edited_by: string
  generated_at: number | null
  replaced_at: number
  replaced_by: string
}

export interface RelationshipPortraitQuery {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export interface RelationshipPortraitMutation {
  ok: boolean
  operation: { kind: string; status: string; id?: string }
  revision: number
  item: RelationshipPortraitView
}

export function getRelationshipPortrait(
  query: RelationshipPortraitQuery & { user_id: string },
  signal?: AbortSignal,
): Promise<RelationshipPortraitView> {
  return fetchJson<RelationshipPortraitView>(`/api/people/portrait${queryString(query)}`, { signal })
}

export function getRelationshipPortraitHistory(
  query: RelationshipPortraitQuery & { user_id: string; limit?: number },
): Promise<{ items: RelationshipPortraitHistoryItem[]; owner_user_id: string; current_version: number }> {
  return fetchJson(`/api/people/portrait/history${queryString(query)}`)
}

export function saveRelationshipPortrait(
  query: RelationshipPortraitQuery,
  payload: { user_id: string; version: number; content: string },
): Promise<RelationshipPortraitMutation> {
  return fetchJson<RelationshipPortraitMutation>(`/api/people/portrait/commands/save${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export function regenerateRelationshipPortrait(
  query: RelationshipPortraitQuery,
  payload: { user_id: string; version: number },
): Promise<RelationshipPortraitMutation> {
  // 要等模型写完，给足时间
  return fetchJson<RelationshipPortraitMutation>(`/api/people/portrait/commands/regenerate${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload), timeoutMs: 120000 })
}

export function keepRelationshipPortrait(
  query: RelationshipPortraitQuery,
  payload: { user_id: string; version: number },
): Promise<RelationshipPortraitMutation> {
  return fetchJson<RelationshipPortraitMutation>(`/api/people/portrait/commands/keep${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export interface LegacyPeopleQuery {
  bot_id: string
  group_id: string
  search?: string
  limit?: PageSize
  offset?: number
}

export function getLegacyPeople(query: LegacyPeopleQuery): Promise<PageResponse<PersonItem>> {
  return fetchJson<PageResponse<PersonItem>>(`/api/people/legacy/audit${queryString(query)}`)
}

/** 管理员确认的跨平台同一人（按 Bot 保存）。 */
export interface IdentityPerson {
  person_key: string
  principals: string[]
  note: string
  created_by?: string
  created_at?: number
}

export function getIdentityLinks(query: PeopleQuery): Promise<{ bot_id: string; items: IdentityPerson[] }> {
  return fetchJson<{ bot_id: string; items: IdentityPerson[] }>(`/api/people/identity-links${queryString(query)}`)
}

export function linkIdentities(query: PeopleQuery, payload: { principals: string[]; note?: string }): Promise<{ item: IdentityPerson }> {
  return fetchJson<{ item: IdentityPerson }>(`/api/people/identity-links${queryString(query)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export function unlinkIdentity(query: PeopleQuery, principal: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/people/identity-links/unlink${queryString(query)}`, { method: 'POST', body: JSON.stringify({ principal }) })
}

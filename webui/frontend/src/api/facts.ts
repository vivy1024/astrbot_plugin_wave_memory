import { fetchJson } from './client'
import type { EvidenceRef, ObjectRefDescriptor, PageResponse } from '@/components/shared/types'

/** 事实可来自群聊或私聊；私聊事实只在该私聊会话下审核与展示。 */
export type FactVisibility = 'group' | 'private'
export type FactReviewAction = 'approve' | 'reject' | 'replace' | 'coexist'
export type FactBatchReviewAction = 'approve' | 'reject'
export type FactReviewHint = '' | 'suggest_approve' | 'suggest_reject' | 'needs_review'

export interface FactSelection {
  bot_id: string
  session_id: string
  visibility: FactVisibility
}

export interface FactActionAvailability {
  available: boolean
  reason_code: string | null
  command?: string
}

export interface ScopedFactItem {
  id: number
  bot_id: string
  session_id: string
  visibility: FactVisibility
  subject: string
  predicate: string
  object: string
  confidence: number
  status: string
  revision: number
  /** Unix 秒；null 表示该端没有时间限制。 */
  valid_from: number | null
  valid_until: number | null
  effective: boolean
  ineffective_reason: string | null
  review_actions: FactReviewAction[]
  conflict_candidates: FactConflictCandidate[]
  source_memory_id?: number | null
  provenance?: Record<string, unknown>
  evidence: EvidenceRef[]
  evidence_status: 'available' | 'unavailable'
  review_hint: FactReviewHint
  object_ref: ObjectRefDescriptor | null
  capabilities: Record<'review' | 'batch_review', FactActionAvailability>
  editable: boolean
  updated_at?: number
  created_at?: number
}

export type FactConflictCandidate = Pick<ScopedFactItem, 'id' | 'revision' | 'subject' | 'predicate' | 'object' | 'status' | 'valid_from' | 'valid_until'>

export interface ScopedFactsResponse extends PageResponse<ScopedFactItem> {
  scope: unknown
  capabilities: Record<string, FactActionAvailability>
}

export interface FactListFilters extends FactSelection {
  limit: number
  offset: number
  search?: string
  status?: string
}

export interface FactReviewRecord {
  id: number
  fact_id: number
  action: FactReviewAction
  actor: string
  reason: string
  from_status: string
  to_status: string
  relation: string
  conflict_with_fact_id: number | null
  reviewed_at: number
}

function scopeEnvelope(scope: FactSelection) {
  const [platform_id, kind, ...conversation] = scope.session_id.split(':')
  const conversation_id = conversation.join(':')
  return {
    kind: 'RuntimeScope',
    payload: {
      bot_id: scope.bot_id,
      visibility: scope.visibility,
      session: { id: scope.session_id, platform_id, kind, conversation_id },
      subject_principal_id: null,
    },
  }
}

export function listFacts(filters: FactListFilters): Promise<ScopedFactsResponse> {
  const params = new URLSearchParams({
    bot_id: filters.bot_id,
    session_id: filters.session_id,
    visibility: filters.visibility,
    limit: String(filters.limit),
    offset: String(filters.offset),
  })
  if (filters.search) params.set('search', filters.search)
  if (filters.status) params.set('status', filters.status)
  return fetchJson<ScopedFactsResponse>(`/api/facts?${params.toString()}`)
}

function validateReview(item: ScopedFactItem, action: FactReviewAction, scope: FactSelection) {
  if (item.bot_id !== scope.bot_id || item.session_id !== scope.session_id || item.visibility !== scope.visibility) {
    throw new Error('该事实不属于当前 Bot 和会话，请刷新后重试')
  }
  if (!item.object_ref?.ref) throw new Error('该事实没有服务端签发的 ObjectRef，不能安全审核')
  if (!Number.isSafeInteger(item.revision) || item.revision < 1) throw new Error('缺少有效审核版本，请刷新后重试')
  if (!item.editable || !item.capabilities?.review?.available || !item.review_actions?.includes(action)) {
    throw new Error('服务端未允许此审核操作，请刷新后重试')
  }
}

/** 修正事实的客体 / 主体 / 关系 / 证据原话，改完仍回到待审，需要重新批准。 */
export interface FactEditFields {
  object?: string
  subject?: string
  predicate?: string
  source_quote?: string
}

export function editFact(item: ScopedFactItem, fields: FactEditFields, scope: FactSelection) {
  if (item.bot_id !== scope.bot_id || item.session_id !== scope.session_id || item.visibility !== scope.visibility) {
    throw new Error('该事实不属于当前 Bot 和会话，请刷新后重试')
  }
  if (!item.object_ref?.ref) throw new Error('该事实没有服务端签发的 ObjectRef，不能安全修改')
  if (!Number.isSafeInteger(item.revision) || item.revision < 1) throw new Error('缺少有效版本，请刷新后重试')
  if (!item.editable) throw new Error('服务端未允许修改此事实，请刷新后重试')
  const cleaned = Object.fromEntries(
    Object.entries(fields).filter(([, value]) => typeof value === 'string' && value.trim().length > 0).map(([key, value]) => [key, (value as string).trim()]),
  )
  if (!Object.keys(cleaned).length) throw new Error('请至少修改一个字段')
  return fetchJson<{ ok: boolean; operation: { kind: string; status: string }; revision: number | null; item: ScopedFactItem }>(
    `/api/facts/${item.id}/edit`,
    {
      method: 'POST',
      body: JSON.stringify({
        scope: scopeEnvelope(scope),
        object_ref: item.object_ref,
        revision: item.revision,
        ...cleaned,
      }),
    },
  )
}

export function reviewFact(item: ScopedFactItem, action: FactReviewAction, scope: FactSelection, reason?: string, target?: FactConflictCandidate) {  validateReview(item, action, scope)
  if (action === 'replace' && (!target || !Number.isSafeInteger(target.revision) || target.revision < 1
    || !item.conflict_candidates?.some((candidate) => candidate.id === target.id && candidate.revision === target.revision))) {
    throw new Error('请选择当前冲突列表中的替换目标及版本')
  }
  return fetchJson<{ ok: boolean; operation: { kind: string; status: string }; revision: number | null; item: ScopedFactItem }>(
    `/api/facts/${item.id}/${action}`,
    {
      method: 'POST',
      body: JSON.stringify({
        scope: scopeEnvelope(scope),
        object_ref: item.object_ref,
        revision: item.revision,
        reason: reason ?? '',
        ...(action === 'replace' ? { supersedes_fact_id: target!.id, supersedes_revision: target!.revision } : {}),
      }),
    },
  )
}

export function approveFact(item: ScopedFactItem, scope: FactSelection, reason?: string) {
  return reviewFact(item, 'approve', scope, reason)
}

export function rejectFact(item: ScopedFactItem, scope: FactSelection, reason?: string) {
  return reviewFact(item, 'reject', scope, reason)
}

export function batchReviewFacts(
  items: ScopedFactItem[],
  action: FactBatchReviewAction,
  scope: FactSelection,
): Promise<{
  operation_kind: string
  status: string
  succeeded: Array<{ id: number; status: string; revision: number }>
  failed: Array<{ id: number | null; code: string }>
}> {
  if (!items.length) throw new Error('批量审核至少需要一条事实')
  if (items.some((item) => !item.object_ref?.ref)) throw new Error('批量审核要求每条事实都带有服务端签发的 ObjectRef')
  items.forEach((item) => {
    validateReview(item, action, scope)
    if (!item.capabilities?.batch_review?.available) throw new Error('服务端未允许批量审核此事实')
  })
  return fetchJson('/api/facts/commands/batch-review', {
    method: 'POST',
    body: JSON.stringify({
      scope: scopeEnvelope(scope),
      action,
      items: items.map((item) => ({ id: item.id, object_ref: item.object_ref, revision: item.revision })),
    }),
  })
}

export interface FactEvidenceMessage {
  id: number
  group_id?: string
  sender_id?: string
  sender_name?: string
  content: string
  timestamp: number
  role: 'before' | 'anchor' | 'after'
}

export interface FactEvidencePayload {
  ok: boolean
  fact: {
    id: number
    subject: string
    predicate: string
    object: string
    source_quote?: string
    revision: number
  }
  anchor: FactEvidenceMessage | null
  messages: FactEvidenceMessage[]
  source_quote?: string
  used_fallback: boolean
}

export function getFactEvidence(fact: ScopedFactItem, scope: FactSelection, before = 15, after = 15): Promise<FactEvidencePayload> {
  const params = new URLSearchParams({
    bot_id: scope.bot_id,
    session_id: scope.session_id,
    visibility: scope.visibility,
    before: String(before),
    after: String(after),
  })
  return fetchJson<FactEvidencePayload>(`/api/facts/${fact.id}/evidence?${params.toString()}`)
}

export function listFactReviews(scope: FactSelection, factId?: number, limit = 50): Promise<{ items: FactReviewRecord[] }> {
  const params = new URLSearchParams({
    bot_id: scope.bot_id,
    session_id: scope.session_id,
    visibility: scope.visibility,
    limit: String(limit),
  })
  if (factId) params.set('fact_id', String(factId))
  return fetchJson<{ items: FactReviewRecord[] }>(`/api/facts/reviews?${params.toString()}`)
}

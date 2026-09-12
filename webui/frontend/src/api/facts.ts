import { fetchJson } from './client'
import type { EvidenceRef, ObjectRefDescriptor, PageResponse } from '@/components/shared/types'

export type FactReviewAction = 'approve' | 'reject'
export type FactReviewHint = '' | 'suggest_approve' | 'suggest_reject' | 'needs_review'

export interface FactSelection {
  bot_id: string
  session_id: string
  visibility: 'group'
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
  visibility: 'group'
  subject: string
  predicate: string
  object: string
  confidence: number
  status: string
  revision: number
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
  const [platform_id, kind, conversation_id] = scope.session_id.split(':', 3)
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

function transitionFact(item: ScopedFactItem, action: FactReviewAction, scope: FactSelection, reason?: string) {
  if (!item.object_ref?.ref) throw new Error('该事实没有服务端签发的 ObjectRef，不能安全审核')
  return fetchJson<{ ok: boolean; operation: { kind: string; status: string }; revision: number | null }>(
    `/api/facts/${item.id}/${action}`,
    {
      method: 'POST',
      body: JSON.stringify({
        scope: scopeEnvelope(scope),
        object_ref: item.object_ref,
        revision: item.revision,
        reason: reason ?? '',
      }),
    },
  )
}

export function approveFact(item: ScopedFactItem, scope: FactSelection, reason?: string) {
  return transitionFact(item, 'approve', scope, reason)
}

export function rejectFact(item: ScopedFactItem, scope: FactSelection, reason?: string) {
  return transitionFact(item, 'reject', scope, reason)
}

export function batchReviewFacts(
  items: ScopedFactItem[],
  action: FactReviewAction,
  scope: FactSelection,
): Promise<{
  operation_kind: string
  status: string
  succeeded: Array<{ id: number; status: string; revision: number }>
  failed: Array<{ id: number | null; code: string }>
}> {
  if (!items.length) throw new Error('批量审核至少需要一条事实')
  if (items.some((item) => !item.object_ref?.ref)) throw new Error('批量审核要求每条事实都带有服务端签发的 ObjectRef')
  return fetchJson('/api/facts/commands/batch-review', {
    method: 'POST',
    body: JSON.stringify({
      scope: scopeEnvelope(scope),
      action,
      items: items.map((item) => ({ id: item.id, object_ref: item.object_ref, revision: item.revision })),
    }),
  })
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

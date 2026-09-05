import { fetchJson } from './client'
import type { EvidenceRef, ObjectRefDescriptor, PageResponse } from '@/components/shared/types'

export type BeliefType = 'self_identity' | 'person_judgment' | 'world_view' | 'preference'

export interface ScopedSelection {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export interface BeliefActionAvailability {
  available: boolean
  reason_code: string | null
}

export interface BeliefGatingSummary {
  decision: 'direct' | 'pending' | 'quarantine' | null
  reason_code: string | null
  trust: number | null
  hostility: number | null
  subjects: string[]
  policy_version: string | null
  review_required: boolean
  unknown_subjects: string[]
  relationship_revisions: Record<string, number>
}

export interface BeliefItem {
  id: number
  belief_key: string
  content: string
  type: BeliefType
  status: 'pending' | 'active' | 'archived' | 'quarantined'
  confidence: number | null
  confidence_components: Record<string, number> | null
  confidence_policy_version: string | null
  confidence_evidence?: Record<string, number | null>
  gating: BeliefGatingSummary
  anchor_sentence: string | null
  evidence_health: 'available' | 'unavailable' | 'quarantined' | 'unknown'
  quarantine_reason: string | null
  bot_id: string
  session_id: string
  visibility: 'group'
  evidence: EvidenceRef[]
  object_ref: ObjectRefDescriptor | null
  revision: number
  actions: Record<'approve' | 'archive' | 'restore' | 'delete', BeliefActionAvailability>
  updated_at?: number
}

export interface BeliefFilters extends ScopedSelection {
  limit: 25 | 50 | 100
  offset: number
  type?: BeliefType
  status?: string
  search?: string
  evidence_health?: string
}

export interface BeliefCapability {
  available: boolean
  reason_code: string | null
  actions?: Array<'approve' | 'archive'>
}

export interface BeliefsResponse extends PageResponse<BeliefItem> {
  scope: { kind: string; payload: unknown }
  capabilities: {
    lifecycle: BeliefCapability
    batch_lifecycle: BeliefCapability
    evidence: BeliefCapability
    create: BeliefCapability
    edit: BeliefCapability
    physical_delete: BeliefCapability
    select_all_matching: BeliefCapability
  }
}

export interface BeliefEvidenceMessage {
  id: number
  group_id: string | null
  sender_id: string | null
  sender_name: string | null
  content: string
  timestamp: number
  role: 'before' | 'anchor' | 'after'
}

export interface BeliefEvidencePayload {
  ok: boolean
  belief: Pick<BeliefItem, 'id' | 'content' | 'type' | 'revision'>
  scope: { kind: string; payload: unknown }
  anchor: BeliefEvidenceMessage | null
  messages: BeliefEvidenceMessage[]
  memories: BeliefEvidenceMessage[]
  support_anchors: Array<BeliefEvidenceMessage & { polarity?: 'support' | 'challenge' }>
  challenge_anchors: Array<BeliefEvidenceMessage & { polarity?: 'support' | 'challenge' }>
  relationship_events: Array<Record<string, unknown>>
  episodes: Array<Record<string, unknown>>
  used_fallback: boolean
  reason_code: string | null
}

export interface MutationResult {
  ok: boolean
  operation: { kind: string; status: string; id?: string }
  revision: number | string | null
  item?: { id: number; status: string }
}

function scopeEnvelope(scope: ScopedSelection) {
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

export function listBeliefs(filters: BeliefFilters): Promise<BeliefsResponse> {
  const params = new URLSearchParams({
    bot_id: filters.bot_id,
    session_id: filters.session_id,
    visibility: filters.visibility,
    limit: String(filters.limit),
    offset: String(filters.offset),
  })
  if (filters.type) params.set('type', filters.type)
  if (filters.status) params.set('status', filters.status)
  if (filters.search) params.set('search', filters.search)
  if (filters.evidence_health) params.set('evidence_health', filters.evidence_health)
  return fetchJson<BeliefsResponse>(`/api/beliefs?${params.toString()}`)
}

export function getBeliefEvidence(item: BeliefItem, scope: ScopedSelection, before = 15, after = 15): Promise<BeliefEvidencePayload> {
  if (!item.object_ref?.ref) throw new Error('该信念没有服务端签发的 ObjectRef，不能安全读取证据')
  const params = new URLSearchParams({
    bot_id: scope.bot_id,
    session_id: scope.session_id,
    visibility: scope.visibility,
    ref: item.object_ref.ref,
    before: String(before),
    after: String(after),
  })
  return fetchJson<BeliefEvidencePayload>(`/api/beliefs/${item.id}/evidence?${params.toString()}`)
}

export function batchTransitionBeliefs(items: BeliefItem[], action: 'approve' | 'archive', scope: ScopedSelection): Promise<{
  ok: boolean
  operation: { kind: string; status: string }
  transitioned_count: number
  items: Array<{ id: number; status: string }>
}> {
  if (!items.length || items.some((item) => !item.object_ref?.ref)) throw new Error('批量操作要求每条信念都带有服务端签发的 ObjectRef')
  return fetchJson<{
    ok: boolean
    operation: { kind: string; status: string }
    transitioned_count: number
    items: Array<{ id: number; status: string }>
  }>('/api/beliefs/commands/batch-lifecycle', {
    method: 'POST',
    body: JSON.stringify({
      scope: scopeEnvelope(scope),
      action,
      items: items.map((item) => ({ id: item.id, object_ref: item.object_ref, revision: item.revision })),
    }),
  })
}

function transitionBelief(item: BeliefItem | number, action: 'approve' | 'archive', scope: ScopedSelection): Promise<MutationResult> {
  const id = typeof item === 'number' ? item : item.id
  const objectRef = typeof item === 'number' ? null : item.object_ref
  const revision = typeof item === 'number' ? null : item.revision
  // Keep the historical client call shape for source compatibility; the server
  // rejects this branch with object_ref_revision_required instead of mutating.
  if (!objectRef) {
    return fetchJson<MutationResult>(`/api/beliefs/${id}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ scope: scopeEnvelope(scope) }),
    })
  }
  return fetchJson<MutationResult>(`/api/beliefs/${id}/${action}`, {
    method: 'POST',
    body: JSON.stringify({ scope: scopeEnvelope(scope), object_ref: objectRef, revision }),
  })
}

export const approveBelief = (item: BeliefItem | number, scope: ScopedSelection) => transitionBelief(item, 'approve', scope)
export const archiveBelief = (item: BeliefItem | number, scope: ScopedSelection) => transitionBelief(item, 'archive', scope)

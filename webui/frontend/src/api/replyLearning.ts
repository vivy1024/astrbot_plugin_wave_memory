import { fetchJson, type ApiError } from './client'
import type { PageResponse, PageSize } from '@/components/shared/types'
import { humanizeApiError } from '@/lib/reason-label'

export interface ReplyLearningScope {
  bot_id: string
  session_id: string
  visibility: 'group'
}

export type ExpressionSituation = 'acknowledge' | 'clarify' | 'encourage' | 'celebrate' | 'boundary' | 'humor'
export type ExpressionReviewAction = 'approve' | 'reject'
export type ExpressionStatus = 'pending' | 'approved' | 'rejected'
export type ReplyOutcome = 'accepted' | 'corrected' | 'rejected' | 'unknown'

interface RuntimeGroupScope {
  bot_id: string
  visibility: 'group'
  session: { id: string; platform_id: string; kind: 'group'; conversation_id: string }
  subject_principal_id: string | null
}

export interface ExpressionCandidate extends ReplyLearningScope {
  id: number
  speaker_id: string
  source_memory_id: number
  content: string
  situation: ExpressionSituation
  status: ExpressionStatus
  revision: number
  source_hash: string
  reviewer: string
  review_note: string
  reviewed_at: number | null
  created_at: number
  evidence_available: boolean
  scope: RuntimeGroupScope
  artifact: unknown
  evidence_refs: unknown[]
  evidence_bindings: unknown[]
}

export interface ReplyFeedbackEvent extends ReplyLearningScope {
  id: number
  reply_memory_id: number
  response_memory_id: number
  speaker_id: string
  outcome: ReplyOutcome
  situation: ExpressionSituation | ''
  source_quote: string
  created_at: number
  evidence_available: boolean
  artifacts: unknown[]
}

export interface ExpressionCandidatesResponse extends PageResponse<ExpressionCandidate> {
  scope: RuntimeGroupScope
  enabled: boolean
}

export interface ReplyFeedbackResponse extends PageResponse<ReplyFeedbackEvent> {
  scope: RuntimeGroupScope
  enabled: boolean
  signals: Record<ReplyOutcome, number>
}

interface LearningListQuery extends ReplyLearningScope {
  limit: PageSize
  offset: number
}

export function isReplyLearningScope(scope: { bot_id: string; session_id: string; visibility: string }): scope is ReplyLearningScope {
  return Boolean(scope.bot_id.trim() && scope.visibility === 'group' && /^[^:]+:group:.+$/u.test(scope.session_id))
}

function runtimeScope(scope: ReplyLearningScope): RuntimeGroupScope {
  if (!isReplyLearningScope(scope)) throw new Error('请先选择有效的 Bot 和群聊。')
  const [platform_id, , ...conversation] = scope.session_id.split(':')
  return {
    bot_id: scope.bot_id,
    visibility: 'group',
    session: { id: scope.session_id, platform_id, kind: 'group', conversation_id: conversation.join(':') },
    subject_principal_id: null,
  }
}

function queryParams(query: LearningListQuery): URLSearchParams {
  runtimeScope(query)
  return new URLSearchParams({
    bot_id: query.bot_id, session_id: query.session_id, visibility: query.visibility,
    limit: String(query.limit), offset: String(query.offset),
  })
}

export function listExpressionCandidates(query: LearningListQuery & { status?: ExpressionStatus }, signal?: AbortSignal): Promise<ExpressionCandidatesResponse> {
  const params = queryParams(query)
  params.set('status', query.status ?? 'pending')
  return fetchJson(`/api/reply-learning/expressions?${params}`, { signal })
}

export function listReplyFeedback(query: LearningListQuery, signal?: AbortSignal): Promise<ReplyFeedbackResponse> {
  return fetchJson(`/api/reply-learning/feedback?${queryParams(query)}`, { signal })
}

export function reviewExpression(
  item: ExpressionCandidate,
  scope: ReplyLearningScope,
  action: ExpressionReviewAction,
  options: { idempotencyKey: string; styleOnly: boolean },
): Promise<{ ok: boolean; item: ExpressionCandidate }> {
  const payload = runtimeScope(scope)
  if (item.bot_id !== scope.bot_id || item.session_id !== scope.session_id || item.visibility !== scope.visibility) {
    throw new Error('该表达不属于当前 Bot 和群聊，请刷新后重试。')
  }
  if (!Number.isSafeInteger(item.revision) || item.revision < 1) throw new Error('缺少有效审核版本，请刷新后重试。')
  if (action === 'approve' && !options.styleOnly) throw new Error('请先确认只学习表达方式。')
  return fetchJson(`/api/reply-learning/expressions/${item.id}/review`, {
    method: 'POST',
    body: JSON.stringify({
      scope: { kind: 'RuntimeScope', payload },
      action,
      revision: item.revision,
      idempotency_key: options.idempotencyKey,
      style_only: options.styleOnly,
      note: '',
    }),
  })
}

export function replyLearningErrorMessage(reason: unknown, fallback: string): string {
  const error = reason as ApiError | undefined
  const payload = error?.payload as { error?: { code?: string } } | undefined
  const code = payload?.error?.code ?? error?.message ?? ''
  const labels: Record<string, string> = {
    stale_revision: '该表达已被其他操作更新，请核对刷新后的候选再审核。',
    idempotency_conflict: '审核请求与已处理的操作冲突，请刷新并核对当前状态。',
    evidence_unavailable_or_unsafe: '来源证据已变化、不可用或未通过安全检查，暂不能批准。',
    style_only_confirmation_required: '请先确认只学习表达方式，不学习说话人的身份或经历。',
    revision_required: '缺少有效审核版本，请刷新后重试。',
    idempotency_key_required: '审核请求缺少有效操作标识，请刷新后重试。',
    scoped_object_not_found: '当前 Bot 和群聊中已找不到这条表达，请刷新列表。',
    group_scope_required: '表达学习仅支持群聊，请选择 Bot 和群。',
    scope_required: '请先选择 Bot 和群聊。',
    scope_mismatch: '请求的 Bot 或群聊不一致，请重新选择后刷新。',
    service_unavailable: '表达学习或回复反馈服务暂不可用，请稍后重试。',
    store_unavailable: '表达学习或回复反馈数据暂时读取失败，请稍后重试。',
  }
  return labels[code] ?? (error?.status === 409 ? '数据已被其他操作更新，请刷新后核对再操作。' : humanizeApiError(reason, fallback))
}

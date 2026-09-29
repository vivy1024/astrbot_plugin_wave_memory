import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchJson } from './client'
import { listExpressionCandidates, listReplyFeedback, replyLearningErrorMessage, reviewExpression, type ExpressionCandidate, type ReplyLearningScope } from './replyLearning'

vi.mock('./client', () => ({ fetchJson: vi.fn() }))
const scope: ReplyLearningScope = { bot_id: 'bot-a', session_id: 'qq:group:42:topic', visibility: 'group' }
const item = { ...scope, id: 7, revision: 3 } as ExpressionCandidate

beforeEach(() => vi.mocked(fetchJson).mockResolvedValue({ ok: true }))

describe('reply learning wire contract', () => {
  it('uses exact group scope and independent pagination for both read routes', async () => {
    const controller = new AbortController()
    await listExpressionCandidates({ ...scope, limit: 25, offset: 50 }, controller.signal)
    await listReplyFeedback({ ...scope, limit: 50, offset: 100 }, controller.signal)
    const calls = vi.mocked(fetchJson).mock.calls
    const expressions = new URL(calls[0][0], 'http://localhost')
    const feedback = new URL(calls[1][0], 'http://localhost')
    expect(expressions.pathname).toBe('/api/reply-learning/expressions')
    expect(Object.fromEntries(expressions.searchParams)).toEqual({ ...scope, limit: '25', offset: '50', status: 'pending' })
    expect(feedback.pathname).toBe('/api/reply-learning/feedback')
    expect(Object.fromEntries(feedback.searchParams)).toEqual({ ...scope, limit: '50', offset: '100' })
    expect(calls[0][1]?.signal).toBe(controller.signal)
  })

  it.each(['approve', 'reject'] as const)('sends %s with revision, stable operation key and explicit style-only flag', async (action) => {
    await reviewExpression(item, scope, action, { idempotencyKey: 'review-key', styleOnly: action === 'approve' })
    const [path, init] = vi.mocked(fetchJson).mock.calls[0]
    expect(path).toBe('/api/reply-learning/expressions/7/review')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toEqual({
      scope: { kind: 'RuntimeScope', payload: {
        bot_id: 'bot-a', visibility: 'group',
        session: { id: scope.session_id, platform_id: 'qq', kind: 'group', conversation_id: '42:topic' },
        subject_principal_id: null,
      } },
      action, revision: 3, idempotency_key: 'review-key', style_only: action === 'approve', note: '',
    })
  })

  it('does not send approval without attestation or allow a foreign candidate', () => {
    expect(() => reviewExpression(item, scope, 'approve', { idempotencyKey: 'a', styleOnly: false })).toThrow('只学习表达方式')
    expect(() => reviewExpression({ ...item, bot_id: 'bot-b' }, scope, 'reject', { idempotencyKey: 'b', styleOnly: false })).toThrow('不属于当前 Bot')
    expect(() => reviewExpression({ ...item, revision: 0 }, scope, 'reject', { idempotencyKey: 'b', styleOnly: false })).toThrow('审核版本')
    expect(fetchJson).not.toHaveBeenCalled()
  })

  it('does not request unscoped or private data', () => {
    expect(() => listExpressionCandidates({ ...scope, bot_id: '', limit: 25, offset: 0 })).toThrow('Bot 和群聊')
    expect(() => listReplyFeedback({ ...scope, session_id: 'qq:private:42', limit: 25, offset: 0 })).toThrow('Bot 和群聊')
    expect(fetchJson).not.toHaveBeenCalled()
  })

  it.each([
    ['stale_revision', '该表达已被其他操作更新'],
    ['idempotency_conflict', '审核请求与已处理的操作冲突'],
    ['evidence_unavailable_or_unsafe', '来源证据已变化'],
    ['store_unavailable', '数据暂时读取失败'],
  ])('localizes backend error %s from its envelope', (code, message) => {
    const reason = Object.assign(new Error('HTTP request failed'), { payload: { error: { code } }, status: 409 })
    expect(replyLearningErrorMessage(reason, '读取失败')).toContain(message)
  })
})

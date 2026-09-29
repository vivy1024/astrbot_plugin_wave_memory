import { useLayoutEffect } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchJson } from '@/api/client'
import type { ExpressionCandidate, ReplyFeedbackEvent, ReplyLearningScope } from '@/api/replyLearning'
import { useGlobalScope } from '@/app/global-scope'
import { setViewport } from '@/test/setup'
import { ReplyLearningPanel } from './ReplyLearningPanel'

// Exercise the real API adapter and controls; the only boundary replaced is HTTP.
vi.mock('@/api/client', () => ({ fetchJson: vi.fn() }))

const scope: ReplyLearningScope = { bot_id: 'bot-a', session_id: 'qq:group:42:topic', visibility: 'group' }
const secondScope: ReplyLearningScope = { ...scope, session_id: 'qq:group:other' }
const attestation = '确认只学习表达方式，不学习说话人的身份或经历'

function candidate(changes: Partial<ExpressionCandidate> = {}): ExpressionCandidate {
  return {
    ...scope, id: 7, content: '慢慢来，一步一步走', situation: 'encourage', status: 'pending', revision: 3,
    speaker_id: 'visitor-a', source_memory_id: 11, source_hash: 'hash', reviewer: '', review_note: '',
    reviewed_at: null, created_at: 100, evidence_available: true,
    scope: { bot_id: scope.bot_id, visibility: 'group', session: { id: scope.session_id, platform_id: 'qq', kind: 'group', conversation_id: '42:topic' }, subject_principal_id: null },
    artifact: {}, evidence_refs: [], evidence_bindings: [], ...changes,
  }
}

function feedback(changes: Partial<ReplyFeedbackEvent> = {}): ReplyFeedbackEvent {
  return { ...scope, id: 19, reply_memory_id: 12, response_memory_id: 13, speaker_id: 'visitor-b',
    outcome: 'accepted', situation: 'encourage', source_quote: '好，我试试', created_at: 101,
    evidence_available: true, artifacts: [], ...changes }
}

function page<T>(items: T[], enabled = true, offset = 0, total = items.length) {
  return { items, enabled, scope: candidate().scope, signals: { accepted: 0, corrected: 0, rejected: 0, unknown: 0 },
    page: { total, total_status: 'exact', reason_code: null, limit: 25, offset,
      page: offset / 25 + 1, page_count: Math.max(1, Math.ceil(total / 25)), has_more: offset + 25 < total } }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function apiError(status: number, code: string) {
  return Object.assign(new Error('HTTP request failed'), { status, payload: { error: { code } } })
}

let rows: ExpressionCandidate[]
let feedbackRows: ReplyFeedbackEvent[]
let expressionEnabled: boolean
let feedbackEnabled: boolean
let reviewFailure: unknown

function serve(path: string, init?: RequestInit): unknown {
  const url = new URL(path, 'http://localhost')
  if (init?.method === 'POST') {
    if (reviewFailure) return Promise.reject(reviewFailure)
    const body = JSON.parse(init.body as string)
    const id = Number(url.pathname.split('/').at(-2))
    const item = rows.find((row) => row.id === id)!
    const updated: ExpressionCandidate = { ...item, status: body.action === 'approve' ? 'approved' : 'rejected', revision: item.revision + 1 }
    rows = rows.map((row) => row.id === id ? updated : row)
    return Promise.resolve({ ok: true, item: updated })
  }
  const owned = (row: ReplyLearningScope) => row.bot_id === url.searchParams.get('bot_id') && row.session_id === url.searchParams.get('session_id') && row.visibility === url.searchParams.get('visibility')
  if (url.pathname.endsWith('/expressions')) return Promise.resolve(page(rows.filter((row) => owned(row) && row.status === url.searchParams.get('status')), expressionEnabled))
  if (url.pathname.endsWith('/feedback')) return Promise.resolve(page(feedbackRows.filter(owned), feedbackEnabled))
  throw new Error(`Unexpected HTTP call: ${path}`)
}

const snapshots: Array<{ session: string; text: string }> = []
function Harness() {
  const global = useGlobalScope()
  // Layout effects capture the scope-change commit before passive request effects.
  useLayoutEffect(() => { snapshots.push({ session: global.sessionId, text: document.body.textContent ?? '' }) }, [global.botId, global.sessionId])
  return <>
    <button onClick={() => global.setScope({ sessionId: secondScope.session_id })}>切换群</button>
    <button onClick={() => global.setScope({ botId: 'bot-b', sessionId: scope.session_id })}>切换 Bot</button>
    <button onClick={() => global.setScope({ sessionId: 'qq:private:person' })}>切换私聊</button>
    <ReplyLearningPanel />
  </>
}

function show(query = new URLSearchParams(scope).toString()) {
  return render(<MemoryRouter initialEntries={[`/few-shot?${query}`]}><Harness /></MemoryRouter>)
}

function writes() { return vi.mocked(fetchJson).mock.calls.filter(([, init]) => init?.method === 'POST') }
function reads(suffix: string) { return vi.mocked(fetchJson).mock.calls.filter(([path, init]) => init?.method !== 'POST' && new URL(path, 'http://localhost').pathname.endsWith(suffix)) }
async function approve() {
  fireEvent.click(await screen.findByRole('checkbox', { name: attestation }))
  fireEvent.click(screen.getByRole('button', { name: '批准表达' }))
}

beforeEach(() => {
  setViewport(1280)
  rows = [candidate()]
  feedbackRows = [feedback()]
  expressionEnabled = true
  feedbackEnabled = true
  reviewFailure = undefined
  snapshots.length = 0
  vi.mocked(fetchJson).mockReset().mockImplementation(serve as typeof fetchJson)
})

describe('表达审核交互与真实请求契约', () => {
  it.each([1280, 390])('%ipx 下必须确认 style-only，批准后能查看并撤销已批准表达', async (width) => {
    setViewport(width)
    show()
    expect(await screen.findByRole('button', { name: '批准表达' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '批准表达' }))
    expect(writes()).toHaveLength(0)
    await approve()
    expect(await screen.findByText('当前群暂无待审表达')).toBeVisible()
    const [path, init] = writes()[0]
    expect(path).toBe('/api/reply-learning/expressions/7/review')
    const body = JSON.parse(init!.body as string)
    expect(body).toEqual({ action: 'approve', style_only: true, revision: 3, idempotency_key: expect.any(String), note: '',
      scope: { kind: 'RuntimeScope', payload: candidate().scope } })
    expect(body.idempotency_key.length).toBeGreaterThan(0)
    expect(screen.getByText(/表达 #7 当前为已批准/)).toHaveTextContent('可按适用情景召回')
    fireEvent.change(screen.getByRole('combobox', { name: '表达状态' }), { target: { value: 'approved' } })
    expect(await screen.findByText(candidate().content)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '撤销批准' }))
    expect(await screen.findByText('当前群暂无已批准表达')).toBeVisible()
    expect(JSON.parse(writes()[1][1]!.body as string)).toMatchObject({ action: 'reject', style_only: false, revision: 4 })
    fireEvent.change(screen.getByRole('combobox', { name: '表达状态' }), { target: { value: 'rejected' } })
    expect(await screen.findByText(candidate().content)).toBeVisible()
    expect(screen.queryByRole('button', { name: /批准表达|撤销批准/ })).not.toBeInTheDocument()
  })

  it.each([false, true])('表达开关为 %s 时准确说明召回条件，反馈开关独立', async (enabled) => {
    expressionEnabled = enabled
    feedbackEnabled = !enabled
    show()
    expect(await screen.findByText(enabled ? /表达学习已开启/ : /表达学习已关闭/)).toBeVisible()
    expect(screen.getByText(enabled ? /回复反馈采集已关闭/ : /回复反馈采集已开启/)).toBeVisible()
    await approve()
    expect(await screen.findByText('当前群暂无待审表达')).toBeVisible()
    expect(screen.getByText(/表达 #7 当前为已批准/)).toHaveTextContent('表达学习开启且来源证据有效时')
    expect(writes()).toHaveLength(1)
  })

  it('证据不可用时无法确认或批准，但仍能拒绝', async () => {
    rows = [candidate({ evidence_available: false })]
    show()
    expect(await screen.findByText(/来源证据不可用，不能批准或召回/)).toBeVisible()
    expect(screen.getByRole('checkbox', { name: attestation })).toBeDisabled()
    expect(screen.getByRole('button', { name: '批准表达' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '批准表达' }))
    expect(writes()).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '拒绝表达' }))
    expect(await screen.findByText('当前群暂无待审表达')).toBeVisible()
    expect(JSON.parse(writes()[0][1]!.body as string)).toMatchObject({ action: 'reject', style_only: false })
  })

  it.each([['stale_revision', 409], ['evidence_unavailable_or_unsafe', 422]] as const)('%s 后刷新候选，清除旧确认并使用新 revision', async (code, status) => {
    show()
    await screen.findByText(candidate().content)
    reviewFailure = apiError(status, code)
    rows = [candidate({ revision: 4, evidence_available: status !== 422 })]
    await approve()
    await waitFor(() => expect(reads('/expressions')).toHaveLength(2))
    expect(await screen.findByRole('checkbox', { name: attestation })).not.toBeChecked()
    expect(screen.getByRole('button', { name: '批准表达' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(status === 409 ? '该表达已被其他操作更新' : '来源证据已变化')
    reviewFailure = undefined
    if (status === 409) {
      await approve()
      await screen.findByText('当前群暂无待审表达')
      const first = JSON.parse(writes()[0][1]!.body as string)
      const second = JSON.parse(writes()[1][1]!.body as string)
      expect(second.revision).toBe(4)
      expect(second.idempotency_key).not.toBe(first.idempotency_key)
    } else expect(screen.getByRole('checkbox', { name: attestation })).toBeDisabled()
  })

  it('网络失败重试复用幂等键，提交期间重复点击只发送一次', async () => {
    const request = deferred<unknown>()
    vi.mocked(fetchJson).mockImplementation(((path, init) => init?.method === 'POST' ? request.promise : serve(path, init)) as typeof fetchJson)
    show()
    await approve()
    expect(screen.getByRole('button', { name: '批准表达' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '批准表达' }))
    expect(writes()).toHaveLength(1)
    await act(async () => request.reject(new Error('网络中断')))
    expect(screen.getByRole('alert')).toHaveTextContent('网络中断')
    expect(reads('/expressions')).toHaveLength(1)
    vi.mocked(fetchJson).mockImplementation(serve as typeof fetchJson)
    fireEvent.click(screen.getByRole('button', { name: '批准表达' }))
    await screen.findByText('当前群暂无待审表达')
    expect(writes()[1][1]!.body).toBe(writes()[0][1]!.body)
  })

  it.each(['切换群', '切换 Bot'])('%s 的首次提交不闪旧群内容、反馈或审核确认，不向旧群写入', async (control) => {
    const next: ReplyLearningScope = control === '切换群' ? secondScope : { ...scope, bot_id: 'bot-b' }
    rows.push(candidate({ ...next, id: 8, content: '新作用域表达' }))
    feedbackRows.push(feedback({ ...next, id: 20, source_quote: '新作用域反馈' }))
    const pending = deferred<ReturnType<typeof page<ExpressionCandidate>>>()
    vi.mocked(fetchJson).mockImplementation(((path, init) => {
      const url = new URL(path, 'http://localhost')
      if (init?.method !== 'POST' && url.pathname.endsWith('/expressions') && url.searchParams.get('bot_id') === next.bot_id && url.searchParams.get('session_id') === next.session_id) return pending.promise
      return serve(path, init)
    }) as typeof fetchJson)
    show()
    fireEvent.click(await screen.findByRole('checkbox', { name: attestation }))
    fireEvent.click(screen.getByRole('button', { name: control }))
    expect(snapshots.at(-1)!.text).not.toContain(candidate().content)
    expect(snapshots.at(-1)!.text).not.toContain(feedback().source_quote)
    expect(screen.queryByRole('button', { name: '批准表达' })).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
    expect(reads('/expressions')[0][1]!.signal!.aborted).toBe(true)
    await act(async () => pending.resolve(page([rows[1]])))
    expect(screen.getByRole('checkbox', { name: attestation })).not.toBeChecked()
    await approve()
    const body = JSON.parse(writes()[0][1]!.body as string)
    expect(body.scope.payload.bot_id).toBe(next.bot_id)
    expect(body.scope.payload.session.id).toBe(next.session_id)
    expect(writes()[0][0]).toContain('/8/review')
  })

  it('旧群列表即使忽略 abort 晚到，也不能覆盖新群', async () => {
    const oldExpressions = deferred<unknown>()
    const oldFeedback = deferred<unknown>()
    vi.mocked(fetchJson).mockImplementation(((path, init) => {
      const url = new URL(path, 'http://localhost')
      if (url.searchParams.get('session_id') === scope.session_id) return url.pathname.endsWith('/expressions') ? oldExpressions.promise : oldFeedback.promise
      return serve(path, init)
    }) as typeof fetchJson)
    rows.push(candidate({ ...secondScope, id: 8, content: '新群表达' }))
    show()
    fireEvent.click(screen.getByRole('button', { name: '切换群' }))
    await screen.findByText('新群表达')
    await act(async () => { oldExpressions.resolve(page([candidate()])); oldFeedback.resolve(page([feedback()])) })
    expect(screen.queryByText(candidate().content)).not.toBeInTheDocument()
    expect(screen.queryByText(feedback().source_quote)).not.toBeInTheDocument()
    expect(screen.getByText('新群表达')).toBeVisible()
  })

  it.each([true, false])('旧群审核晚到（成功 %s）不在新群提示或触发刷新', async (success) => {
    const pending = deferred<unknown>()
    vi.mocked(fetchJson).mockImplementation(((path, init) => init?.method === 'POST' ? pending.promise : serve(path, init)) as typeof fetchJson)
    rows.push(candidate({ ...secondScope, id: 8, content: '新群表达' }))
    show()
    await approve()
    fireEvent.click(screen.getByRole('button', { name: '切换群' }))
    await screen.findByText('新群表达')
    await act(async () => { if (success) pending.resolve({ ok: true, item: candidate({ status: 'approved', revision: 4 }) }); else pending.reject(apiError(409, 'stale_revision')) })
    expect(reads('/expressions')).toHaveLength(2)
    expect(screen.queryByText(/表达 #7 当前为|该表达已被其他操作更新/)).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: attestation })).not.toBeChecked()
    expect(writes()).toHaveLength(1)
  })

  it('缺少作用域或切到私聊时不读写表达接口', async () => {
    const view = show('')
    expect(screen.getByText(/请选择有效的 Bot 和群/)).toBeVisible()
    expect(fetchJson).not.toHaveBeenCalled()
    view.unmount()
    show()
    await screen.findByText(candidate().content)
    const count = vi.mocked(fetchJson).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '切换私聊' }))
    expect(screen.getByText(/请选择有效的 Bot 和群/)).toBeVisible()
    expect(vi.mocked(fetchJson).mock.calls).toHaveLength(count)
    expect(screen.queryByRole('checkbox', { name: attestation })).not.toBeInTheDocument()
  })

  it('读取失败可重试，证据缺失的反馈不被标成已确认效果', async () => {
    feedbackRows = [feedback({ evidence_available: false, outcome: 'unknown', source_quote: '', response_memory_id: 0 })]
    vi.mocked(fetchJson).mockRejectedValueOnce(apiError(503, 'store_unavailable'))
    show()
    expect(await screen.findByText(/数据暂时读取失败/)).toBeVisible()
    expect(screen.getByText('没有可引用的回应原话，效果未知。')).toBeVisible()
    expect(screen.getByText(/此记录不代表已确认的回复效果/)).toBeVisible()
    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: '重试' }))
    expect(await screen.findByText(candidate().content)).toBeVisible()
    expect(writes()).toHaveLength(0)
  })
})

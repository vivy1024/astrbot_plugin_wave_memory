import { useLayoutEffect } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'

import { fetchJson } from '@/api/client'
import { reviewFact, type FactConflictCandidate, type FactSelection, type ScopedFactItem } from '@/api/facts'
import { useGlobalScope } from '@/app/global-scope'
import { setViewport } from '@/test/setup'
import { FactsPage } from './FactsPage'

vi.mock('@/api/client', () => ({ fetchJson: vi.fn() }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
vi.mock('@/api/options', async (original) => ({
  ...await original<typeof import('@/api/options')>(),
  getScopeOptions: vi.fn(async () => ({ bots: [], sessions: [], channels: [], generated_at: 1, source: { health: 'healthy', reason_code: null } })),
}))

const scope: FactSelection = { bot_id: 'bot-a', session_id: 'qq:group:42:topic', visibility: 'group' }
const oldFact: FactConflictCandidate = { id: 2, revision: 7, subject: '林舟', predicate: '住在', object: '北港', status: 'active', valid_from: null, valid_until: null }
function fact(changes: Partial<ScopedFactItem> = {}): ScopedFactItem {
  return { ...scope, id: 8, subject: '林舟', predicate: '住在', object: '南湾', confidence: 0.9, status: 'conflict', revision: 3,
    valid_from: null, valid_until: null, effective: false, ineffective_reason: 'conflict',
    review_actions: ['approve', 'reject', 'replace', 'coexist'], conflict_candidates: [oldFact],
    evidence: [], evidence_status: 'unavailable', review_hint: 'needs_review', object_ref: { ref: 'signed-fact-8', version: 3 },
    capabilities: { review: { available: true, reason_code: null }, batch_review: { available: true, reason_code: null } }, editable: true, ...changes }
}

function page(items: ScopedFactItem[]) {
  return { items, scope, capabilities: {}, page: { total: items.length, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false } }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((yes) => { resolve = yes })
  return { promise, resolve }
}

let rows: ScopedFactItem[]
let nextItem: ScopedFactItem
let failure: unknown
let scopeCommit: string
function serve(path: string, init?: RequestInit) {
  if (init?.method === 'POST') return failure ? Promise.reject(failure) : Promise.resolve({ ok: true, operation: { kind: 'fact.review', status: 'succeeded' }, revision: 4, item: nextItem })
  const url = new URL(path, 'http://localhost')
  return Promise.resolve(page(rows.filter((row) => row.bot_id === url.searchParams.get('bot_id') && row.session_id === url.searchParams.get('session_id'))))
}

function Harness() {
  const global = useGlobalScope()
  useLayoutEffect(() => { scopeCommit = document.body.textContent ?? '' }, [global.sessionId])
  return <><button onClick={() => global.setScope({ sessionId: 'qq:group:other' })}>切换群</button><FactsPage /></>
}

function show() { return render(<MemoryRouter initialEntries={[`/facts?${new URLSearchParams(scope)}`]}><Harness /></MemoryRouter>) }
function writes() { return vi.mocked(fetchJson).mock.calls.filter(([, init]) => init?.method === 'POST') }
async function detail(width = 1280) {
  fireEvent.click(await screen.findByRole('button', { name: width < 768 ? '查看详情' : '查看事实 8 详情' }))
  return within(await screen.findByRole('dialog'))
}

beforeEach(() => {
  setViewport(1280)
  rows = [fact()]
  nextItem = fact({ status: 'active', effective: true, ineffective_reason: null, revision: 4, review_actions: [] })
  failure = undefined
  scopeCommit = ''
  vi.mocked(fetchJson).mockReset().mockImplementation(serve as typeof fetchJson)
})

describe('事实审核按钮到后端 wire contract', () => {
  it.each([1280, 390])('%ipx 替换必须选择旧事实，并发送双方 revision 与完整会话', async (width) => {
    setViewport(width)
    show()
    const dialog = await detail(width)
    expect(dialog.getByText('未生效：冲突待裁决')).toBeVisible()
    expect(dialog.getByRole('button', { name: '批准并替换' })).toBeDisabled()
    fireEvent.click(dialog.getByRole('button', { name: '批准并替换' }))
    expect(writes()).toHaveLength(0)
    fireEvent.change(dialog.getByRole('combobox', { name: '替换的旧事实' }), { target: { value: '2' } })
    fireEvent.click(dialog.getByRole('button', { name: '批准并替换' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    const [path, init] = writes()[0]
    expect(path).toBe('/api/facts/8/replace')
    expect(JSON.parse(init!.body as string)).toEqual({
      scope: { kind: 'RuntimeScope', payload: { bot_id: 'bot-a', visibility: 'group', subject_principal_id: null,
        session: { id: scope.session_id, platform_id: 'qq', kind: 'group', conversation_id: '42:topic' } } },
      object_ref: { ref: 'signed-fact-8', version: 3 }, revision: 3, reason: '', supersedes_fact_id: 2, supersedes_revision: 7,
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(toast.success).toHaveBeenCalledWith('已替换事实')
  })

  it('共存不携带替换目标，也不更改旧事实', async () => {
    show()
    const dialog = await detail()
    fireEvent.click(dialog.getByRole('button', { name: '批准共存' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes()[0][0]).toBe('/api/facts/8/coexist')
    const body = JSON.parse(writes()[0][1]!.body as string)
    expect(body.revision).toBe(3)
    expect(body).not.toHaveProperty('supersedes_fact_id')
    expect(body).not.toHaveProperty('supersedes_revision')
  })

  it('服务端仅允许拒绝时，列表、详情与批量入口都不允许批准', async () => {
    rows = [fact({ review_actions: ['reject'] })]
    show()
    await screen.findByText('南湾')
    expect(screen.queryByRole('button', { name: '批准事实 8' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: '选择事实 8' }))
    expect(screen.getByRole('button', { name: '批量批准' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '批量拒绝' })).toBeEnabled()
    const dialog = await detail()
    expect(dialog.queryByRole('button', { name: /批准/ })).not.toBeInTheDocument()
    expect(dialog.getByRole('button', { name: '拒绝' })).toBeEnabled()
    expect(writes()).toHaveLength(0)
  })

  it('批准返回 conflict 时准确提示，并保留替换/共存入口', async () => {
    nextItem = fact({ revision: 4 })
    show()
    fireEvent.click(await screen.findByRole('button', { name: '批准事实 8' }))
    await waitFor(() => expect(toast.warning).toHaveBeenCalledWith('事实仍有冲突，请查看详情选择共存或替换'))
    expect(toast.success).not.toHaveBeenCalled()
    const dialog = await detail()
    expect(dialog.getByRole('button', { name: '批准共存' })).toBeEnabled()
    expect(dialog.getByRole('combobox', { name: '替换的旧事实' })).toBeEnabled()
  })

  it('替换 409 后关闭旧详情并刷新目标版本，重开后必须重新选择', async () => {
    show()
    const dialog = await detail()
    fireEvent.change(dialog.getByRole('combobox', { name: '替换的旧事实' }), { target: { value: '2' } })
    failure = Object.assign(new Error('stale_revision'), { status: 409 })
    rows = [fact({ revision: 4, conflict_candidates: [{ ...oldFact, revision: 8 }] })]
    fireEvent.click(dialog.getByRole('button', { name: '批准并替换' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    const fresh = await detail()
    expect(fresh.getByRole('combobox', { name: '替换的旧事实' })).toHaveValue('')
    expect(fresh.getByRole('button', { name: '批准并替换' })).toBeDisabled()
    failure = undefined
    fireEvent.change(fresh.getByRole('combobox', { name: '替换的旧事实' }), { target: { value: '2' } })
    fireEvent.click(fresh.getByRole('button', { name: '批准并替换' }))
    await waitFor(() => expect(writes()).toHaveLength(2))
    expect(JSON.parse(writes()[1][1]!.body as string)).toMatchObject({ revision: 4, supersedes_fact_id: 2, supersedes_revision: 8 })
  })

  it('详情打开时切群不残留旧事实，旧群审核返回不刷新新群', async () => {
    const pending = deferred<unknown>()
    vi.mocked(fetchJson).mockImplementation(((path, init) => init?.method === 'POST' ? pending.promise : serve(path, init)) as typeof fetchJson)
    rows.push(fact({ id: 10, object: '新群地点', session_id: 'qq:group:other' }))
    show()
    const dialog = await detail()
    fireEvent.click(dialog.getByRole('button', { name: '批准事实' }))
    fireEvent.click(screen.getByText('切换群'))
    expect(scopeCommit).not.toContain('南湾')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await screen.findByText('新群地点')
    await act(async () => pending.resolve({ ok: true, item: nextItem }))
    expect(toast.success).not.toHaveBeenCalled()
    expect(writes()).toHaveLength(1)
    expect(vi.mocked(fetchJson).mock.calls.filter(([, init]) => init?.method !== 'POST')).toHaveLength(2)
    expect(screen.getByText('新群地点')).toBeVisible()
  })

  it('API 拒绝缺失/过期替换目标和跨作用域事实，不发送请求', () => {
    expect(() => reviewFact(fact(), 'replace', scope)).toThrow('替换目标及版本')
    expect(() => reviewFact(fact(), 'replace', scope, '', { ...oldFact, revision: 6 })).toThrow('替换目标及版本')
    expect(() => reviewFact(fact(), 'replace', scope, '', { ...oldFact, id: 99 })).toThrow('替换目标及版本')
    expect(() => reviewFact(fact(), 'approve', { ...scope, bot_id: 'other' })).toThrow('不属于当前 Bot')
    expect(() => reviewFact(fact({ review_actions: ['reject'] }), 'approve', scope)).toThrow('未允许')
    expect(fetchJson).not.toHaveBeenCalled()
  })
})

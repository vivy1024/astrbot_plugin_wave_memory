import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setViewport } from '@/test/setup'
import { InjectionPage } from './InjectionPage'

const api = vi.hoisted(() => ({
  traces: vi.fn(),
  detail: vi.fn(),
  scopes: vi.fn(),
}))

vi.mock('@/api/injection', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/injection')>()
  return { ...actual, listInjectionTraces: api.traces, getInjectionTrace: api.detail }
})
vi.mock('@/api/options', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/options')>()
  return { ...actual, getScopeOptions: api.scopes }
})

function page(preview: string) {
  return {
    items: [{ trace_id: preview, timestamp: 1, status: 'ok', preview }],
    page: { total: 1, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

beforeEach(() => {
  api.detail.mockResolvedValue({ trace_id: 'trace' })
  api.scopes.mockResolvedValue({
    bots: [{ db_id: 'bot-a', name: 'Bot A', status: 'active' }],
    sessions: [{ id: 'session-a', bot_id: 'bot-a', platform_id: 'qq', kind: 'group', conversation_id: '1', label: '群 1' }],
    channels: [{ id: 'timeline', enabled: true }],
    generated_at: 1,
    source: { health: 'healthy', reason_code: null },
  })
})

describe('InjectionPage 异步筛选', () => {
  it('草稿查询会取消旧请求，旧响应不会覆盖新结果', async () => {
    const first = deferred<ReturnType<typeof page>>()
    const second = deferred<ReturnType<typeof page>>()
    api.traces.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const user = userEvent.setup()

    render(<MemoryRouter initialEntries={['/observatory?bot_id=bot-a&session_id=session-a&visibility=group']}><InjectionPage /></MemoryRouter>)
    await waitFor(() => expect(api.traces).toHaveBeenCalledTimes(1))

    await user.type(screen.getByLabelText('发送者 ID'), 'new-sender')
    expect(api.traces).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '查询' }))
    await waitFor(() => expect(api.traces).toHaveBeenCalledTimes(2))

    second.resolve(page('new-result'))
    expect((await screen.findAllByText('new-result')).length).toBeGreaterThan(0)
    first.resolve(page('old-result'))
    await waitFor(() => expect(screen.queryByText('old-result')).not.toBeInTheDocument())
  })

  it('清除私有筛选保留 canonical Scope，并只提交一次规范查询', async () => {
    api.traces.mockResolvedValue(page('result'))
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/observatory?bot_id=bot-a&session_id=session-a&visibility=group&sender_id=sender-a&offset=25']}><InjectionPage /></MemoryRouter>)
    await waitFor(() => expect(api.traces).toHaveBeenCalledTimes(1))

    await user.click(screen.getByRole('button', { name: '清除筛选' }))
    await waitFor(() => expect(api.traces).toHaveBeenCalledTimes(2))
    const filters = api.traces.mock.calls[1][0]
    expect(filters).toMatchObject({ bot_id: 'bot-a', session_id: 'session-a', offset: 0 })
    expect(filters.sender_id).toBeUndefined()
    expect(screen.getByLabelText('发送者 ID')).toHaveValue('')
  })

  it('只读取 Trace API，不展示反馈记录或旧群组筛选控件', async () => {
    api.traces.mockResolvedValue(page('trace-only'))
    render(<MemoryRouter initialEntries={['/observatory?bot_id=bot-a&session_id=session-a&group_id=ignored-group']}><InjectionPage /></MemoryRouter>)

    expect(await screen.findAllByText('trace-only')).not.toHaveLength(0)
    expect(screen.queryByText('反馈记录')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('群 / 会话 ID')).not.toBeInTheDocument()
    expect(api.traces.mock.calls[0][0]).not.toHaveProperty('group_id')
  })

  it('窄屏将 Trace 摘要改为完整卡片而非裁切宽表', async () => {
    setViewport(390)
    api.traces.mockResolvedValue(page('mobile-trace'))
    const view = render(<MemoryRouter initialEntries={['/observatory?bot_id=bot-a&session_id=session-a&visibility=group']}><InjectionPage /></MemoryRouter>)

    expect((await screen.findAllByText('mobile-trace')).length).toBeGreaterThan(0)
    await waitFor(() => expect(view.container.querySelector('[data-responsive-table="cards"]')).toBeInTheDocument())
    expect(view.container.querySelector('[data-responsive-table="cards"] table')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看注入详情' })).toBeVisible()
  })
})

import { MemoryRouter, useLocation } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setViewport } from '@/test/setup'
import { MemoriesPage } from './MemoriesPage'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  senders: vi.fn(),
  scopes: vi.fn(),
  detail: vi.fn(),
  tagState: vi.fn(),
  similar: vi.fn(),
  context: vi.fn(),
  traces: vi.fn(),
}))

vi.mock('@/api/memories', () => ({
  listMemories: api.list,
  listSenders: api.senders,
  getMemoryDetail: api.detail,
  getSimilarMemories: api.similar,
  getMemoryContext: api.context,
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  getMemoryTagState: api.tagState,
  correctMemoryTags: vi.fn(),
  undoMemoryTagCorrection: vi.fn(),
  batchDeleteMemories: vi.fn(),
  memoryBatchStreamUrl: vi.fn(() => '/api/memories/batch/re-embed'),
  reEmbedMemory: vi.fn(),
  runPostStream: vi.fn(),
}))
vi.mock('@/api/options', () => ({
  getScopeOptions: api.scopes,
  scopeOptionsFor: (_payload: unknown, kinds: string[]) => kinds.includes('bot')
    ? [{ value: 'bot-real', label: '真实 Bot', kind: 'bot', description: 'Bot ID bot-real' }]
    : [{ value: 'qq:group:42', label: '群 42', kind: 'session', description: 'bot-real · group · runtime' }],
}))
vi.mock('@/api/injection', () => ({ listMemoryTraces: api.traces }))
vi.mock('@/components/tag/TagExtractionConfigPanel', () => ({ TagExtractionConfigPanel: () => <div>标签提取配置</div> }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), info: vi.fn(), success: vi.fn(), message: vi.fn() } }))

const page = { total: 10, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false }

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset())
  api.scopes.mockResolvedValue({ bots: [], sessions: [], channels: [], generated_at: 1, source: { health: 'healthy', reason_code: null } })
  api.list.mockResolvedValue({ items: [], page })
  api.senders.mockResolvedValue({ senders: [], source: { status: 'available', reason_code: null } })
})

describe('MemoriesPage 筛选草稿', () => {
  it('来源选择不会逐项请求，提交时带入筛选并重置 offset', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group&offset=25']}><MemoriesPage /></MemoryRouter>)

    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1))
    const sourceSelect = screen.getAllByRole('combobox')[2]
    await user.click(sourceSelect)
    await user.click(screen.getByRole('option', { name: 'live' }))
    expect(api.list).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '搜索' }))
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))
    expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'live', offset: 0 }))
  })

  it('桌面保留记忆表格，窄屏显示完整卡片与详情入口', async () => {
    api.list.mockResolvedValue({ items: [{ ref: 'memory:1', id: 1, content: '移动端完整记忆正文', sender_name: 'Alice', source: 'live', tags: [{ name: '重要', type: 'topic' }], has_vector: true, timestamp: 1 }], page })
    const desktop = render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group']}><MemoriesPage /></MemoryRouter>)
    await waitFor(() => expect(desktop.container.querySelector('[data-responsive-table="table"] table')).toBeInTheDocument())
    desktop.unmount()

    setViewport(390)
    const mobile = render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group']}><MemoriesPage /></MemoryRouter>)
    expect(await screen.findByText('移动端完整记忆正文')).toBeVisible()
    await waitFor(() => expect(mobile.container.querySelector('[data-responsive-table="cards"]')).toBeInTheDocument())
    expect(mobile.container.querySelector('[data-responsive-table="cards"] table')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开详情' })).toBeVisible()
    expect(mobile.container.querySelector('[data-responsive-table="cards"]')).toHaveTextContent('有向量')
  })
})

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.search}</output>
}

const scopedMemory = {
  id: 397782,
  content: '深链打开的记忆原文',
  sender_id: 'u1',
  sender_name: 'Alice',
  group_id: '42',
  bot_id: 'bot-real',
  session_id: 'qq:group:42',
  visibility: 'group',
  source: 'core',
  timestamp: 1,
  has_vector: true,
  version: 3,
  ref: 'oref.a.b',
  detail_url: '/api/memories/397782?ref=oref.a.b&bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group',
  mutation_url: '/api/memories/397782?ref=oref.a.b&bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group',
}

describe('MemoriesPage memory_id 深链', () => {
  it('按编号定位并直接打开详情、上下文与「最近被这些回复用到」', async () => {
    api.list.mockImplementation(async (filters: { id?: number }) => filters.id ? { items: [scopedMemory], page } : { items: [], page })
    api.detail.mockResolvedValue({ item: scopedMemory })
    api.tagState.mockResolvedValue({ item: { automatic: [], effective: [], manual: null } })
    api.similar.mockResolvedValue({ items: [] })
    api.context.mockResolvedValue({ memory_id: 397782, before: 5, after: 5, messages: [{ id: 397782, content: '深链打开的记忆原文', role: 'anchor' }] })
    api.traces.mockResolvedValue({ memory_id: 397782, bot_id: 'bot-real', count: 1, limit: 10, channels: ['memory'], items: [{ trace_id: 'trace-9', timestamp: 2, group_id: '42', channel: 'memory', score: 0.5, channels: [{ channel: 'memory', score: 0.5, rank: 1 }], message_preview: '触发回复的消息' }] })

    render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group&memory_id=397782']}><MemoriesPage /><LocationProbe /></MemoryRouter>)

    await waitFor(() => expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ id: 397782, bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' })))
    await waitFor(() => expect(api.detail).toHaveBeenCalledWith(scopedMemory.detail_url))
    const traceLink = await screen.findByRole('link', { name: /触发回复的消息/ })
    expect(traceLink).toHaveAttribute('href', '/observatory?bot_id=bot-real&trace_id=trace-9')
    expect(api.traces).toHaveBeenCalledWith(397782, 'bot-real', 10, expect.any(AbortSignal))
    expect(api.context).toHaveBeenCalled()
    await waitFor(() => {
      const search = new URLSearchParams(screen.getByTestId('location').textContent ?? '')
      expect(search.get('memory_id')).toBeNull()
      expect(search.get('ref')).toBe('oref.a.b')
      expect(search.get('object_id')).toBe('397782')
    })
  })

  it('找到但没有签发 ref 时只读展示原文，不用裸编号读详情', async () => {
    const legacy = { ...scopedMemory, ref: '', detail_url: '', mutation_url: '', version: null as unknown as number, content: '缺版本号的旧记忆' }
    api.list.mockImplementation(async (filters: { id?: number }) => filters.id ? { items: [legacy], page } : { items: [], page })
    api.traces.mockResolvedValue({ memory_id: 397782, bot_id: 'bot-real', count: 0, limit: 10, channels: ['memory'], items: [] })

    render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group&memory_id=397782']}><MemoriesPage /></MemoryRouter>)

    expect(await screen.findByText('缺版本号的旧记忆')).toBeInTheDocument()
    expect(document.querySelector('[data-slot="memory-unref-fallback"] [data-slot="memory-trace-usage"]')).toBeInTheDocument()
    expect(api.detail).not.toHaveBeenCalled()
  })

  it('当前群里找不到这条记忆时提示对象不存在', async () => {
    api.list.mockResolvedValue({ items: [], page })
    render(<MemoryRouter initialEntries={['/memories?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group&memory_id=5']}><MemoriesPage /></MemoryRouter>)
    expect(await screen.findByText('无法打开这条记忆')).toBeInTheDocument()
  })
})

import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MemoryContextSection, MemoryTraceUsageSection } from './MemoryProvenance'

const api = vi.hoisted(() => ({ traces: vi.fn(), context: vi.fn() }))

vi.mock('@/api/injection', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/injection')>()
  return { ...actual, listMemoryTraces: api.traces }
})
vi.mock('@/api/memories', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/memories')>()
  return { ...actual, getMemoryContext: api.context }
})

beforeEach(() => {
  api.traces.mockReset()
  api.context.mockReset()
})

describe('MemoryTraceUsageSection', () => {
  it('列出时间、群、通道、得分并跳回观测台 trace', async () => {
    api.traces.mockResolvedValue({
      memory_id: 397782,
      bot_id: 'yushu',
      count: 1,
      limit: 10,
      channels: ['memory', 'fts5'],
      items: [{
        trace_id: 'active-1790506362371167663',
        timestamp: 1790506362,
        group_id: '398291136',
        bot_profile_id: 'yushu',
        sender_name: '群友',
        message_preview: '羽书还记得上次的事吗',
        channel: 'memory',
        score: 0.9922896,
        channels: [{ channel: 'memory', score: 0.9922896, rank: 1 }, { channel: 'fts5', score: 1, rank: 3 }],
      }],
    })
    render(<MemoryRouter><MemoryTraceUsageSection memoryId={397782} botId="yushu" /></MemoryRouter>)

    const link = await screen.findByRole('link', { name: /羽书还记得上次的事吗/ })
    expect(link).toHaveAttribute('href', '/observatory?bot_id=yushu&trace_id=active-1790506362371167663')
    expect(link).toHaveTextContent('群 398291136')
    expect(link).toHaveTextContent('memory · 得分 0.992 · 第 1 条')
    expect(link).toHaveTextContent('fts5 · 得分 1.000 · 第 3 条')
    expect(api.traces).toHaveBeenCalledWith(397782, 'yushu', 10, expect.any(AbortSignal))
  })

  it('没有用到时显示空态，接口失败显示错误', async () => {
    api.traces.mockResolvedValueOnce({ memory_id: 1, bot_id: 'yushu', count: 0, limit: 10, channels: [], items: [] })
    const { unmount } = render(<MemoryRouter><MemoryTraceUsageSection memoryId={1} botId="yushu" /></MemoryRouter>)
    expect(await screen.findByText('观测台保留期内没有回复用到这条记忆。')).toBeInTheDocument()
    unmount()

    api.traces.mockRejectedValueOnce(new Error('boom'))
    render(<MemoryRouter><MemoryTraceUsageSection memoryId={1} botId="yushu" /></MemoryRouter>)
    await waitFor(() => expect(document.querySelector('[data-slot="memory-trace-usage"] .text-destructive')).toBeInTheDocument())
  })
})

describe('MemoryContextSection', () => {
  it('按 ref 读取前后消息并高亮本条', async () => {
    api.context.mockResolvedValue({
      memory_id: 5,
      before: 5,
      after: 5,
      messages: [
        { id: 3, content: '前一句', sender_name: 'A', role: 'before' },
        { id: 5, content: '锚点原文', sender_name: 'B', role: 'anchor' },
        { id: 6, content: '后一句', sender_name: 'C', role: 'after' },
      ],
    })
    render(<MemoryContextSection memory={{ id: 5, mutation_url: '/api/memories/5?ref=oref.x&bot_id=yushu' }} />)

    expect(await screen.findByText('锚点原文')).toBeInTheDocument()
    expect(document.querySelector('[data-role="anchor"]')).toHaveTextContent('本条')
    expect(document.querySelectorAll('[data-slot="memory-context"] [data-role]')).toHaveLength(3)
    expect(api.context).toHaveBeenCalledWith({ mutation_url: '/api/memories/5?ref=oref.x&bot_id=yushu' }, 5, 5, expect.any(AbortSignal))
  })
})

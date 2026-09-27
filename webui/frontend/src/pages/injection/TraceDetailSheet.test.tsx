import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { TraceDetailSheet } from './TraceDetailSheet'

vi.mock('@/api/options', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/options')>()
  return {
    ...actual,
    getScopeOptions: vi.fn().mockResolvedValue({
      bots: [],
      sessions: [{ id: '羽书:group:398291136', bot_id: 'yushu', platform_id: '羽书', kind: 'group', conversation_id: '398291136', label: '3-5层群', is_primary_alias: true }],
      channels: [],
      generated_at: 1,
      source: { health: 'healthy', reason_code: null },
    }),
  }
})

const detail = {
  trace_id: 'active-1',
  request: { bot_id: '2500447291', bot_profile_id: 'yushu', group_id: '398291136', sender_id: 'u1' },
  channels: [
    {
      channel: 'memory',
      status: 'ok',
      hit_items: [{ id: 397782, score: 0.99, preview: '一条群聊记忆', link: { kind: 'memory', memory_id: 397782, memory_scope: { bot_id: 'yushu', session_id: '羽书:group:150727649', visibility: 'group', group_id: '150727649' } } }],
      filtered_items: [],
    },
    {
      channel: 'jargon',
      status: 'ok',
      hit_items: [{ id: null, preview: 'v我50 → 请我吃饭', word: 'v我50', link: { word: 'v我50' } }],
      filtered_items: [],
    },
    { channel: 'persona', status: 'ok', hit_items: [{ id: null, preview: '<self_persona>' }], filtered_items: [] },
  ],
}

function hrefParams(href: string | null): URLSearchParams {
  return new URLSearchParams((href ?? '').split('?')[1] ?? '')
}

describe('TraceDetailSheet 回复溯源链接', () => {
  it('命中条目跳到对象页，通道名跳到通道配置', async () => {
    render(<MemoryRouter><TraceDetailSheet open detail={detail} loading={false} error="" onOpenChange={() => {}} /></MemoryRouter>)

    const memoryLink = await screen.findByRole('link', { name: /打开记忆 #397782/ })
    expect(memoryLink.getAttribute('href')?.startsWith('/memories?')).toBe(true)
    const memoryParams = hrefParams(memoryLink.getAttribute('href'))
    expect(memoryParams.get('memory_id')).toBe('397782')
    expect(memoryParams.get('session_id')).toBe('羽书:group:150727649')
    expect(memoryParams.get('bot_id')).toBe('yushu')

    // jargon 需要 trace 群号换成 canonical session，等 scope 选项加载后才带上 session_id
    await waitFor(() => {
      const jargonLink = screen.getByRole('link', { name: /查找黑话「v我50」/ })
      expect(hrefParams(jargonLink.getAttribute('href')).get('session_id')).toBe('羽书:group:398291136')
    })

    const channelLinks = document.querySelectorAll('[data-slot="trace-channel-link"]')
    expect(Array.from(channelLinks).map((node) => node.getAttribute('href'))).toEqual([
      '/channels?channel=memory',
      '/channels?channel=jargon',
      '/channels?channel=persona',
    ])
    // 人设通道没有对象页，不渲染条目链接
    expect(document.querySelectorAll('[data-slot="trace-item-link"]')).toHaveLength(2)
  })
})

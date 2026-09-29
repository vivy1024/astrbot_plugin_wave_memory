import { MemoryRouter } from 'react-router-dom'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ScopedFactItem, ScopedFactsResponse } from '@/api/facts'
import { setViewport } from '@/test/setup'
import { FactsPage } from './FactsPage'

const api = vi.hoisted(() => ({
  listFacts: vi.fn(),
  getScopeOptions: vi.fn(),
  approveFact: vi.fn(),
  rejectFact: vi.fn(),
  reviewFact: vi.fn(),
  batchReviewFacts: vi.fn(),
  getFactEvidence: vi.fn(),
}))

vi.mock('@/api/facts', () => api)
vi.mock('@/api/options', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/options')>(),
  getScopeOptions: api.getScopeOptions,
}))

const NOW = Date.parse('2026-09-29T04:00:00Z') / 1000

function fact(id: number, changes: Partial<ScopedFactItem> = {}): ScopedFactItem {
  return {
    id, bot_id: 'bot-a', session_id: 'qq:group:42', visibility: 'group',
    subject: `测试事实 ${id}`, predicate: '位于', object: '测试地点',
    confidence: 0.9, status: 'active', revision: 3,
    valid_from: null, valid_until: null,
    effective: true, ineffective_reason: null, review_actions: [], conflict_candidates: [],
    evidence: [], evidence_status: 'unavailable', review_hint: '',
    object_ref: { ref: `signed-fact-${id}`, version: 3 },
    capabilities: {
      review: { available: true, reason_code: null },
      batch_review: { available: true, reason_code: null },
    },
    editable: false,
    ...changes,
  }
}

function showFacts(items: ScopedFactItem[]) {
  const response: ScopedFactsResponse = {
    items,
    page: { total: items.length, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false },
    scope: { bot_id: 'bot-a', session_id: 'qq:group:42', visibility: 'group' },
    capabilities: {},
  }
  api.listFacts.mockResolvedValue(response)
  return render(<MemoryRouter initialEntries={['/facts?bot_id=bot-a&session_id=qq%3Agroup%3A42']}><FactsPage /></MemoryRouter>)
}

beforeEach(() => {
  setViewport(1280)
  vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
  api.getScopeOptions.mockResolvedValue({
    bots: [], sessions: [], channels: [], generated_at: 1,
    source: { health: 'healthy', reason_code: null },
  })
})

afterEach(() => vi.useRealTimers())

describe('事实有效期', () => {
  it.each([1280, 390])('在 %ipx 列表保留审核状态，单独提示过期与尚未生效', async (width) => {
    setViewport(width)
    showFacts([
      fact(1, { valid_until: NOW - 1 }),
      fact(2, { status: 'conflict', valid_from: NOW + 1 }),
      fact(3, { status: 'pending', valid_from: NOW - 1, valid_until: NOW + 1 }),
    ])

    await screen.findByText('测试事实 1')
    const row = (id: number) => within(screen.getByText(`测试事实 ${id}`).closest(width < 768 ? 'article' : 'tr') as HTMLElement)
    expect(row(1).getByText('active')).toBeVisible()
    expect(row(1).getByText('已过期')).toBeVisible()
    expect(row(2).getByText('conflict')).toBeVisible()
    expect(row(2).getByText('尚未生效')).toBeVisible()
    expect(row(3).getByText('pending')).toBeVisible()
    expect(row(3).queryByText(/已过期|尚未生效/)).not.toBeInTheDocument()
    expect(api.listFacts).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-a', session_id: 'qq:group:42', visibility: 'group' }))
  })

  it.each([
    { name: '到达开始时刻', valid_from: NOW, valid_until: NOW + 1, expired: false },
    { name: '到达截止时刻', valid_from: NOW - 1, valid_until: NOW, expired: true },
    { name: '没有时间限制', valid_from: null, valid_until: null, expired: false },
    { name: '截止为 Unix 零秒', valid_from: null, valid_until: 0, expired: true },
  ])('$name：遵循包含开始、不包含截止的区间', async ({ valid_from, valid_until, expired }) => {
    showFacts([fact(1, { valid_from, valid_until })])
    await screen.findByText('测试事实 1')
    expect(Boolean(screen.queryByText('已过期'))).toBe(expired)
    expect(screen.queryByText('尚未生效')).not.toBeInTheDocument()
    expect(screen.getByText('active')).toBeVisible()
  })

  it('详情按 Unix 秒显示开始与截止，并注明本机时区', async () => {
    showFacts([fact(1, { valid_from: 0, valid_until: NOW })])
    fireEvent.click(await screen.findByRole('button', { name: '查看事实 1 详情' }))

    const dialog = await screen.findByRole('dialog')
    const start = within(dialog).getByText('有效期开始').nextElementSibling
    const end = within(dialog).getByText('有效期截止').nextElementSibling
    expect(start?.querySelector('time')).toHaveAttribute('datetime', '1970-01-01T00:00:00.000Z')
    expect(end?.querySelector('time')).toHaveAttribute('datetime', '2026-09-29T04:00:00.000Z')
    expect(start).toHaveTextContent(new Date(0).toLocaleString('zh-CN'))
    expect(end).toHaveTextContent(new Date(NOW * 1000).toLocaleString('zh-CN'))
    expect(within(dialog).getByText('active')).toBeVisible()
    expect(within(dialog).getByText('已过期')).toBeVisible()
    expect(within(dialog).getByText(/时间按本机时区显示/)).toBeVisible()
  })

  it('详情将 null 显示为未限定，不编造开始与截止', async () => {
    showFacts([fact(1)])
    fireEvent.click(await screen.findByRole('button', { name: '查看事实 1 详情' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('未限定开始')).toBeVisible()
    expect(within(dialog).getByText('未限定截止')).toBeVisible()
    expect(dialog.querySelector('time')).toBeNull()
    expect(within(dialog).queryByText(/已过期|尚未生效/)).not.toBeInTheDocument()
  })

  it('停留页面时按本机时间更新提示，不重新请求事实或修改审核状态', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const view = showFacts([fact(1, { valid_from: NOW + 30, valid_until: NOW + 90 })])
    expect(await screen.findByText('尚未生效')).toBeVisible()

    vi.spyOn(Date, 'now').mockReturnValue((NOW + 60) * 1000)
    act(() => vi.advanceTimersByTime(60_000))
    expect(screen.queryByText('尚未生效')).not.toBeInTheDocument()
    expect(screen.queryByText('已过期')).not.toBeInTheDocument()

    vi.spyOn(Date, 'now').mockReturnValue((NOW + 120) * 1000)
    act(() => vi.advanceTimersByTime(60_000))
    expect(screen.getByText('已过期')).toBeVisible()
    expect(screen.getByText('active')).toBeVisible()
    expect(api.listFacts).toHaveBeenCalledTimes(1)
    expect(api.approveFact).not.toHaveBeenCalled()
    expect(api.rejectFact).not.toHaveBeenCalled()
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

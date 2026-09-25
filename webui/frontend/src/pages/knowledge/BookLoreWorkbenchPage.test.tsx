import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { BookLoreWorkbenchPage } from '@/pages/knowledge/BookLoreWorkbenchPage'

const api = vi.hoisted(() => ({
  getWorkbenchOverview: vi.fn(),
  listWorkbenchNotes: vi.fn(),
  getWorkbenchNote: vi.fn(),
  saveWorkbenchNote: vi.fn(),
  deleteWorkbenchNote: vi.fn(),
  syncWorkbenchIndex: vi.fn(),
  previewChapters: vi.fn(),
  importChapters: vi.fn(),
}))
vi.mock('@/api/bookLoreWorkbench', () => api)
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const overview = {
  available: true,
  counts: { notes: { rows: 2497, with_vector: 2445 }, communities: { rows: 5458, with_vector: 5458 }, entities: { rows: 17283, with_vector: 17283 } },
  indexed: { notes: 2020, communities: 5458, entities: 17283 },
  notes_not_indexed: 477,
  notes_not_indexed_sample: ['note_ch950', 'note_ch951'],
  stale_index_entries: 0,
  facets: { categories: [{ name: '章节事件', count: 61 }], arcs: [{ name: 'arc05_化神及以后', count: 400 }], books: ['没钱修什么仙'] },
  latest_chapter: 961,
  chapter_count: 61,
  sources: [{ path: '/data/没钱修什么仙.txt', name: '没钱修什么仙.txt', size: 12938963, modified_at: 1790000000, chapters: 965, latest_chapter: 965, latest_title: '第965章 新的开始', new_chapters: 4, new_range: [962, 965] }],
  recent_edits: [{ id: 1, at: 1790000000, action: 'import_chapters', target: '4 条', actor: 'webui', detail: {} }],
}

function renderPage() {
  return render(<MemoryRouter><BookLoreWorkbenchPage /></MemoryRouter>)
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getWorkbenchOverview.mockResolvedValue(overview)
  api.listWorkbenchNotes.mockResolvedValue({ items: [{ id: 'note_ch950', book_name: '没钱修什么仙', arc: 'arc05_化神及以后', category: '章节事件', title: '第950章', preview: '正文', length: 3000, has_vector: 0, indexed: false }], total: 1, limit: 30, offset: 0 })
  api.getWorkbenchNote.mockResolvedValue({ item: { id: 'note_ch950', book_name: '没钱修什么仙', arc: 'arc05_化神及以后', category: '章节事件', title: '第950章', content: '正文', source_file: '', has_vector: 0, indexed: false } })
  api.saveWorkbenchNote.mockResolvedValue({ ok: true, saved: ['note_ch950'], indexed: 1, pending_vectors: 0 })
  api.syncWorkbenchIndex.mockResolvedValue({ ok: true, missing: 477, indexed: 477, embedded: 52, failed: 0, removed_stale: 0, remaining: 0 })
  api.importChapters.mockResolvedValue({ ok: true, saved: ['note_ch962'], indexed: 4, pending_vectors: 0, chapters: [962, 963, 964, 965] })
  api.previewChapters.mockResolvedValue({ chapters: [{ number: 962, title: '第962章 新章', length: 3000, exists: false }, { number: 961, title: '第961章', length: 2000, exists: true }] })
})

describe('BookLoreWorkbenchPage', () => {
  it('提示未入索引并补齐', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText(/477 条笔记没进索引/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: /补齐索引/ }))
    await waitFor(() => expect(api.syncWorkbenchIndex).toHaveBeenCalled())
    await waitFor(() => expect(api.getWorkbenchOverview).toHaveBeenCalledTimes(2))
  })

  it('原文里的新章节需要确认后导入', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText(/4 章新章节（第 962–965 章）/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: /导入新章节/ }))
    expect(api.importChapters).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '确认导入' }))
    await waitFor(() => expect(api.importChapters).toHaveBeenCalledWith({ path: '/data/没钱修什么仙.txt', arc: '' }))
  })

  it('粘贴文本先解析，只导入新章', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '导入章节' }))
    await user.type(screen.getByRole('textbox', { name: '章节文本' }), '第962章 新章')
    await user.click(screen.getByRole('button', { name: '解析章节' }))
    expect(await screen.findByRole('button', { name: /导入 1 章/ })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /导入 1 章/ }))
    await waitFor(() => expect(api.importChapters).toHaveBeenCalledWith({ text: '第962章 新章', arc: '', overwrite: false }))
  })

  it('编辑笔记保存后刷新', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '笔记' }))
    await user.click(await screen.findByRole('button', { name: /第950章/ }))
    expect(await screen.findByText('还没进检索索引，保存后会自动加入')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /保存并更新索引/ }))
    await waitFor(() => expect(api.saveWorkbenchNote).toHaveBeenCalledWith(expect.objectContaining({ id: 'note_ch950', content: '正文' })))
  })
})

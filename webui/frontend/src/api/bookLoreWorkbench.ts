import { fetchJson } from './client'

const BASE = '/api/book-lore/workbench'

export interface TierCount { rows: number; with_vector: number }

export interface WorkbenchSource {
  path: string
  name: string
  size: number
  modified_at: number
  chapters: number
  latest_chapter: number
  latest_title: string
  new_chapters: number
  new_range: [number, number] | null
  /** 早于库里最新章、没有逐章笔记的章节（由 GraphRAG 社区摘要覆盖，默认不导入） */
  earlier_without_notes?: number
}

export interface WorkbenchEdit { id: number; at: number; action: string; target: string; actor: string; detail: Record<string, unknown> }

export interface WorkbenchOverview {
  available: boolean
  counts: Record<'entities' | 'communities' | 'notes', TierCount>
  indexed: Record<'entities' | 'communities' | 'notes', number>
  notes_not_indexed: number
  notes_not_indexed_sample: string[]
  stale_index_entries: number
  facets: { categories: Array<{ name: string; count: number }>; arcs: Array<{ name: string; count: number }>; books: string[] }
  latest_chapter: number | null
  chapter_count: number
  sources: WorkbenchSource[]
  recent_edits: WorkbenchEdit[]
}

export interface WorkbenchNoteRow {
  id: string
  book_name: string
  arc: string
  category: string
  title: string
  preview: string
  length: number
  has_vector: number | boolean
  indexed: boolean
}

export interface WorkbenchNote {
  id: string
  book_name: string
  arc: string
  category: string
  title: string
  content: string
  source_file: string
  has_vector: number | boolean
  indexed: boolean
}

export interface NoteQuery { q?: string; category?: string; arc?: string; indexed?: '' | '0' | '1'; limit?: number; offset?: number }

export interface ChapterPreview { number: number; title: string; length: number; exists: boolean }

export interface SaveResult { ok: boolean; saved: string[]; indexed: number; pending_vectors: number; chapters?: number[]; arc?: string; skipped_existing?: number }

export function getWorkbenchOverview(signal?: AbortSignal) {
  return fetchJson<WorkbenchOverview>(`${BASE}/overview`, { signal })
}

export function listWorkbenchNotes(query: NoteQuery = {}, signal?: AbortSignal) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
  return fetchJson<{ items: WorkbenchNoteRow[]; total: number; limit: number; offset: number }>(`${BASE}/notes?${params}`, { signal })
}

export function getWorkbenchNote(id: string) {
  return fetchJson<{ item: WorkbenchNote }>(`${BASE}/notes/${encodeURIComponent(id)}`)
}

export function saveWorkbenchNote(item: Partial<WorkbenchNote>) {
  return fetchJson<SaveResult>(`${BASE}/notes`, { method: 'POST', body: JSON.stringify({ item }) })
}

export function deleteWorkbenchNote(id: string) {
  return fetchJson<{ ok: boolean; deleted: string }>(`${BASE}/notes/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function syncWorkbenchIndex() {
  return fetchJson<{ ok: boolean; missing: number; indexed: number; embedded: number; failed: number; removed_stale: number; remaining: number }>(
    `${BASE}/sync-index`, { method: 'POST', body: '{}' },
  )
}

export function previewChapters(text: string) {
  return fetchJson<{ chapters: ChapterPreview[] }>(`${BASE}/chapters/preview`, { method: 'POST', body: JSON.stringify({ text }) })
}

export function importChapters(body: { path?: string; text?: string; numbers?: number[]; arc?: string; overwrite?: boolean }) {
  return fetchJson<SaveResult>(`${BASE}/chapters/import`, { method: 'POST', body: JSON.stringify(body) })
}

import { fetchJson } from './client'

const BASE = '/api/game-knowledge'

export type GameItemStatus = 'draft' | 'active' | 'archived'
export type GameItemKind = 'cards' | 'terms'
export type ImportStatusMode = 'auto' | 'draft' | 'active'
export type ImportOnExisting = 'skip' | 'update' | 'merge'

export interface StatusCounts { draft: number; active: number; archived: number }

export interface GameLibrary {
  id: string
  name: string
  aliases: string[]
  description: string
  enabled: boolean
  version: number
  created_at: number
  updated_at: number
  counts: { cards: StatusCounts; terms: StatusCounts }
}

export interface GamePack {
  id: string
  title: string
  description: string
  force_status: string | null
  game: { id: string; name: string }
  cards: number
  terms: number
  game_exists: boolean
}

export interface LegacySource {
  source: string
  game_id: string
  game_name: string
  game_exists: boolean
  description: string
  scanned: boolean
  rows: number | null
  scanned_at: number | null
  migrated: number
}

export interface GameOverview {
  games: GameLibrary[]
  packs: GamePack[]
  legacy_sources: LegacySource[]
  statuses: Record<GameItemStatus, string>
  source_types: Record<string, string>
  import_modes: ImportOnExisting[]
  trigger_rule: string
}

export interface GameCondition { key: string; op: string; value?: unknown; min?: number; max?: number; required?: boolean }

interface ItemBase {
  id: number
  game_id: string
  key: string
  source: string
  source_url: string
  source_type: string
  status: GameItemStatus
  version: number
  note: string
  meta: Record<string, unknown>
  created_at: number
  updated_at: number
}

export interface GameCard extends ItemBase {
  title: string
  stage: string
  points: string[]
  pitfalls: string[]
  keywords: string[]
  conditions: GameCondition[]
  conditions_text: string[]
  trigger_mode: 'keywords' | 'conditions' | 'none'
}

export interface GameTerm extends ItemBase {
  term: string
  aliases: string[]
  explanation: string
  related: string[]
}

export interface ItemPage<T> { items: T[]; total: number; limit: number; offset: number }

export interface ItemQuery { status?: string; stage?: string; q?: string; source_type?: string; limit?: number; offset?: number }

export interface MutationResult<T> { ok: boolean; revision: number; item: T }

export interface StatusResult {
  ok: boolean
  status: GameItemStatus
  changed: number[]
  unchanged: number[]
  missing: number[]
  conflicts: Array<{ id: number; expected: number; actual: number }>
}

export interface ImportSummary { created: number; updated: number; unchanged: number; skipped: number }

export interface ImportPreview {
  format: string
  format_label: string
  game_id: string
  file_game: Record<string, unknown>
  force_status: string | null
  default_status_mode: ImportStatusMode
  warnings: string[]
  cards: { total: number; new: number; existing: number; invalid: number; by_status: Record<string, number> }
  terms: { total: number; new: number; existing: number; invalid: number; by_status: Record<string, number> }
  errors: Array<{ kind: 'card' | 'term'; index: number; label: string; code: string; message: string }>
  samples: {
    cards: Array<{ key: string; status: string; exists: boolean; title: string; stage: string; keywords: string[]; conditions: string[]; points: number; pitfalls: number; source: string }>
    terms: Array<{ key: string; status: string; exists: boolean; term: string; aliases: string[]; explanation: string }>
  }
}

export interface ImportResult {
  ok: boolean
  format: string
  format_label: string
  status_mode: string
  on_existing: ImportOnExisting
  cards: ImportSummary
  terms: ImportSummary
  invalid: { cards: number; terms: number }
  errors: ImportPreview['errors']
  warnings: string[]
  pack?: string
  created_game?: boolean
}

export interface LegacyMigrateResult extends Partial<ImportResult> {
  ok: boolean
  source: string
  rows: number
  skipped: boolean
  reason?: string
  parsed?: number
  parse_failed?: number
  duplicates?: number
  created_game: boolean
}

export interface TrialCard { id: number; key: string; title: string; stage: string; status: GameItemStatus; score: number; reason: string }

export interface TrialResult {
  game_id: string
  include_drafts: boolean
  state: Record<string, unknown>
  keywords: string[]
  warnings: string[]
  reason: string
  cards: TrialCard[]
  overflow: TrialCard[]
  terms: Array<{ id: number; term: string; status: GameItemStatus; reason: string }>
  blocked: Array<{ id: number; title: string; reason: string }>
  text: string
  rule: string
}

export interface GamePayload { id?: string; name?: string; aliases?: string[] | string; description?: string; enabled?: boolean }

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== '') search.set(key, String(value))
  const text = search.toString()
  return text ? `?${text}` : ''
}

function gamePath(gameId: string): string {
  return `${BASE}/games/${encodeURIComponent(gameId)}`
}

export function getGameOverview(signal?: AbortSignal) {
  return fetchJson<GameOverview>(`${BASE}/overview`, { signal })
}

export function createGame(item: GamePayload) {
  return fetchJson<MutationResult<GameLibrary>>(`${BASE}/games`, { method: 'POST', body: JSON.stringify({ item }) })
}

export function updateGame(gameId: string, payload: GamePayload, version: number) {
  return fetchJson<MutationResult<GameLibrary>>(gamePath(gameId), { method: 'PUT', body: JSON.stringify({ ...payload, version }) })
}

export function getGame(gameId: string, signal?: AbortSignal) {
  return fetchJson<{ item: GameLibrary; stages: Array<{ name: string; count: number }> }>(gamePath(gameId), { signal })
}

export function listGameCards(gameId: string, params: ItemQuery = {}, signal?: AbortSignal) {
  return fetchJson<ItemPage<GameCard>>(`${gamePath(gameId)}/cards${query({ ...params })}`, { signal })
}

export function listGameTerms(gameId: string, params: ItemQuery = {}, signal?: AbortSignal) {
  return fetchJson<ItemPage<GameTerm>>(`${gamePath(gameId)}/terms${query({ ...params })}`, { signal })
}

export function getGameCard(gameId: string, id: number) {
  return fetchJson<{ item: GameCard }>(`${gamePath(gameId)}/cards/${id}`)
}

export function getGameTerm(gameId: string, id: number) {
  return fetchJson<{ item: GameTerm }>(`${gamePath(gameId)}/terms/${id}`)
}

export function saveGameCard(gameId: string, item: Record<string, unknown>, id?: number, version?: number) {
  if (id === undefined) return fetchJson<MutationResult<GameCard>>(`${gamePath(gameId)}/cards`, { method: 'POST', body: JSON.stringify({ item }) })
  return fetchJson<MutationResult<GameCard>>(`${gamePath(gameId)}/cards/${id}`, { method: 'PUT', body: JSON.stringify({ item, version }) })
}

export function saveGameTerm(gameId: string, item: Record<string, unknown>, id?: number, version?: number) {
  if (id === undefined) return fetchJson<MutationResult<GameTerm>>(`${gamePath(gameId)}/terms`, { method: 'POST', body: JSON.stringify({ item }) })
  return fetchJson<MutationResult<GameTerm>>(`${gamePath(gameId)}/terms/${id}`, { method: 'PUT', body: JSON.stringify({ item, version }) })
}

export function setGameItemsStatus(gameId: string, kind: GameItemKind, ids: number[], status: GameItemStatus, versions?: Record<string, number>) {
  return fetchJson<StatusResult>(`${gamePath(gameId)}/${kind}/status`, { method: 'POST', body: JSON.stringify({ ids, status, versions }) })
}

export function previewGameImport(gameId: string, data: unknown, format = 'auto') {
  return fetchJson<ImportPreview>(`${gamePath(gameId)}/import/preview`, { method: 'POST', body: JSON.stringify({ data, format }), timeoutMs: 60000 })
}

export function runGameImport(gameId: string, data: unknown, options: { status: ImportStatusMode; on_existing: ImportOnExisting; format?: string }) {
  return fetchJson<ImportResult>(`${gamePath(gameId)}/import`, { method: 'POST', body: JSON.stringify({ data, ...options }), timeoutMs: 60000 })
}

export function exportGame(gameId: string, status?: string) {
  return fetchJson<Record<string, unknown>>(`${gamePath(gameId)}/export${query({ status })}`)
}

export function trialGameMatch(gameId: string, body: { message: string; keywords: string[]; state: Record<string, string | number | boolean>; include_drafts: boolean }) {
  return fetchJson<TrialResult>(`${gamePath(gameId)}/trial`, { method: 'POST', body: JSON.stringify(body) })
}

export function importGamePack(packId: string) {
  return fetchJson<ImportResult>(`${BASE}/packs/${encodeURIComponent(packId)}/import`, { method: 'POST', body: '{}', timeoutMs: 60000 })
}

export function scanLegacySource(source: string) {
  return fetchJson<{ ok: boolean; source: string; rows: number; parsed: number | null }>(`${BASE}/legacy/${encodeURIComponent(source)}/scan`, { method: 'POST', body: '{}', timeoutMs: 120000 })
}

export function migrateLegacySource(source: string) {
  return fetchJson<LegacyMigrateResult>(`${BASE}/legacy/${encodeURIComponent(source)}/migrate`, { method: 'POST', body: '{}', timeoutMs: 120000 })
}

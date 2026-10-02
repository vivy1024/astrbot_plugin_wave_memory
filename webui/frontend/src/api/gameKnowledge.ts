import { fetchJson } from './client'

const BASE = '/api/game-knowledge'

export type GameItemStatus = 'draft' | 'active' | 'archived'
export type GameItemKind = 'cards' | 'terms'
export type ImportStatusMode = 'auto' | 'draft' | 'active'
export type ImportOnExisting = 'skip' | 'update' | 'merge'

export interface StatusCounts { draft: number; active: number; archived: number }

/** 直播请求里见过的状态键（只读）：键 → 一个示例值与首次见到的时间。 */
export type GameStateKeys = Record<string, { example?: string; seen_at?: number; [extra: string]: unknown }>

export interface GameLibrary {
  id: string
  name: string
  aliases: string[]
  description: string
  enabled: boolean
  /** 允许 Bot 在这个游戏里从自己的经历写经验卡（总开关是 Learning_Settings.game_lesson_enabled）。 */
  lessons_enabled?: boolean
  state_keys?: GameStateKeys
  version: number
  created_at: number
  updated_at: number
  /** cards 只算共用的攻略卡；lessons 是各 Bot 经验卡的合计。 */
  counts: { cards: StatusCounts; terms: StatusCounts; lessons?: StatusCounts }
}

export interface GameBot { id: string; name: string }

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
  legacy_sources: LegacySource[]
  bots: GameBot[]
  /** 游戏经验总开关（Learning_Settings.game_lesson_enabled）是否开着、服务是否在跑。 */
  lessons_enabled: boolean
  statuses: Record<GameItemStatus, string>
  source_types: Record<string, string>
  import_modes: ImportOnExisting[]
  trigger_rule: string
}

/** required：缺键不触发；soft（「参考」）：只影响排序，不挡触发。 */
export interface GameCondition { key: string; op: string; value?: unknown; min?: number; max?: number; required?: boolean; soft?: boolean }

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
  topic?: string
  points: string[]
  pitfalls: string[]
  keywords: string[]
  conditions: GameCondition[]
  conditions_text: string[]
  trigger_mode: 'keywords' | 'conditions' | 'none'
  /** 空串 = 所有 Bot 共用的攻略；否则是这个 Bot 自己的经验卡。 */
  bot_id?: string
  experience?: boolean
}

export interface LessonEvidence { memory_id: number; at: number; type: string; text: string }

/** 经验卡的 meta（points 是「做法」，pitfalls 是「经过」）。 */
export interface LessonMeta {
  times?: number
  first_at?: number
  last_at?: number
  evidence?: LessonEvidence[]
  revisions?: number
  revised_why?: string
  vetoed?: boolean
  vetoed_at?: number
  [extra: string]: unknown
}

export type LessonTrigger = 'event' | 'daily' | 'manual' | string

export interface LessonRun {
  id: number
  bot_id: string
  /** 一次回想可能同时想了几个游戏：逗号分隔。 */
  game_id: string
  trigger: LessonTrigger
  status: string
  started_at: number
  finished_at: number | null
  from_memory_id: number
  to_memory_id: number
  events: number
  model_calls: number
  counts: Record<string, number>
  error: string
}

export interface LessonPage extends ItemPage<GameCard> {
  enabled: boolean
  runs: LessonRun[]
  stats: Record<string, number>
  pending: string[]
}

export interface ReflectResult { ok: boolean; status: 'started' | 'running' | 'disabled' | string }

export interface GameTerm extends ItemBase {
  term: string
  aliases: string[]
  explanation: string
  related: string[]
}

export interface ItemPage<T> { items: T[]; total: number; limit: number; offset: number }

/** owner（只对知识卡）：shared = 共用的攻略（默认）/ all / 某个 Bot 的标识（它自己的经验卡）。 */
export interface ItemQuery { status?: string; stage?: string; topic?: string; q?: string; source_type?: string; owner?: string; limit?: number; offset?: number }

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

export interface TrialCard {
  id: number
  key: string
  title: string
  stage: string
  status: GameItemStatus
  /** 空串 = 共用的攻略；否则是这个 Bot 自己的经验卡。 */
  bot_id?: string
  experience?: boolean
  score: number
  reason: string
}

export interface TrialResult {
  game_id: string
  bot_id?: string
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

export interface GamePayload { id?: string; name?: string; aliases?: string[] | string; description?: string; enabled?: boolean; lessons_enabled?: boolean }

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
  return fetchJson<{ item: GameLibrary; stages: Array<{ name: string; count: number }>; topics: Array<{ name: string; count: number }> }>(gamePath(gameId), { signal })
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

export function trialGameMatch(gameId: string, body: { message: string; keywords: string[]; state: Record<string, string | number | boolean>; include_drafts: boolean; bot_id?: string }) {
  return fetchJson<TrialResult>(`${gamePath(gameId)}/trial`, { method: 'POST', body: JSON.stringify(body) })
}

export interface LessonQuery { bot_id: string; status?: string; q?: string; limit?: number; offset?: number }

/** 某个 Bot 在这个游戏里自己写下的经验卡与最近的回想记录。 */
export function listGameLessons(gameId: string, params: LessonQuery, signal?: AbortSignal) {
  return fetchJson<LessonPage>(`${gamePath(gameId)}/lessons${query({ ...params })}`, { signal })
}

/** 「现在想一次」：后台回想这个 Bot 还没想过的游戏经历，立即返回。 */
export function reflectGameLessons(gameId: string, botId: string) {
  return fetchJson<ReflectResult>(`${gamePath(gameId)}/lessons/reflect`, { method: 'POST', body: JSON.stringify({ bot_id: botId }) })
}

/** 删掉一条经验（否决：她不会再写意思相近的）。 */
export function vetoGameLesson(gameId: string, cardId: number, botId: string, version: number) {
  return fetchJson<MutationResult<GameCard>>(`${gamePath(gameId)}/lessons/${cardId}/veto`, { method: 'POST', body: JSON.stringify({ bot_id: botId, version }) })
}

/** 恢复一条删掉 / 归档的经验。 */
export function restoreGameLesson(gameId: string, cardId: number, botId: string, version: number) {
  return fetchJson<MutationResult<GameCard>>(`${gamePath(gameId)}/lessons/${cardId}/restore`, { method: 'POST', body: JSON.stringify({ bot_id: botId, version }) })
}

export function scanLegacySource(source: string) {
  return fetchJson<{ ok: boolean; source: string; rows: number; parsed: number | null }>(`${BASE}/legacy/${encodeURIComponent(source)}/scan`, { method: 'POST', body: '{}', timeoutMs: 120000 })
}

export function migrateLegacySource(source: string) {
  return fetchJson<LegacyMigrateResult>(`${BASE}/legacy/${encodeURIComponent(source)}/migrate`, { method: 'POST', body: '{}', timeoutMs: 120000 })
}

import { fetchJson } from './client'

const BASE = '/api/character-experience'
/** 列表里只看「没分阶段」的经历 */
export const UNASSIGNED_STAGE = '__unassigned__'

export type ExperienceStatus = 'draft' | 'active' | 'archived'
export type CanonLevel = 'canon' | 'inferred' | 'invented'
export type StageStatus = 'draft' | 'confirmed'

export const STATUS_LABELS: Record<ExperienceStatus, string> = { draft: '草稿', active: '启用', archived: '归档' }
export const CANON_LABELS: Record<CanonLevel, string> = { canon: '原著', inferred: '推演', invented: '编造' }
export const SOURCE_KIND_LABELS: Record<string, string> = {
  novel_extract: '从原著提取',
  setting_compose: '按设定编写',
  manual: '手写',
  jsonl_import: '导入 JSONL',
  legacy_migration: '旧数据迁移',
}
export const JOB_KINDS = {
  extract: 'character_experience.extract.v1',
  timeline: 'character_experience.timeline.v1',
  expand: 'character_experience.expand.v1',
  embed: 'character_experience.embed.v1',
} as const
export const JOB_KIND_LABELS: Record<string, string> = {
  [JOB_KINDS.extract]: '从原著提取',
  [JOB_KINDS.timeline]: '起草时间线',
  [JOB_KINDS.expand]: '展开成经历',
  [JOB_KINDS.embed]: '补算向量',
}
export const EMOTIONS = ['日常', '温馨', '战斗', '紧张', '伤感', '搞笑', '愤怒', '感动'] as const

export type StatusCounts = Record<ExperienceStatus, number>

export interface CharacterExperience {
  id: number
  bot_id: string
  content: string
  content_hash?: string
  story_time: string
  stage_key: string
  seq: number
  plot_stage: string
  emotion: string
  characters: string[]
  canon_level: CanonLevel
  source_kind: string
  source_ref: Record<string, unknown>
  status: ExperienceStatus
  version: number
  job_id: string
  created_by: string
  created_at: number
  updated_at: number
  has_vector: boolean
}

export interface KeyEvent { text: string; basis: number[] }

export interface TimelineStage {
  bot_id: string
  stage_key: string
  ordinal: number
  title: string
  story_time: string
  description: string
  key_events: KeyEvent[]
  chapter_from: number | null
  chapter_to: number | null
  status: StageStatus
  source_kind: string
  source_ref: Record<string, unknown>
  version: number
  created_at: number
  updated_at: number
  counts?: StatusCounts
  is_current?: boolean
  recallable?: boolean
}

export interface CharacterSettings {
  enabled: boolean
  /** 当前时间点（阶段 key）；空 = 不限，能想起所有阶段 */
  current_stage: string
  top_k: number
  max_chars: number
  min_score: number
  legacy_sources: string[]
  lore_categories: string[]
}

export interface CharacterJob {
  run_id: string
  request_id: string
  kind: string
  payload: Record<string, unknown>
  status: string
  progress: Record<string, unknown>
  result: Record<string, unknown> | null
  error_code: string | null
  error_message: string | null
  created_at: number
  updated_at: number
  generation: number
  resumable: boolean
}

export interface CharacterOverview {
  bot: { db_id: string; name: string; aliases: string[]; enabled: boolean; version: number; persona_text: string }
  settings: CharacterSettings
  stages: TimelineStage[]
  unassigned: StatusCounts
  counts: {
    total: number
    by_status: StatusCounts
    by_stage: Record<string, StatusCounts>
    by_canon: Record<CanonLevel, number>
    by_source: Record<string, number>
    active_missing_vectors: number
  }
  recallable_active: number
  warnings: Array<{ code: string; message: string }>
  model: { available: boolean; primary?: string; source_label?: string; chain?: string[] }
  embedding_available: boolean
  jobs: CharacterJob[]
  scene_stats: Record<string, Record<string, number>>
  migrations: Array<{ key: string; status: string; report: Record<string, unknown>; applied_at: number }>
  sources: Array<{ path: string; name: string; size: number; modified_at: number }>
}

export interface ExperienceQuery { stage?: string; status?: string; q?: string; canon?: string; source_kind?: string; order?: 'timeline' | 'recent'; limit?: number; offset?: number }

export interface ExtractionParams {
  path: string
  name: string
  aliases: string
  description: string
  chapter_from?: number | null
  chapter_to?: number | null
  require_full_name: boolean
  redo: boolean
  concurrency: number
}

export interface ExtractionPlan {
  source: { path: string; name: string }
  chapters_total: number
  chapters_in_range: number
  chapters_with_character: number
  scenes: number
  already_processed: number
  calls: number
  by_stage: Record<string, number>
  unassigned_scenes: number
  sample: Array<{ chapter: number; title: string; chars: number; preview: string }>
  model: { available: boolean; primary?: string; source_label?: string }
}

export interface ExpansionPlan {
  stages: string[]
  unconfirmed_stages: string[]
  events: number
  already_processed: number
  calls: number
  model: { available: boolean; primary?: string; source_label?: string }
}

export interface JobAccepted { ok: boolean; calls: number; job: { job_id: string; request_id: string; status: string } }

export interface ImportResult {
  ok: boolean
  lines: number
  valid: number
  inserted: number
  updated: number
  duplicates: number
  errors: Array<{ line: number; message: string }>
  error_count: number
  stages_created: string[]
}

export interface RecallPreview {
  items: Array<CharacterExperience & { score: number; similarity: number | null; keyword_hit: boolean; stage_title: string; rendered_text: string }>
  warning: string
  current_stage: string
  eligible_stages: string[] | null
  embedding_used: boolean
}

type VersionedId = { id: number; version: number }
type VersionedStage = { stage_key: string; version: number }

function bot(botId: string): string {
  return `${BASE}/bots/${encodeURIComponent(botId)}`
}

function post<T>(path: string, body: unknown, timeoutMs = 15000): Promise<T> {
  return fetchJson<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}), timeoutMs })
}

export function getCharacterOverview(botId: string, signal?: AbortSignal) {
  return fetchJson<CharacterOverview>(`${bot(botId)}/overview`, { signal })
}

export function listCharacterExperiences(botId: string, query: ExperienceQuery = {}, signal?: AbortSignal) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
  return fetchJson<{ items: CharacterExperience[]; total: number; limit: number; offset: number }>(`${bot(botId)}/experiences?${params}`, { signal })
}

export function getCharacterExperience(botId: string, id: number) {
  return fetchJson<{ item: CharacterExperience; stage: TimelineStage | null }>(`${bot(botId)}/experiences/${id}`)
}

export function createCharacterExperience(botId: string, item: Partial<CharacterExperience>) {
  return post<{ ok: boolean; item: CharacterExperience }>(`${bot(botId)}/experiences`, { item })
}

export function updateCharacterExperience(botId: string, id: number, version: number, fields: Partial<CharacterExperience>) {
  return fetchJson<{ ok: boolean; item: CharacterExperience }>(`${bot(botId)}/experiences/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ version, fields }),
  })
}

export function setCharacterExperienceStatus(botId: string, items: VersionedId[], status: ExperienceStatus) {
  return post<{ ok: boolean; changed: number[] }>(`${bot(botId)}/experiences/status`, { items, status })
}

export function setCharacterExperienceStage(botId: string, items: VersionedId[], stageKey: string) {
  return post<{ ok: boolean; changed: number[] }>(`${bot(botId)}/experiences/stage`, { items, stage_key: stageKey })
}

export function saveCharacterSettings(botId: string, version: number, patch: Partial<CharacterSettings>) {
  return fetchJson<{ ok: boolean; version: number; settings: CharacterSettings }>(`${bot(botId)}/settings`, {
    method: 'PUT',
    body: JSON.stringify({ version, patch }),
  })
}

export function saveTimelineStage(botId: string, stageKey: string, version: number, fields: Partial<TimelineStage>) {
  return fetchJson<{ ok: boolean; item: TimelineStage }>(`${bot(botId)}/stages/${encodeURIComponent(stageKey)}`, {
    method: 'PUT',
    body: JSON.stringify({ version, fields }),
  })
}

export function deleteTimelineStage(botId: string, stageKey: string, version: number) {
  return fetchJson<{ ok: boolean }>(`${bot(botId)}/stages/${encodeURIComponent(stageKey)}?version=${version}`, { method: 'DELETE' })
}

export function reorderTimelineStages(botId: string, items: VersionedStage[]) {
  return post<{ ok: boolean; items: TimelineStage[] }>(`${bot(botId)}/stages/reorder`, { items })
}

export function confirmTimelineStages(botId: string, items: VersionedStage[]) {
  return post<{ ok: boolean; confirmed: string[] }>(`${bot(botId)}/stages/confirm`, { items })
}

export function importCharacterJsonl(botId: string, body: { text: string; status: 'draft' | 'active' | 'as_file'; canon_level: CanonLevel; on_duplicate: 'skip' | 'fill' | 'overwrite' }) {
  return post<ImportResult>(`${bot(botId)}/import`, body, 120000)
}

export async function exportCharacterJsonl(botId: string, statuses: ExperienceStatus[]): Promise<string> {
  const params = statuses.length ? `?status=${statuses.join(',')}` : ''
  // 响应是 application/x-ndjson：多行时 fetchJson 返回原文；只有一行时会被当成 JSON 解析成对象，这里还原成一行
  const payload = await fetchJson<unknown>(`${bot(botId)}/export${params}`, { timeoutMs: 60000 })
  if (typeof payload === 'string') return payload
  return payload == null ? '' : `${JSON.stringify(payload)}\n`
}

export function previewExtraction(botId: string, params: ExtractionParams) {
  return post<ExtractionPlan>(`${bot(botId)}/extract/preview`, params, 120000)
}

export function startExtraction(botId: string, params: ExtractionParams, expectedCalls: number) {
  return post<JobAccepted>(`${bot(botId)}/extract`, { ...params, confirm: true, expected_calls: expectedCalls }, 120000)
}

export function previewTimelineDraft(botId: string, setting: string) {
  return post<{ calls: number; paragraphs: number; setting_chars: number }>(`${bot(botId)}/timeline/preview`, { setting })
}

export function startTimelineDraft(botId: string, setting: string, replaceDrafts: boolean) {
  return post<JobAccepted>(`${bot(botId)}/timeline/draft`, { setting, replace_drafts: replaceDrafts, confirm: true, expected_calls: 1 })
}

export function previewExpansion(botId: string, stageKeys: string[], redo = false) {
  return post<ExpansionPlan>(`${bot(botId)}/expand/preview`, { stage_keys: stageKeys, redo })
}

export function startExpansion(botId: string, body: { stage_keys: string[]; setting: string; redo: boolean }, expectedCalls: number) {
  return post<JobAccepted>(`${bot(botId)}/expand`, { ...body, confirm: true, expected_calls: expectedCalls })
}

export function startEmbedding(botId: string) {
  return post<{ ok: boolean; job: JobAccepted['job'] | null }>(`${bot(botId)}/embed`, {})
}

export function listCharacterJobs(botId: string, signal?: AbortSignal) {
  return fetchJson<{ items: CharacterJob[] }>(`${bot(botId)}/jobs`, { signal })
}

export function resumeCharacterJob(runId: string) {
  return post<{ ok: boolean; run_id: string; generation: number }>(`${BASE}/jobs/${encodeURIComponent(runId)}/resume`, {})
}

export function cancelCharacterJob(runId: string) {
  return post<{ status?: string }>(`/api/maintenance/jobs/${encodeURIComponent(runId)}/cancel`, {})
}

export function tryCharacterRecall(botId: string, message: string) {
  return post<RecallPreview>(`${bot(botId)}/recall`, { message }, 30000)
}

export function runLegacyMigration(dryRun: boolean) {
  return post<{ ok: boolean; reports: Array<Record<string, unknown>> }>(`${BASE}/legacy-migration`, { dry_run: dryRun }, 120000)
}

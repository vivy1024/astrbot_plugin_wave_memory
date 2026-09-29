import { fetchJson } from './client'

export type BindingHost = 'astrbot' | 'cortico' | 'bilibili'

export interface BotBindingDto {
  host: BindingHost
  self_id?: string
  platform?: string
  deployment?: string
  room?: string
}

export interface PersonaDto {
  self_terms: string[]
  identity_guard_enabled: boolean
  identity_guard_rules: string[]
  diary_signature: string
  lore_lines: string[]
  lore_title: string
  experience_source: string
  reflection_prefixes: string[]
  /** 空=部署默认书设；none=不用书设；其他=语料 id */
  lore_corpus?: string
}

/** 心情画像（Bot Profile 的 mood 段；见 domain/mood_policy.py） */
export interface MoodDto {
  enabled: boolean
  baseline_valence: number
  baseline_arousal: number
  sensitivity: number
  /** 0 = 用全局 Lifecycle_Settings.mood_duration_hours */
  half_life_minutes: number
  strong_event_boost: number
  atmosphere_weight: number
  fatigue_enabled: boolean
  fatigue_window_minutes: number
  fatigue_reply_threshold: number
  night_start_hour: number
  night_end_hour: number
  fatigue_strength: number
  /** 情绪词或类别（正面/负面/平静/疲惫）→ 说话倾向 */
  speaking_hints: Record<string, string>
  positive_words: string[]
  negative_words: string[]
}

export function emptyMood(): MoodDto {
  return {
    enabled: true,
    baseline_valence: 0.1,
    baseline_arousal: 0.4,
    sensitivity: 1,
    half_life_minutes: 0,
    strong_event_boost: 1,
    atmosphere_weight: 0.15,
    fatigue_enabled: true,
    fatigue_window_minutes: 120,
    fatigue_reply_threshold: 40,
    night_start_hour: 1,
    night_end_hour: 6,
    fatigue_strength: 0.5,
    speaking_hints: {},
    positive_words: [],
    negative_words: [],
  }
}

export interface BotProfileDto {
  db_id: string
  name: string
  qq_id: string
  aliases: string[]
  meta_prompt: string
  proactive_enabled: boolean
  proactive_interval_seconds: number
  proactive_max_per_hour: number
  exclude_sources: string[]
  interest_keywords: string[]
  enabled: boolean
  /** 该 Bot 的默认模型：只作用于 BOT_MODEL_TASKS（Bot 口吻任务） */
  model: string
  /** 按任务的模型覆盖 {任务 id: provider id}；旧后端没有这一段 */
  models?: Record<string, string>
  session_prefix: string
  bindings: BotBindingDto[]
  persona: PersonaDto
  /** 旧后端没有这一段；编辑器按 emptyMood() 补齐 */
  mood?: MoodDto
  channels: Record<string, Record<string, unknown>>
  tools_allow: string[]
  tools_deny: string[]
  version: number
  origin: string
  self_ids?: string[]
  identity_terms?: string[]
}

export interface BotRegistryStatus {
  source: string
  attached: boolean
  revision: number
  enabled: number
  total: number
  legacy_slots: string[]
  migration: { migrated?: string[] }
  invalid_rows: Record<string, string>
}

export interface BotListPayload {
  items: BotProfileDto[]
  status: BotRegistryStatus
  binding_hosts: BindingHost[]
  /** 允许按 Bot 覆盖模型的任务 id（旧后端没有） */
  model_tasks?: string[]
}

/** 允许按 Bot 覆盖模型的任务：只有以 Bot 自己口吻产出内容的任务（与 domain/bot_profile.py 一致）。 */
export const BOT_MODEL_TASKS = [
  { id: 'meta_thinking', label: '内心判断' },
  { id: 'bookkeeping', label: '事后记账' },
  { id: 'diary', label: '每日日记' },
  { id: 'portrait', label: '关系小传（预留）' },
] as const

export interface BotHistoryEntry {
  version: number
  changed_at: number
  changed_by: string
  reason: string
}

export interface BotDetailPayload {
  item: BotProfileDto
  counts: Record<string, number>
  history: BotHistoryEntry[]
}

export function listBots(signal?: AbortSignal) {
  return fetchJson<BotListPayload>('/api/bots', { signal })
}

export function getBot(dbId: string, signal?: AbortSignal) {
  return fetchJson<BotDetailPayload>(`/api/bots/${encodeURIComponent(dbId)}`, { signal })
}

export function saveBot(dbId: string, item: Partial<BotProfileDto>, version: number, reason = '') {
  return fetchJson<{ ok: boolean; item: BotProfileDto }>(`/api/bots/${encodeURIComponent(dbId)}`, {
    method: 'PUT',
    body: JSON.stringify({ item, version, reason }),
  })
}

export function setBotEnabled(dbId: string, enabled: boolean, version: number) {
  return fetchJson<{ ok: boolean; item: BotProfileDto }>(`/api/bots/${encodeURIComponent(dbId)}/enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled, version }),
  })
}

export function exportBots() {
  return fetchJson<{ schema: string; items: BotProfileDto[] }>('/api/bots/export')
}

export function importBots(items: unknown[]) {
  return fetchJson<{ ok: boolean; imported: string[]; errors: Record<string, string> }>('/api/bots/import', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })
}

export function emptyBot(dbId = ''): BotProfileDto {
  return {
    db_id: dbId,
    name: '',
    qq_id: '',
    aliases: [],
    meta_prompt: '',
    proactive_enabled: false,
    proactive_interval_seconds: 600,
    proactive_max_per_hour: 3,
    exclude_sources: [],
    interest_keywords: [],
    enabled: true,
    model: '',
    models: {},
    session_prefix: '',
    bindings: [],
    persona: {
      self_terms: [],
      identity_guard_enabled: true,
      identity_guard_rules: [],
      diary_signature: '',
      lore_lines: [],
      lore_title: '',
      experience_source: '',
      reflection_prefixes: [],
      lore_corpus: '',
    },
    mood: emptyMood(),
    channels: {},
    tools_allow: [],
    tools_deny: [],
    version: 0,
    origin: 'webui',
  }
}

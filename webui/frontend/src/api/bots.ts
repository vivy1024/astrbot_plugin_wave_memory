import { fetchJson } from './client'
import { DEFAULT_PRONOUN, type CharacterPronoun } from './characterExperience'

export type BindingHost = 'astrbot' | 'cortico' | 'bilibili'

export interface BotBindingDto {
  host: BindingHost
  self_id?: string
  platform?: string
  deployment?: string
  room?: string
}

export type PreferenceContext = 'all' | 'group' | 'private'

export interface PreferenceItemDto {
  text: string
  tags: string[]
  /** 1–5，越大越容易在相关话题里被想起 */
  strength: number
  context: PreferenceContext
}

export interface ReactionExampleDto {
  situation: string
  reaction: string
  tags: string[]
  strength: number
  context: PreferenceContext
}

/** Bot 自己的喜恶预设（跨群稳定）；缺字段=还没配置过，空对象=明确清空 */
export interface PreferenceSettingsDto {
  likes: PreferenceItemDto[]
  dislikes: PreferenceItemDto[]
  reaction_examples: ReactionExampleDto[]
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
  /** 旧后端没有这一段；编辑器按 emptyPreferences() 补齐 */
  preferences?: PreferenceSettingsDto | null
}

export function emptyPreferences(): PreferenceSettingsDto {
  return { likes: [], dislikes: [], reaction_examples: [] }
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

/** 角色经历设置（Bot Profile 的 character_experience 段；见 domain/character_experience.py）。没有这一段 = 关闭 */
export interface CharacterExperienceDto {
  enabled: boolean
  /** 当前时间点（时间线阶段 key）；空 = 不限，能想起所有阶段 */
  current_stage: string
  /** 提取、起草、展开的提示词里指代角色用的代词（她 / 他 / ta，默认 ta）；旧资料没有这个字段时按默认值 */
  pronoun?: CharacterPronoun
  top_k: number
  max_chars: number
  min_score: number
  /** 记忆表里已由角色经历接管的旧来源值（这些旧行不再走普通记忆召回） */
  legacy_sources: string[]
  /** 书设库里属于她个人经历的笔记类别（不当世界观书设注入） */
  lore_categories: string[]
}

export function emptyCharacterExperience(): CharacterExperienceDto {
  return { enabled: false, current_stage: '', pronoun: DEFAULT_PRONOUN, top_k: 3, max_chars: 600, min_score: 0.35, legacy_sources: [], lore_categories: [] }
}

export interface BodyDto {
  enabled: boolean
  sleep_start: number | null
  sleep_end: number | null
  sleep_replies: string[]
  sleep_reply_cooldown_seconds: number
  hormone_gain: number
  service_quiet_start: number | null
  service_quiet_end: number | null
  quiet_replies: string[]
}

export function emptyBody(): BodyDto {
  return {
    enabled: false,
    sleep_start: null,
    sleep_end: null,
    sleep_replies: ['……zzz', '（睡着了）', '困……明天再说'],
    sleep_reply_cooldown_seconds: 1800,
    hormone_gain: 1,
    service_quiet_start: null,
    service_quiet_end: null,
    quiet_replies: [
      '（打哈欠）这会儿困得睁不开眼，{end}再来找我吧……',
      'zzz……梦里算账呢，{end}见。',
      '夜深了，我先去睡了，{end}以后再聊。',
      '眼皮打架中，明早{end}再陪你。',
      '已经睡着啦，{end}再叫醒我哦。',
      '账本合上了，睡觉去，{end}见。',
    ],
  }
}

export type ProactiveMode = 'off' | 'shadow' | 'enforce'
export type ProactiveStylePreset = 'quiet' | 'balanced' | 'active'

/** Bot 的读空气与主动对话配置（Bot Profile 的 proactive 段） */
export interface ProactiveDto {
  enabled: boolean
  mode: ProactiveMode
  style_preset: ProactiveStylePreset
  preferred_topics: string[]
  avoid_topics: string[]
  score_threshold: number
  min_gap_seconds: number
  max_per_hour: number
  followup_window_seconds: number
  max_consecutive_turns: number
  mood_influence: number
  quiet_hours_start: number
  quiet_hours_end: number
  bot_collab_max_turns: number
  shared_group_cooldown_seconds: number
}

export function emptyProactive(): ProactiveDto {
  return {
    enabled: false,
    mode: 'off',
    style_preset: 'balanced',
    preferred_topics: [],
    avoid_topics: [],
    score_threshold: 55,
    min_gap_seconds: 300,
    max_per_hour: 3,
    followup_window_seconds: 120,
    max_consecutive_turns: 2,
    mood_influence: 1,
    quiet_hours_start: 1,
    quiet_hours_end: 7,
    bot_collab_max_turns: 2,
    shared_group_cooldown_seconds: 90,
  }
}

export interface BotProfileDto {
  db_id: string
  name: string
  qq_id: string
  aliases: string[]
  meta_prompt: string
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
  /** 旧后端没有这一段；编辑器按 emptyCharacterExperience() 补齐 */
  character_experience?: CharacterExperienceDto
  /** 旧后端没有这一段；编辑器按 emptyProactive() 补齐 */
  proactive?: ProactiveDto
  /** 旧后端没有这一段；编辑器按 emptyBody() 补齐 */
  body?: BodyDto
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
  { id: 'belief_reflection', label: '信念反思' },
  { id: 'character_experience', label: '角色经历' },
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
      preferences: emptyPreferences(),
    },
    mood: emptyMood(),
    character_experience: emptyCharacterExperience(),
    proactive: emptyProactive(),
    body: emptyBody(),
    channels: {},
    tools_allow: [],
    tools_deny: [],
    version: 0,
    origin: 'webui',
  }
}

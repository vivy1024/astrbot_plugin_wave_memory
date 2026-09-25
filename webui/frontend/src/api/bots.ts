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
  model: string
  session_prefix: string
  bindings: BotBindingDto[]
  persona: PersonaDto
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
}

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
    channels: {},
    tools_allow: [],
    tools_deny: [],
    version: 0,
    origin: 'webui',
  }
}

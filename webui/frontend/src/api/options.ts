import { fetchJson } from './client'
import type { ScopeOption } from '@/components/shared/types'

export interface BotOptionDto {
  db_id: string
  name: string
  qq_id?: string
  aliases?: string[]
  status?: string
}

export interface SessionOptionDto {
  id: string
  bot_id: string
  platform_id: string
  kind: string
  conversation_id: string
  group_name?: string
  label: string
  source?: string
  sources?: string[]
  count?: number
  capabilities?: Record<string, number>
  alias_group?: string | null
  is_primary_alias?: boolean
  alias_session_ids?: string[]
}

export interface LegacyGroupOptionDto {
  bot_id: string
  group_id: string
  label: string
  source?: string
  count?: number
  binding_status?: 'unresolved' | string
}

export interface ChannelOptionDto {
  id: string
  enabled: boolean
  source?: string
  trace_count?: number
}

export interface ScopeOptionsPayload {
  bots: BotOptionDto[]
  sessions: SessionOptionDto[]
  legacy_groups?: LegacyGroupOptionDto[]
  channels: ChannelOptionDto[]

  generated_at: number
  source: {
    health: 'healthy' | 'empty' | 'error'
    reason_code: string | null
    providers?: string[]
  }
}

export function getScopeOptions(): Promise<ScopeOptionsPayload> {
  return fetchJson<ScopeOptionsPayload>('/api/options/scopes')
}

export function scopeOptionsFor(payload: ScopeOptionsPayload, kinds: Array<ScopeOption['kind']>): ScopeOption[] {
  const items: ScopeOption[] = []
  if (kinds.includes('bot')) {
    items.push(...payload.bots.map((bot) => ({
      value: bot.db_id,
      label: bot.name || bot.db_id,
      kind: 'bot' as const,
      description: bot.qq_id ? `Bot ID ${bot.db_id} · QQ ${bot.qq_id}` : `Bot ID ${bot.db_id}`,
      disabled: bot.status !== undefined && bot.status !== 'active',
    })))
  }
  if (kinds.includes('session')) {
    items.push(...payload.sessions.map((session) => {
      const groupName = session.group_name?.trim()
      const baseLabel = session.kind === 'group' && groupName && groupName !== session.conversation_id
        ? `${groupName}（${session.conversation_id}）`
        : session.label || session.conversation_id
      const aliasNote = session.kind === 'group' && session.is_primary_alias === false
        ? `同一群 · ${session.platform_id}残留`
        : session.kind === 'group' && (session.alias_session_ids?.length ?? 0) > 1
          ? '同一群主会话'
          : ''
      return {
        value: session.id,
        label: aliasNote && session.is_primary_alias === false ? `${baseLabel} · ${session.platform_id}残留` : baseLabel,
        kind: 'session' as const,
        description: `${session.bot_id} · ${session.kind} · ${session.source ?? 'runtime'}${aliasNote ? ` · ${aliasNote}` : ''}`,
        disabled: session.kind === 'private',
      }
    }))
    for (const group of payload.legacy_groups ?? []) {
      items.push({
        value: `legacy:${group.bot_id}:${group.group_id}`,
        label: `${group.label || group.group_id}（未绑定）`,
        kind: 'session',
        description: `${group.bot_id} · group · unresolved · ${group.count ?? 0} 条人物`,
      })
    }
  }
  if (kinds.includes('channel')) {
    items.push(...payload.channels.map((channel) => ({
      value: channel.id,
      label: channel.id,
      kind: 'channel' as const,
      description: `${channel.enabled ? '已启用' : '已停用'} · ${channel.source ?? 'registry'}`,
      disabled: !channel.enabled,
    })))
  }
  return items
}

export function groupSessionOptions(options: ScopeOption[], botId?: string): ScopeOption[] {
  return options
    .filter((option) => option.kind === 'session' && (!botId || option.description?.startsWith(`${botId} ·`)))
    .map((option) => {
      const isPrivate = option.disabled || option.description?.includes(' · private ·') || option.value.includes(':private:')
      return isPrivate
        ? { ...option, disabled: true, description: `${option.description ?? ''} · 本页只支持群会话` }
        : option
    })
}

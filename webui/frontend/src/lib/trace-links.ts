import type { SessionOptionDto } from '@/api/options'

/**
 * 回复溯源的跳转规则：观测台 trace 条目 → 各对象页；记忆页 → 观测台 trace。
 * 只拼作用域和页面已支持的查询参数，不伪造服务端签发的 ObjectRef。
 */

export interface TraceLinkContext {
  /** BotProfile.db_id（trace 的 bot_profile_id 优先，Cortico trace 的 bot_id 即 db_id）。 */
  botId: string
  /** 与 trace 群号对应的 canonical group session（来自 scope 选项）。 */
  sessionId?: string
}

export interface TraceItemLink {
  to: string
  label: string
  /** true：直接定位到该条目；false：只带作用域/搜索词到对应页面。 */
  precise: boolean
}

type LinkFields = Record<string, unknown>

function asRecord(value: unknown): LinkFields | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as LinkFields : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : typeof value === 'number' && Number.isFinite(value) ? String(value) : ''
}

function href(pathname: string, params: Record<string, string | number | undefined | null>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    query.set(key, String(value))
  }
  const serialized = query.toString()
  return serialized ? `${pathname}?${serialized}` : pathname
}

function scopeParams(ctx: TraceLinkContext): Record<string, string | undefined> {
  return {
    bot_id: ctx.botId || undefined,
    session_id: ctx.sessionId || undefined,
    visibility: ctx.sessionId ? 'group' : undefined,
  }
}

/** 通道名 → 通道配置页对应行（`/channels?channel=<id>`，由通道配置页读取并定位）。 */
export function channelConfigHref(channel: string): string {
  return href('/channels', { channel })
}

/** 记忆页按编号打开详情。 */
export function memoryHref(memoryId: number | string, scope: { botId?: string; sessionId?: string; visibility?: string }): string {
  return href('/memories', {
    bot_id: scope.botId,
    session_id: scope.sessionId,
    visibility: scope.sessionId ? scope.visibility || 'group' : undefined,
    memory_id: memoryId,
  })
}

/** 观测台直接打开某条 trace 详情。 */
export function observatoryTraceHref(traceId: string, botId?: string): string {
  return href('/observatory', { bot_id: botId, trace_id: traceId })
}

/** 在 scope 选项里找 trace 群号对应的 canonical group session。 */
export function sessionForTrace(sessions: SessionOptionDto[] | undefined, botId: string, groupId: unknown): string | undefined {
  const conversation = text(groupId)
  if (!sessions?.length || !botId || !conversation) return undefined
  const candidates = sessions.filter((item) => item.kind === 'group' && item.bot_id === botId && item.conversation_id === conversation)
  return (candidates.find((item) => item.is_primary_alias !== false) ?? candidates[0])?.id
}

function subjectUserId(principal: unknown): string {
  const value = text(principal)
  const marker = ':user:'
  const index = value.lastIndexOf(marker)
  return index >= 0 ? value.slice(index + marker.length) : value
}

/**
 * 通道命中条目 → 目标页链接。``item`` 是详情接口的 hit_items / filtered_items 条目，
 * 其中 ``link`` 字段由后端从原始通道明细里挑出（memory_id、memory_scope、subject、word …）。
 */
export function traceItemLink(channel: string, item: LinkFields, ctx: TraceLinkContext): TraceItemLink | null {
  const link = asRecord(item.link) ?? {}
  const scope = scopeParams(ctx)

  if (link.kind === 'memory' || channel === 'memory' || channel === 'fts5') {
    const memoryId = text(link.memory_id ?? item.id)
    if (!/^\d+$/.test(memoryId)) return null
    const memoryScope = asRecord(link.memory_scope)
    return {
      to: memoryHref(memoryId, {
        botId: text(memoryScope?.bot_id) || ctx.botId,
        sessionId: text(memoryScope?.session_id) || ctx.sessionId,
        visibility: text(memoryScope?.visibility) || 'group',
      }),
      label: `打开记忆 #${memoryId}`,
      precise: true,
    }
  }

  switch (channel) {
    case 'facts': {
      const term = text(link.object) || text(link.subject)
      return { to: href('/facts', { ...scope, search: term }), label: term ? '在事实页查找' : '打开事实页', precise: false }
    }
    case 'belief':
      return { to: href('/beliefs', scope), label: '打开信念页', precise: false }
    case 'jargon': {
      const word = text(link.word ?? item.word)
      return { to: href('/jargon', { ...scope, search: word }), label: word ? `查找黑话「${word}」` : '打开黑话页', precise: false }
    }
    case 'fewshot':
      return { to: href('/knowledge/style-examples', scope), label: '打开风格样例', precise: false }
    case 'book_lore': {
      const title = text(link.title)
      const tab = link.community_id ? 'communities' : link.note_id ? 'notes' : undefined
      return { to: href('/knowledge/book-lore', { tab, search: title }), label: title ? '在书设定中查找' : '打开书设定', precise: false }
    }
    case 'affinity': {
      const userId = subjectUserId(link.subject_principal_id)
      return { to: href('/people', { ...scope, search: userId }), label: userId ? '查看人物关系' : '打开人物页', precise: false }
    }
    case 'soul_state':
      return { to: href('/soul', scope), label: '打开心智状态', precise: false }
    default:
      // persona / holyman_persona / safety 等来自人设包或规则，没有可浏览的对象页
      return null
  }
}

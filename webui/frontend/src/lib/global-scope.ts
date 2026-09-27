import type { ScopeOptionsPayload, SessionOptionDto } from '@/api/options'

/** 页面需要的作用域级别：none 不读 Bot/群；bot 只需要 Bot；session 需要 Bot + 群/会话。 */
export type RouteScopeLevel = 'none' | 'bot' | 'session'

/** 页面额外允许的会话类型；默认只接受已绑定的群会话。 */
export interface RouteScopeAllowances {
  /** 允许私聊会话（例如事实审核）。 */
  private?: boolean
  /** 允许 legacy:<bot>:<group> 形式的未绑定群（例如人物、经历的只读旧数据）。 */
  legacy?: boolean
}

export interface ScopeSelection {
  botId: string
  sessionId: string
  visibility: string
}

export const GLOBAL_SCOPE_STORAGE_KEY = 'wavememory.webui.global-scope.v1'

/** 作用域变化后必须失效的对象级 / 分页参数：旧群的对象引用不能落到新群上。 */
export const SCOPE_DEPENDENT_QUERY_KEYS = ['offset', 'ref', 'object_id', 'subject_principal_id', 'trace_id', 'source_ref', 'target_ref', 'node', 'path_from', 'path_to'] as const

export interface StoredGlobalScope {
  bot_id: string
  session_id: string
  visibility: string
  /** 每个 Bot 最近一次选中的群，切换 Bot 时优先回到该群。 */
  sessions_by_bot: Record<string, string>
}

export function isLegacySessionId(sessionId: string): boolean {
  return sessionId.startsWith('legacy:')
}

export function visibilityForSession(sessionId: string, sessions: SessionOptionDto[] = []): string {
  if (!sessionId) return 'group'
  const known = sessions.find((item) => item.id === sessionId)
  if (known) return known.kind === 'private' ? 'private' : 'group'
  return sessionId.includes(':private:') ? 'private' : 'group'
}

/**
 * 在服务端证实的群会话里挑选 canonical session：
 * 精确命中时把非主别名收敛到主会话；否则按消息量挑选该 Bot 最活跃的主群。
 */
export function preferredSession(sessions: SessionOptionDto[], botId: string, sessionId: string): SessionOptionDto | undefined {
  const exact = sessions.find((item) => item.bot_id === botId && item.id === sessionId)
  if (exact) {
    if (exact.is_primary_alias === false && exact.alias_session_ids?.length) {
      const primary = sessions.find((item) => item.id === exact.alias_session_ids?.[0] && item.bot_id === exact.bot_id)
      return primary ?? exact
    }
    return exact
  }
  const bySession = sessionId ? sessions.find((item) => item.id === sessionId && (!botId || item.bot_id === botId)) : undefined
  if (bySession) return bySession
  const candidates = (botId ? sessions.filter((item) => item.bot_id === botId) : sessions).filter((item) => item.is_primary_alias !== false)
  return [...candidates].sort((left, right) => (right.count ?? 0) - (left.count ?? 0) || left.id.localeCompare(right.id))[0]
}

function isActiveBot(bot: ScopeOptionsPayload['bots'][number]): boolean {
  return bot.status === undefined || bot.status === 'active'
}

function acceptedSession(
  payload: ScopeOptionsPayload,
  botId: string,
  sessionId: string,
  allow: RouteScopeAllowances,
): string | null {
  if (!sessionId) return null
  if (isLegacySessionId(sessionId)) {
    if (!allow.legacy) return null
    const [, legacyBot, ...rest] = sessionId.split(':')
    return legacyBot === botId && rest.join(':') ? sessionId : null
  }
  const known = payload.sessions.find((item) => item.id === sessionId && item.bot_id === botId)
  if (!known) return null
  if (known.kind === 'private') return allow.private ? sessionId : null
  if (known.kind !== 'group') return null
  return preferredSession(payload.sessions.filter((item) => item.kind === 'group'), botId, sessionId)?.id ?? sessionId
}

export interface ResolveScopeDefaultInput {
  payload: ScopeOptionsPayload
  level: RouteScopeLevel
  allow?: RouteScopeAllowances
  current: { botId: string; sessionId: string; visibility: string }
  stored?: StoredGlobalScope | null
}

/**
 * 计算当前路由应当落地的作用域；不需要改动 URL 时返回 null。
 *
 * 优先级：URL 参数（校验并收敛 canonical 别名）> localStorage 上次选择 > 第一个有群的活跃 Bot + 其最活跃群。
 * level=none 的页面永远返回 null，不做任何自动填充。
 */
export function resolveScopeDefault({ payload, level, allow = {}, current, stored }: ResolveScopeDefaultInput): ScopeSelection | null {
  if (level === 'none') return null
  const knownBot = (botId: string) => Boolean(botId) && payload.bots.some((bot) => bot.db_id === botId)
  const groupSessions = payload.sessions.filter((item) => item.kind === 'group')
  const hasGroup = (botId: string) => groupSessions.some((item) => item.bot_id === botId)

  let botId = ''
  let candidates: string[] = []
  if (knownBot(current.botId)) {
    botId = current.botId
    candidates = [current.sessionId, stored?.sessions_by_bot?.[botId] ?? '']
  } else if (!current.botId && current.sessionId) {
    // 只给了会话：用服务端登记的会话归属反推 Bot，不从裸编号猜测。
    const owner = payload.sessions.find((item) => item.id === current.sessionId)
    const legacyBot = isLegacySessionId(current.sessionId) ? current.sessionId.split(':')[1] ?? '' : ''
    botId = owner?.bot_id ?? (knownBot(legacyBot) ? legacyBot : '')
    candidates = [current.sessionId]
  }
  if (!botId && stored && knownBot(stored.bot_id)) {
    botId = stored.bot_id
    candidates = [stored.session_id, stored.sessions_by_bot?.[stored.bot_id] ?? '']
  }
  if (!botId) {
    // 首次进入：选群聊消息量最多的活跃 Bot（通常就是主 Bot），而不是列表里排第一的
    const active = payload.bots.filter(isActiveBot)
    const volume = (id: string) => groupSessions.filter((item) => item.bot_id === id).reduce((sum, item) => sum + (item.count ?? 0), 0)
    const ranked = active.filter((bot) => hasGroup(bot.db_id)).sort((left, right) => volume(right.db_id) - volume(left.db_id))
    botId = (ranked[0] ?? active[0])?.db_id ?? ''
    candidates = []
  }
  if (!botId) return null

  // bot 级页面只保留仍然有效的现有会话，不主动挑群，避免改动与群无关页面的 URL。
  if (level === 'bot') candidates = candidates.slice(0, 1).filter((candidate) => candidate === current.sessionId)
  let sessionId = ''
  for (const candidate of candidates) {
    const accepted = acceptedSession(payload, botId, candidate, allow)
    if (accepted) { sessionId = accepted; break }
  }
  if (!sessionId && level === 'session') {
    sessionId = preferredSession(groupSessions, botId, '')?.id ?? ''
  }

  const visibility = sessionId ? visibilityForSession(sessionId, payload.sessions) : (current.visibility || 'group')
  const currentVisibility = current.visibility || 'group'
  if (botId === current.botId && sessionId === current.sessionId && (!sessionId || visibility === currentVisibility)) return null
  return { botId, sessionId, visibility }
}

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null
  } catch {
    return null
  }
}

export function readStoredScope(): StoredGlobalScope | null {
  try {
    const raw = storage()?.getItem(GLOBAL_SCOPE_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<StoredGlobalScope>
    if (!parsed || typeof parsed.bot_id !== 'string' || !parsed.bot_id) return null
    return {
      bot_id: parsed.bot_id,
      session_id: typeof parsed.session_id === 'string' ? parsed.session_id : '',
      visibility: typeof parsed.visibility === 'string' && parsed.visibility ? parsed.visibility : 'group',
      sessions_by_bot: parsed.sessions_by_bot && typeof parsed.sessions_by_bot === 'object' ? { ...parsed.sessions_by_bot } : {},
    }
  } catch {
    return null
  }
}

export function writeStoredScope(selection: ScopeSelection): void {
  if (!selection.botId) return
  const previous = readStoredScope()
  const sessionsByBot = { ...(previous?.sessions_by_bot ?? {}) }
  if (selection.sessionId) sessionsByBot[selection.botId] = selection.sessionId
  const next: StoredGlobalScope = {
    bot_id: selection.botId,
    session_id: selection.sessionId || sessionsByBot[selection.botId] || '',
    visibility: selection.visibility || 'group',
    sessions_by_bot: sessionsByBot,
  }
  try {
    storage()?.setItem(GLOBAL_SCOPE_STORAGE_KEY, JSON.stringify(next))
  } catch {
    // 隐私模式或配额不足时仅失去“记住上次选择”，不影响 URL 作用域。
  }
}

/**
 * 配置项 → 真正的编辑位置。
 *
 * 同一个配置可能在系统配置、通道配置、Bot 管理三处出现，但真正生效的写入口只有一个。
 * 配置中心「修改」按钮与配置来源页「去修改」按钮共用这里的规则，保证两处跳到同一个地方。
 *
 * 键格式与 /api/config/inventory（services/config/inventory.py）一致：
 *   - `Section.item` / `scalar_key`        schema 静态配置 → 系统配置
 *   - `hot:<param>`                        实时热参数     → 系统配置「实时热参数」
 *   - `channel:<name>.<field>`             注入通道       → 通道配置
 *   - `bot:<db_id>.channels.<name>.<field>` Bot 通道覆盖  → Bot 管理「通道与工具」
 * 特例：
 *   - `Channel_Settings.*` 是通道配置的底层存储，由通道配置页写入 → 通道配置
 *   - `MetaThinking_BotN.*` 是 v5 旧槽位，Bot 首次迁移进 bot_profiles 后注册表不再读取 → Bot 管理
 */

export type ConfigTarget = 'settings' | 'channels' | 'bots'
/** SettingsPage 的标签值：static=保存即生效，hot=实时热参数，restart=需重启参数，advanced=全部高级设置 */
export type SettingsTab = 'static' | 'hot' | 'restart' | 'advanced'
/** BotsPage 编辑器的标签值 */
export type BotEditorTab = 'identity' | 'persona' | 'behavior' | 'advanced'

export interface ConfigLocation {
  target: ConfigTarget
  pathname: '/settings' | '/channels' | '/bots'
  /** 查询参数；settings: key + tab，channels: channel (+ field)，bots: bot (+ tab) */
  params: Record<string, string>
  /** 给用户看的位置描述，如「系统配置 · 需重启参数」 */
  label: string
  /** 额外说明，如旧槽位改了不生效 */
  note?: string
}

export interface LocateContext {
  /** 该项的生效方式（schema / inventory 的 apply_mode），用于决定系统配置落在哪个标签 */
  applyMode?: string | null
  /** 旧槽位 → Bot db_id，例如 { MetaThinking_Bot1: 'yushu' }，来自 MetaThinking_BotN.db_id 的当前值 */
  legacyBotIds?: Record<string, string>
}

const SETTINGS_TAB_LABELS: Record<SettingsTab, string> = {
  static: '保存即生效',
  hot: '实时热参数',
  restart: '需重启参数',
  advanced: '全部高级设置',
}

const BOT_TAB_LABELS: Record<BotEditorTab, string> = {
  identity: '身份与绑定',
  persona: '人设',
  behavior: '行为',
  advanced: '通道与工具',
}

/** 旧槽位字段在 Bot 编辑器里的对应标签（见 BotsPage 的 BotEditor）。 */
const LEGACY_BOT_FIELD_TABS: Record<string, BotEditorTab> = {
  qq_id: 'identity',
  name: 'identity',
  db_id: 'identity',
  aliases: 'identity',
  exclude_sources: 'behavior',
  interest_keywords: 'behavior',
  meta_prompt: 'behavior',
  model: 'behavior',
  proactive_enabled: 'behavior',
  proactive_interval_seconds: 'behavior',
  proactive_max_per_hour: 'behavior',
}

export const LEGACY_BOT_SLOT = /^MetaThinking_Bot\d+$/

/** 系统配置里某项落在哪个标签：与 SettingsPage 的过滤规则一致，未知时落到「全部高级设置」。 */
export function settingsTabFor(applyMode?: string | null): SettingsTab {
  if (applyMode === 'restart') return 'restart'
  if (applyMode === 'next_run' || applyMode === 'service') return 'static'
  // schema 里 apply_mode=hot 的项（如 Query_Settings.min_similarity）在 SettingsPage 只出现在「全部高级设置」。
  return 'advanced'
}

function settingsLocation(key: string, tab: SettingsTab): ConfigLocation {
  return {
    target: 'settings',
    pathname: '/settings',
    params: { key, tab },
    label: `系统配置 · ${SETTINGS_TAB_LABELS[tab]}`,
  }
}

function botLocation(dbId: string | undefined, tab: BotEditorTab | undefined, note?: string): ConfigLocation {
  const params: Record<string, string> = {}
  if (dbId) params.bot = dbId
  if (tab) params.tab = tab
  const parts = ['Bot 管理']
  if (dbId) parts.push(dbId)
  if (tab) parts.push(BOT_TAB_LABELS[tab])
  return { target: 'bots', pathname: '/bots', params, label: parts.join(' · '), ...(note ? { note } : {}) }
}

export function locateConfig(rawKey: string, context: LocateContext = {}): ConfigLocation {
  const key = rawKey.trim()

  if (key.startsWith('hot:')) return settingsLocation(key.slice(4), 'hot')

  if (key.startsWith('channel:')) {
    const rest = key.slice('channel:'.length)
    const dot = rest.lastIndexOf('.')
    const channel = dot > 0 ? rest.slice(0, dot) : rest
    const field = dot > 0 ? rest.slice(dot + 1) : ''
    return {
      target: 'channels',
      pathname: '/channels',
      params: field ? { channel, field } : { channel },
      label: `通道配置 · ${channel}`,
    }
  }

  if (key.startsWith('bot:')) {
    const rest = key.slice('bot:'.length)
    const dot = rest.indexOf('.')
    const dbId = dot > 0 ? rest.slice(0, dot) : rest
    const tail = dot > 0 ? rest.slice(dot + 1) : ''
    return botLocation(dbId || undefined, tail.startsWith('channels.') ? 'advanced' : undefined)
  }

  const dot = key.indexOf('.')
  const section = dot > 0 ? key.slice(0, dot) : key
  const field = dot > 0 ? key.slice(dot + 1) : ''

  if (section === 'Channel_Settings') {
    return {
      target: 'channels',
      pathname: '/channels',
      params: {},
      label: '通道配置',
      note: '这是通道配置的底层存储，请在通道配置页修改，不要手写 JSON。',
    }
  }

  if (LEGACY_BOT_SLOT.test(section)) {
    const dbId = context.legacyBotIds?.[section]?.trim() || undefined
    return botLocation(
      dbId,
      LEGACY_BOT_FIELD_TABS[field],
      `${section} 是旧版静态槽位，只在 Bot 首次迁移进 Bot 管理时读取一次；之后改这里不会生效。`,
    )
  }

  return settingsLocation(key, settingsTabFor(context.applyMode))
}

/** 独立路由下的跳转地址，例如 `/settings?key=Query_Settings.inject_top_k&tab=static`。 */
export function locationHref(location: ConfigLocation): string {
  const search = new URLSearchParams(location.params).toString()
  return search ? `${location.pathname}?${search}` : location.pathname
}

/** 从配置快照里收集旧槽位 → db_id，供 locateConfig 使用。 */
export function legacyBotIdsFrom(entries: Iterable<{ key: string; effective: unknown }>): Record<string, string> {
  const ids: Record<string, string> = {}
  for (const entry of entries) {
    const match = /^(MetaThinking_Bot\d+)\.db_id$/.exec(entry.key)
    if (match && typeof entry.effective === 'string' && entry.effective.trim()) ids[match[1]] = entry.effective.trim()
  }
  return ids
}

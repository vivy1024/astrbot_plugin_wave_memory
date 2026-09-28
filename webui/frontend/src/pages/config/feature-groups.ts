/**
 * 「按功能」分组规则：用户是按症状找配置（「回复太长」「召回不准」「标签太多」），
 * 不是按生效方式或 schema 章节找。
 *
 * 判定顺序（先命中先用）：
 *   1. KEY_FEATURE     单键覆盖（章节里个别键更属于别的功能，如 Inject_Settings.facts_max → 书设与知识）
 *   2. 键前缀           hot:<前缀>.x 按 HOT_PREFIX_FEATURE；channel:/bot: → 回复与人格
 *   3. SECTION_FEATURE schema 章节（_conf_schema.json 顶层键）；MetaThinking_BotN 统一按对话规则处理
 *   4. 兜底 other      未登记的新章节不会从页面上消失
 */
import type { ConfigEntry } from '@/pages/config/config-catalog'

export type FeatureId = 'reply' | 'mind' | 'recall' | 'learning' | 'lore' | 'index' | 'security' | 'maintenance' | 'other'

export interface FeatureGroup {
  id: FeatureId
  title: string
  /** 一句话：这组管什么 */
  description: string
  /** 用户会怎么描述问题；参与搜索 */
  symptoms: string[]
  /** 相关页面入口（非配置，但常一起看） */
  links?: Array<{ label: string; to: string }>
  defaultOpen: boolean
}

export const FEATURE_GROUPS: FeatureGroup[] = [
  {
    id: 'reply',
    title: '回复与人格',
    description: 'Bot 回复时带多少上下文、按什么规则说话、用什么风格和口癖、多久主动插话一次。',
    symptoms: ['回复太长', '回复太短', '说话不像', '主动插话太多', '刷屏', '深夜还说话', '黑话', '风格'],
    links: [{ label: '通道配置', to: '/channels' }, { label: 'Bot 管理', to: '/bots' }],
    defaultOpen: true,
  },
  {
    id: 'mind',
    title: '心智与社交',
    description: 'Bot 的心情、好感变化幅度、被骚扰时的冷却，以及深夜做梦的频率。',
    symptoms: ['情绪', '好感涨太快', '好感掉太快', '被骂', '冷却', '做梦'],
    defaultOpen: false,
  },
  {
    id: 'recall',
    title: '记忆召回',
    description: '回复时从记忆里找哪些内容、找多准、跨不跨群。',
    symptoms: ['召回不准', '记不住', '想不起来', '答非所问', '串群', '翻旧账'],
    links: [{ label: '注入观测台', to: '/observatory' }],
    defaultOpen: true,
  },
  {
    id: 'learning',
    title: '标签与学习',
    description: '哪些消息会被记下来，怎么从聊天里提炼标签，以及自省纠错开关。',
    symptoms: ['标签太多', '标签太少', '标签不准', '记了没用的话', '某个群不想被记'],
    links: [{ label: '标签', to: '/tags' }],
    defaultOpen: true,
  },
  {
    id: 'lore',
    title: '书设与知识',
    description: '书设语料库的位置，以及事实注入的条数与衰减。',
    symptoms: ['书设', '设定', '事实', '知识'],
    links: [{ label: '书设工作台', to: '/knowledge/book-lore/workbench' }],
    defaultOpen: false,
  },
  {
    id: 'index',
    title: '性能与索引',
    description: '向量模型、检索索引容量、内存预算与写入批处理——决定能不能搜到、多快、占多少内存。',
    symptoms: ['内存太高', '卡顿', '慢', '搜不到', '索引重建', '向量模型'],
    links: [{ label: '索引诊断', to: '/diagnostics/indexes' }],
    defaultOpen: false,
  },
  {
    id: 'security',
    title: 'WebUI 与安全',
    description: '控制台监听地址、端口与密码，整机运行级别，以及与其他记忆插件的共存方式。',
    symptoms: ['打不开控制台', '端口', '密码', '局域网访问', '重复注入'],
    defaultOpen: false,
  },
  {
    id: 'maintenance',
    title: '维护与备份',
    description: '自动备份保留几份、低价值记忆多久清理、观测记录留多少。',
    symptoms: ['备份', '磁盘满', '清理', '淘汰', '观测记录'],
    links: [{ label: '维护任务', to: '/maintenance' }, { label: '服务与扩展', to: '/services' }],
    defaultOpen: false,
  },
  {
    id: 'other',
    title: '其他设置',
    description: '尚未归类的配置项；新加的配置会先出现在这里。',
    symptoms: [],
    defaultOpen: true,
  },
]

export const SECTION_FEATURE: Record<string, FeatureId> = {
  Inject_Settings: 'reply',
  MetaThinking_Settings: 'reply',
  FewShot_Settings: 'reply',
  Jargon_Settings: 'reply',
  Channel_Settings: 'reply',
  llm_fallback_provider_ids: 'reply',
  Lifecycle_Settings: 'mind',
  Social_Settings: 'mind',
  Affinity_Settings: 'mind',
  Query_Settings: 'recall',
  Cross_Group_Settings: 'recall',
  Tag_Settings: 'learning',
  TagWorker_Settings: 'learning',
  Study_Settings: 'learning',
  Learning_Settings: 'learning',
  Message_Filter: 'learning',
  tag_llm_provider_id: 'learning',
  BookLore_Settings: 'lore',
  embedding_provider_id: 'index',
  embedding_dimension: 'index',
  Memory_Index_Settings: 'index',
  Memory_Budget_Settings: 'index',
  Storage_Settings: 'index',
  Performance_Settings: 'index',
  WebUI_Settings: 'security',
  Runtime_Settings: 'security',
  Compatibility_Settings: 'security',
  backup_max_count: 'maintenance',
  Eviction_Settings: 'maintenance',
  Trace_Settings: 'maintenance',
}

export const KEY_FEATURE: Record<string, FeatureId> = {
  'Inject_Settings.facts_max': 'lore',
  'Storage_Settings.facts_decay_rate': 'lore',
  'Memory_Index_Settings.cold_recall_enabled': 'recall',
  'Memory_Index_Settings.cold_candidate_limit': 'recall',
  'Memory_Index_Settings.chat_hot_days': 'recall',
}

/** 热参数 `hot:<前缀>.<名>` 的前缀归属。 */
export const HOT_PREFIX_FEATURE: Record<string, FeatureId> = {
  spike: 'recall',
  query: 'recall',
  geodesic: 'recall',
  residual: 'recall',
  social: 'mind',
  ingress: 'reply',
  injection: 'maintenance',
}

/** 单键的症状词，让「回复太长」能直接搜到具体的键。 */
export const KEY_SYMPTOMS: Record<string, string[]> = {
  'Query_Settings.inject_top_k': ['回复太长', '记忆太多', '召回太少'],
  'Query_Settings.min_similarity': ['召回不准', '答非所问', '召回太少'],
  'hot:query.min_similarity': ['召回不准', '答非所问', '召回太少'],
  'Query_Settings.enable_auto_inject': ['记不住', '不带记忆'],
  'Inject_Settings.facts_max': ['回复太长'],
  'Inject_Settings.soul_timeline_max_items': ['回复太长'],
  'Inject_Settings.skip_recent_minutes': ['复读', '重复刚说的'],
  'Jargon_Settings.max_inject': ['回复太长', '黑话太多'],
  'FewShot_Settings.max_inject': ['回复太长', '说话不像'],
  'Cross_Group_Settings.cross_group_enabled': ['串群', '隐私'],
  'hot:query.group_weight_cross': ['串群'],
  'Tag_Settings.max_tags_per_message': ['标签太多', '标签太少'],
  'Tag_Settings.tag_batch_size': ['标签太慢'],
  'Message_Filter.max_message_length': ['记了没用的话'],
  'Message_Filter.ignore_bot_messages': ['记了没用的话'],
  'Query_Settings.enable_shotgun': ['召回不准', '答非所问'],
  'MetaThinking_Settings.spam_window_seconds': ['刷屏'],
  'Lifecycle_Settings.relationship_daily_delta_cap': ['好感涨太快', '好感掉太快'],
  'Lifecycle_Settings.relationship_single_delta_cap': ['好感涨太快', '好感掉太快'],
  'Social_Settings.abuse_cooldown_base': ['被骂', '冷却'],
  'Lifecycle_Settings.dream_interval_hours': ['做梦'],
  'Eviction_Settings.noise_ttl_days': ['清理', '磁盘满'],
  'Trace_Settings.max_rows': ['观测记录', '磁盘满'],
  'Tag_Settings.tag_blacklist': ['标签太多', '标签不准'],
  'Tag_Settings.tag_extraction_enabled': ['没有标签', '标签太少'],
  'Message_Filter.min_message_length': ['记了没用的话'],
  'Message_Filter.group_blacklist': ['某个群不想被记'],
  'MetaThinking_Settings.spam_threshold': ['刷屏'],
  'MetaThinking_Settings.silent_hours_start': ['深夜还说话'],
  'MetaThinking_Settings.silent_hours_end': ['深夜还说话'],
  'Memory_Index_Settings.hot_max_vectors': ['内存太高', '记不住'],
  'Memory_Budget_Settings.memory_budget_mb': ['内存太高'],
  'embedding_provider_id': ['搜不到', '向量模型'],
  'WebUI_Settings.webui_host': ['局域网访问', '打不开控制台'],
  'WebUI_Settings.webui_password': ['密码'],
  'backup_max_count': ['备份', '磁盘满'],
}

const GROUP_BY_ID = new Map(FEATURE_GROUPS.map((group) => [group.id, group]))

export function featureGroup(id: FeatureId): FeatureGroup {
  return GROUP_BY_ID.get(id) ?? GROUP_BY_ID.get('other')!
}

export function featureOf(key: string): FeatureId {
  const override = KEY_FEATURE[key]
  if (override) return override
  if (key.startsWith('hot:')) {
    const prefix = key.slice(4).split('.')[0]
    return HOT_PREFIX_FEATURE[prefix] ?? 'other'
  }
  if (key.startsWith('channel:') || key.startsWith('bot:')) return 'reply'
  const section = key.split('.')[0]
  if (/^MetaThinking_Bot\d+$/.test(section)) return 'reply'
  return SECTION_FEATURE[section] ?? 'other'
}

export interface FeatureBucket {
  group: FeatureGroup
  entries: ConfigEntry[]
  changedCount: number
}

/** 按 FEATURE_GROUPS 的顺序分桶，组内保持后端返回顺序；空组不返回。 */
export function groupByFeature(entries: ConfigEntry[]): FeatureBucket[] {
  const buckets = new Map<FeatureId, ConfigEntry[]>()
  for (const entry of entries) {
    const id = featureOf(entry.key)
    const list = buckets.get(id) ?? []
    list.push(entry)
    buckets.set(id, list)
  }
  return FEATURE_GROUPS.filter((group) => buckets.has(group.id)).map((group) => {
    const list = buckets.get(group.id) ?? []
    return { group, entries: list, changedCount: list.filter((entry) => entry.changed).length }
  })
}

export interface SearchHit {
  entry: ConfigEntry
  group: FeatureGroup
  /** 命中的是症状词或分组名（而不是键/名称/说明） */
  bySymptom: boolean
  /** 命中的症状词 */
  symptom?: string
}

function normalize(text: string): string {
  return text.toLocaleLowerCase().replace(/\s+/g, '')
}

/**
 * 跨所有配置键 / 名称 / 说明 / 分组名 / 症状词搜索。
 * 直接命中键、名称或说明的排在前面，只靠症状词命中的排在后面。
 */
export function searchConfig(entries: ConfigEntry[], term: string, limit = 60): SearchHit[] {
  const needle = normalize(term)
  if (!needle) return []
  const direct: SearchHit[] = []
  const bySymptom: SearchHit[] = []
  for (const entry of entries) {
    const group = featureGroup(featureOf(entry.key))
    const text = normalize([entry.key, entry.title, entry.hint, entry.section, entry.mirrorOf ?? ''].join(' '))
    if (text.includes(needle)) {
      direct.push({ entry, group, bySymptom: false })
      continue
    }
    const matches = (word: string) => {
      const normalized = normalize(word)
      return normalized.includes(needle) || needle.includes(normalized)
    }
    const symptom = (KEY_SYMPTOMS[entry.key] ?? []).find(matches)
    // 只按键自己的症状词或分组名命中；分组级症状词只作页面提示，不把整组无关项带出来。
    if (symptom || normalize(group.title).includes(needle)) bySymptom.push({ entry, group, bySymptom: true, ...(symptom ? { symptom } : {}) })
  }
  return [...direct, ...bySymptom].slice(0, limit)
}

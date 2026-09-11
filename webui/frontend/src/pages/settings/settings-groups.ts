/**
 * 系统配置页的展示层映射。
 *
 * 后端 schema 的章节名（Memory_Index_Settings、Storage_Settings…）与取值范围里的
 * 术语（plugin_config、runtime_startup_snapshot、HNSW、canonical…）是给开发者看的，
 * 直接渲染到配置页会让用户面对数据库 schema。这里集中做三层收敛：
 *   1. 章节 → 用户视角的功能分组，并标注常用/高级
 *   2. 内部来源取值 → 人话
 *   3. 分组默认展开策略
 * 不改动任何配置值本身，也不改后端契约。
 */

export interface SettingsGroupMeta {
  /** 用户视角的分组名 */
  title: string
  /** 一句话说明这组管什么 */
  description: string
  /** 默认是否展开；只有最常调的组默认展开 */
  defaultOpen: boolean
  /** 该组归入哪个 tab：'static' = 保存即生效，'restart' = 需重启，'always' = 两个 tab 都显示 */
  bucket: 'static' | 'restart' | 'always'
}

/**
 * schema 章节 key → 功能分组。
 * 未登记的章节回退为「其他设置」，保证新加的章节不会从页面上消失。
 */
export const SECTION_GROUPS: Record<string, SettingsGroupMeta> = {
  Query_Settings: {
    title: '记忆召回',
    description: 'Bot 回复时自动带上哪些记忆、带多少条',
    defaultOpen: true,
    bucket: 'always',
  },
  Memory_Index_Settings: {
    title: '记忆检索索引',
    description: '决定记忆能被搜到多快、多准，以及热冷分层',
    defaultOpen: true,
    bucket: 'always',
  },
  Inject_Settings: {
    title: '回复注入',
    description: '注入内容的篇幅与时间窗口',
    defaultOpen: true,
    bucket: 'always',
  },
  Tag_Settings: {
    title: '标签与分类',
    description: '自动从聊天里提炼标签的参数，含标签图谱图例',
    defaultOpen: true,
    bucket: 'always',
  },
  Message_Filter: {
    title: '闲聊过滤',
    description: '先过滤掉无意义消息，避免污染记忆',
    defaultOpen: false,
    bucket: 'always',
  },
  Lifecycle_Settings: {
    title: '心智与情绪',
    description: 'Bot 的心情、关切、作息等内在状态',
    defaultOpen: false,
    bucket: 'always',
  },
  MetaThinking_Settings: {
    title: '对话规则',
    description: '全局的发言规则与防骚扰策略',
    defaultOpen: false,
    bucket: 'always',
  },
  MetaThinking_Bot1: {
    title: '对话规则 · Bot 1',
    description: '仅对 Bot 1 生效的覆盖配置',
    defaultOpen: false,
    bucket: 'always',
  },
  MetaThinking_Bot2: {
    title: '对话规则 · Bot 2',
    description: '仅对 Bot 2 生效的覆盖配置',
    defaultOpen: false,
    bucket: 'always',
  },
  Cross_Group_Settings: {
    title: '跨群共享',
    description: '记忆与画像是否跨群生效、以及授权范围',
    defaultOpen: false,
    bucket: 'always',
  },
  Jargon_Settings: {
    title: '黑话与口癖',
    description: '群内黑话的发现与使用',
    defaultOpen: false,
    bucket: 'always',
  },
  FewShot_Settings: {
    title: '风格示范',
    description: '用已审核的回复范例影响说话风格',
    defaultOpen: false,
    bucket: 'always',
  },
  Study_Settings: {
    title: '自主学习',
    description: '沉淀对话经验的开关与频率',
    defaultOpen: false,
    bucket: 'always',
  },
  Eviction_Settings: {
    title: '记忆淘汰',
    description: '记忆过多时按什么规则降级或清理',
    defaultOpen: false,
    bucket: 'always',
  },
  Memory_Budget_Settings: {
    title: '内存占用',
    description: '常驻内存的总预算，防止占满机器',
    defaultOpen: false,
    bucket: 'always',
  },
  Storage_Settings: {
    title: '存储容量',
    description: '数据库能保存多少记忆',
    defaultOpen: false,
    bucket: 'always',
  },
  Trace_Settings: {
    title: '观测数据留存',
    description: '注入观测台保留多少条运行记录',
    defaultOpen: false,
    bucket: 'always',
  },
  WebUI_Settings: {
    title: '控制台',
    description: '本页所在的 WebUI 自身的开关与端口',
    defaultOpen: false,
    bucket: 'restart',
  },
  Runtime_Settings: {
    title: '运行能力',
    description: '整机层面的能力边界',
    defaultOpen: false,
    bucket: 'always',
  },
  Compatibility_Settings: {
    title: '插件兼容',
    description: '与其他记忆插件的共存方式',
    defaultOpen: false,
    bucket: 'restart',
  },
  Social_Settings: {
    title: '社交与人际防骚扰',
    description: '防骚扰冷却、连续对话时间窗、社交能量沉淀上限与跨群权重',
    defaultOpen: false,
    bucket: 'always',
  },
  Performance_Settings: {
    title: '性能与批处理调优',
    description: '向量化批处理大小与数据库异步写入缓冲间隔',
    defaultOpen: false,
    bucket: 'restart',
  },
  BookLore_Settings: {
    title: '书设存储路径',
    description: '只读语料数据库的外部挂载路径配置',
    defaultOpen: false,
    bucket: 'restart',
  },
  Channel_Settings: {
    title: '注入通道底层配置',
    description: '底层各注入通道的自定义优先级序列',
    defaultOpen: false,
    bucket: 'always',
  },
  Affinity_Settings: {
    title: '好感度校准边界',
    description: '控制台人工校准好感时的单次幅度上限',
    defaultOpen: false,
    bucket: 'always',
  },
  TagWorker_Settings: {
    title: '标签后台队列',
    description: '后台异步标签提取与分类任务工作机制',
    defaultOpen: false,
    bucket: 'always',
  },
  // ---- 顶层标量字段：这些是最该先配的，必须单独成组而不是落进「其他设置」 ----
  embedding_provider_id: {
    title: '向量模型',
    description: '把文字转成向量的模型，决定记忆能不能被语义搜到',
    defaultOpen: true,
    bucket: 'restart',
  },
  embedding_dimension: {
    title: '向量维度',
    description: '必须与上面所选模型一致，填错会导致记忆全部搜不到',
    defaultOpen: true,
    bucket: 'restart',
  },
  tag_llm_provider_id: {
    title: '标签分析模型',
    description: '用于从聊天里提炼标签的大模型',
    defaultOpen: true,
    bucket: 'restart',
  },
  llm_fallback_provider_ids: {
    title: '模型回退链',
    description: '主模型不可用时按顺序尝试的备用模型',
    defaultOpen: false,
    bucket: 'restart',
  },
  backup_max_count: {
    title: '备份保留数量',
    description: '自动整库备份最多保留几份',
    defaultOpen: false,
    bucket: 'always',
  },
}

/** 不渲染的 schema 键：纯说明文本，不是配置项。 */
const HIDDEN_SECTIONS = new Set(['_system_status'])

/**
 * 界面上隐藏的具体配置字段：
 * 这些字段在历史版本中存在，为避免旧 config.json 报未知字段而在 schema 中保留，
 * 但对应的后台任务已下线，渲染在前端会导致误导。
 */
const HIDDEN_ITEMS = new Set([
  'Lifecycle_Settings.enable_consolidation',
  'Lifecycle_Settings.consolidation_interval_hours',
  'Lifecycle_Settings.consolidation_topic_backfill',
  'Tag_Settings.consolidation_skip_topics',
])

export function isHiddenSection(sectionKey: string): boolean {
  return HIDDEN_SECTIONS.has(sectionKey)
}

export function isHiddenItem(sectionKey: string, itemKey: string): boolean {
  return HIDDEN_ITEMS.has(`${sectionKey}.${itemKey}`)
}

const FALLBACK_GROUP: SettingsGroupMeta = {
  title: '其他设置',
  description: '未归类到这个页面的配置项',
  // 未登记的章节默认展开：宁可多显示，也不能让新配置静默消失在折叠区里。
  defaultOpen: true,
  bucket: 'always',
}

export function groupMeta(sectionKey: string): SettingsGroupMeta {
  return SECTION_GROUPS[sectionKey] ?? FALLBACK_GROUP
}

export interface SectionGroup<T> {
  key: string
  meta: SettingsGroupMeta
  groups: T[]
  itemCount: number
}

/** 按功能分组名、章节说明与字段名/说明做匹配，保持后端返回顺序。 */
export function matchesSearch(
  meta: SettingsGroupMeta,
  sectionKey: string,
  group: { description?: string; hint?: string },
  items: Array<{ key: string; description?: string; hint?: string }>,
  term: string,
): boolean {
  const needle = term.trim().toLocaleLowerCase()
  if (!needle) return true
  const haystack = [
    meta.title,
    meta.description,
    sectionKey,
    group.description ?? '',
    group.hint ?? '',
    ...items.flatMap((item) => [item.key, item.description ?? '', item.hint ?? '']),
  ]
    .join(' ')
    .toLocaleLowerCase()
  return haystack.includes(needle)
}

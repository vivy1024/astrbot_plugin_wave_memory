/**
 * 高风险配置表：改了要重启、会影响已有数据、或会把控制台暴露出去的键。
 * 「高风险」标签只读展示这张表，编辑一律跳转到 config-location 定位的真实入口。
 */
import { sameValue, type ConfigEntry } from '@/pages/config/config-catalog'

export type RiskStatus = 'danger' | 'warn' | 'deviated' | 'normal' | 'missing'

export interface RiskCheckResult {
  status: 'danger' | 'warn'
  message: string
}

export type ValueLookup = (key: string) => unknown

export interface RiskRule {
  key: string
  /** 为什么列为高风险 */
  reason: string
  /** 改了之后会怎样 */
  impact: string
  /** 是否需要重启 AstrBot 才生效 */
  restart: boolean
  /** 是否会影响已有数据（索引、记忆条目、备份） */
  affectsData: boolean
  /** 默认值只是占位（如 Provider 默认空），偏离默认本身不算异常 */
  deviationExpected?: boolean
  /** 危险状态判定：返回后果说明；null 表示当前值没有额外问题 */
  check?: (value: unknown, lookup: ValueLookup) => RiskCheckResult | null
}

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

export function isLoopbackHost(host: unknown): boolean {
  if (typeof host !== 'string') return true
  const value = host.trim().toLowerCase()
  // 空值由后端回落到默认 127.0.0.1。
  return value === '' || LOOPBACK_HOSTS.has(value) || value.startsWith('127.')
}

function isBlank(value: unknown): boolean {
  return value === null || value === undefined || (typeof value === 'string' && value.trim() === '')
}

export const RISK_RULES: RiskRule[] = [
  {
    key: 'WebUI_Settings.webui_host',
    reason: '决定控制台监听哪块网卡；控制台能读写全部记忆、配置并触发维护任务。',
    impact: '改成 127.0.0.1 以外的地址后，同一网络内其他设备都能访问控制台与 Runtime API。',
    restart: true,
    affectsData: false,
    check: (value, lookup) => {
      if (isLoopbackHost(value)) return null
      if (isBlank(lookup('WebUI_Settings.webui_password'))) {
        return { status: 'danger', message: `当前监听 ${String(value)} 且未设置访问密码：同一网络内任何设备无需登录即可打开控制台，读取、改写或删除记忆与配置。` }
      }
      return { status: 'warn', message: `当前监听 ${String(value)}，已设置密码；控制台走 HTTP 明文，请确认所在网络可信。` }
    },
  },
  {
    key: 'WebUI_Settings.webui_password',
    reason: '控制台唯一的访问凭证。',
    impact: '清空后，只要监听地址不是本机，就不再需要登录。',
    restart: false,
    affectsData: false,
    deviationExpected: true,
    check: (value, lookup) => {
      const host = lookup('WebUI_Settings.webui_host')
      if (isBlank(value) && !isLoopbackHost(host)) {
        return { status: 'danger', message: `密码为空，而控制台监听 ${String(host)}：等同于对局域网开放无密码管理权限。` }
      }
      return null
    },
  },
  {
    key: 'WebUI_Settings.webui_port',
    reason: '控制台与 Runtime API 的端口，外部调用方按它连接。',
    impact: '改后需用新端口访问；写死 9876 的脚本、Cortico 扩展会连不上。',
    restart: true,
    affectsData: false,
  },
  {
    key: 'WebUI_Settings.webui_enabled',
    reason: '关闭后控制台整体下线。',
    impact: '只能回 AstrBot 插件配置页改回；依赖 9876 的 Runtime API 调用同时失效。',
    restart: true,
    affectsData: false,
    check: (value) => value === false ? { status: 'danger', message: '控制台已配置为关闭，重启后本页面将无法访问。' } : null,
  },
  {
    key: 'embedding_provider_id',
    reason: '所有记忆向量都由这个模型生成，检索时也用它编码查询。',
    impact: '换模型后旧向量与新查询不在同一空间，召回会大面积失准，需要重新向量化并重建索引；留空则语义检索不可用。',
    restart: true,
    affectsData: true,
    deviationExpected: true,
    check: (value) => isBlank(value) ? { status: 'danger', message: '未指定向量模型：新记忆无法向量化，语义召回不可用。' } : null,
  },
  {
    key: 'embedding_dimension',
    reason: '索引按这个维度建立，必须与向量模型的输出维度一致。',
    impact: '填错会导致向量写入失败或全部记忆搜不到；改动后需重建索引。',
    restart: true,
    affectsData: true,
  },
  {
    key: 'tag_llm_provider_id',
    reason: '标签提炼、自省等后台 LLM 调用都走这个模型。',
    impact: '换模型会改变标签风格、质量与调用成本；留空时标签提炼停摆。',
    restart: true,
    affectsData: false,
    deviationExpected: true,
    check: (value, lookup) => isBlank(value) && lookup('Tag_Settings.tag_extraction_enabled') !== false
      ? { status: 'danger', message: '标签提炼已开启，但没有指定标签分析模型。' }
      : null,
  },
  {
    key: 'Memory_Index_Settings.hot_max_vectors',
    reason: '常驻内存的热记忆索引容量。',
    impact: '调高会按比例增加内存占用与索引重建耗时，可能超出内存预算；调低会让更多记忆降为冷记忆，只能靠标签召回。',
    restart: true,
    affectsData: true,
  },
  {
    key: 'Memory_Index_Settings.tag_index_max_vectors',
    reason: '标签索引容量，与热记忆索引一起占用常驻内存。',
    impact: '调高增加内存与重建耗时；调低会让部分标签无法参与召回。',
    restart: true,
    affectsData: true,
  },
  {
    key: 'Memory_Budget_Settings.memory_budget_mb',
    reason: '索引与缓存的常驻内存总预算。',
    impact: '过低会让索引重建被拒绝或降级；过高可能让整机内存吃紧。',
    restart: true,
    affectsData: false,
  },
  {
    key: 'Storage_Settings.max_memories',
    reason: '活跃记忆的软上限，超出后按策略降级。',
    impact: '调低会让更多普通聊天被冷落库、不再参与常规召回。',
    restart: true,
    affectsData: true,
  },
  {
    key: 'Runtime_Settings.runtime_mode',
    reason: '整机运行级别，决定哪些能力被启用。',
    impact: 'memory_only 关闭信念、事实等高级认知；compat_only 关闭原生主动注入。',
    restart: true,
    affectsData: false,
    check: (value) => typeof value === 'string' && value !== '' && value !== 'full'
      ? { status: 'warn', message: `当前为 ${value}：部分认知能力或原生注入已关闭。` }
      : null,
  },
  {
    key: 'Query_Settings.enable_auto_inject',
    reason: '回复时自动带上记忆的总开关。',
    impact: '关闭后 Bot 回复不再带任何记忆，表现为「什么都记不住」。',
    restart: false,
    affectsData: false,
    check: (value) => value === false ? { status: 'danger', message: '自动注入已关闭：Bot 回复时不会带任何记忆。' } : null,
  },
  {
    key: 'Cross_Group_Settings.cross_group_enabled',
    reason: '记忆是否跨群生效，涉及群与群之间的隐私边界。',
    impact: '开启后一个群里聊过的内容可能出现在另一个群的回复里。',
    restart: false,
    affectsData: false,
  },
  {
    key: 'Compatibility_Settings.compat_only_auto_inject_enabled',
    reason: '兼容模式下强行启用原生注入。',
    impact: '与 SelfLearning / ChatPlus 等同类插件共存时，同一轮聊天可能被重复注入记忆。',
    restart: true,
    affectsData: false,
    check: (value, lookup) => value === true && lookup('Runtime_Settings.runtime_mode') === 'compat_only'
      ? { status: 'warn', message: '兼容模式下已强行开启原生注入，请确认没有其他记忆插件在同时注入。' }
      : null,
  },
  {
    key: 'Eviction_Settings.enabled',
    reason: '定期清理低价值记忆的后台作业。',
    impact: '开启时超期噪声记忆会被删除；关闭后数据库只增不减。',
    restart: false,
    affectsData: true,
  },
  {
    key: 'Eviction_Settings.noise_ttl_days',
    reason: '噪声记忆超过这个天数会被删除。',
    impact: '调低会更早删除噪声记忆，删除后无法从控制台恢复，只能从备份找回。',
    restart: false,
    affectsData: true,
    check: (value, lookup) => typeof value === 'number' && value < 7 && lookup('Eviction_Settings.enabled') !== false
      ? { status: 'warn', message: `噪声记忆只保留 ${value} 天，比默认 7 天更早被删除。` }
      : null,
  },
  {
    key: 'backup_max_count',
    reason: '自动整库备份保留份数。',
    impact: '份数越少回滚余地越小；份数越多占用磁盘越多（整库体积较大）。',
    restart: false,
    affectsData: true,
  },
  {
    key: 'Channel_Settings.layers',
    reason: '注入通道配置的底层存储。',
    impact: '手写内容不合法时通道配置解析失败，全部通道回退为默认值。',
    restart: false,
    affectsData: false,
  },
]

export interface RiskAssessment {
  rule: RiskRule
  entry: ConfigEntry | null
  status: RiskStatus
  /** 标红时的后果说明 */
  message: string | null
}

const STATUS_ORDER: Record<RiskStatus, number> = { danger: 0, warn: 1, deviated: 2, normal: 3, missing: 4 }

export function assessRisks(entries: ConfigEntry[], rules: RiskRule[] = RISK_RULES): RiskAssessment[] {
  const byKey = new Map(entries.map((entry) => [entry.key, entry]))
  const lookup: ValueLookup = (key) => byKey.get(key)?.effectiveValue
  const results = rules.map((rule): RiskAssessment => {
    const entry = byKey.get(rule.key) ?? null
    if (!entry) return { rule, entry, status: 'missing', message: null }
    const checked = rule.check?.(entry.effectiveValue, lookup) ?? null
    if (checked) return { rule, entry, status: checked.status, message: checked.message }
    if (!rule.deviationExpected && !sameValue(entry.effectiveValue, entry.defaultValue)) {
      return { rule, entry, status: 'deviated', message: '当前值与默认值不同，请确认是有意调整。' }
    }
    return { rule, entry, status: 'normal', message: null }
  })
  return results
    .map((item, index) => ({ item, index }))
    .sort((left, right) => STATUS_ORDER[left.item.status] - STATUS_ORDER[right.item.status] || left.index - right.index)
    .map(({ item }) => item)
}

export const RISK_KEYS: ReadonlySet<string> = new Set(RISK_RULES.map((rule) => rule.key))

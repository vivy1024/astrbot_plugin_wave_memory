// 中央后端原因码 / 错误文案映射。
// 目标：后端返回的 reason_code、fail-closed code 与英文/技术串不得原样出现在界面上；
// 未收录的技术串走通用中文兜底，原始 code 仅供 title/tooltip 调试。

const REASON_LABELS: Record<string, string> = {
  // 作用域 / 会话
  scope_required: '请先选择 Bot 和群',
  bot_and_canonical_session_unavailable: '请先选择 Bot 和有效群会话',
  canonical_platform_and_session_unavailable: '平台与会话信息不完整，暂时无法读取',
  complete_runtime_scope_unavailable: '缺少完整作用域，暂时无法读取',
  unresolved_scope: '作用域尚未解析完成，请稍后重试',
  subject_missing: '缺少目标对象，暂时无法操作',
  server_signed_object_refs_required: '需要先由服务端签发对象引用后再操作',
  object_ref_batch_required: '请逐条选择对象后再操作',
  alias_session_readonly: '这是同一群的旧平台残留，只能查看不能修改',
  legacy_mutation_disabled: '旧数据面已停用，不能在这里修改',
  legacy_caller_not_allowed: '旧调用路径已停用',
  legacy_projection_visibility_unsupported: '旧投影可见性不支持此操作',
  legacy_scope_projection: '旧作用域数据需迁移后才能使用',
  scoped_global_toggle_unsupported: '暂不支持跨作用域整体开关',

  // 心智 / Soul
  soul_scoped_repository_unavailable: '心智数据还没准备好',
  scoped_soul_mutation_unavailable: '还不能在这里改心智数据',
  soul_runtime_refresh_unavailable: '还不能强制刷新心智',
  formal_soul_context_unavailable: '还没有时区、精力或困倦记录',
  proactive_disabled: '主动关心能力当前已关闭',
  proactive_hourly_limit: '本小时主动关心次数已达上限',
  proactive_interval: '距离上次主动关心时间还太短',
  proactive_silent_hour: '当前处于静默时段',
  proactive_llm_failed: '主动关心判断失败，请稍后重试',
  proactive_memory_read_failed: '主动关心所需记忆读取失败',
  proactive_relationship_read_failed: '主动关心所需关系读取失败',
  proactive_dependencies_unavailable: '主动关心依赖的服务暂不可用',
  trigger_signal_absent: '当前没有可触发的信号',
  concern_required: '需要先有一条关切才能继续',
  low_depth_concern_insufficient: '关系还不够深，暂不建议主动关心',
  medium_depth_concern_required: '需要更明确的关系或关切',

  // 关系 / 好感
  relationship_unknown: '还没有和这个人的关系记录',
  relationship_repository_unavailable: '关系数据暂时不可读',
  relationship_calibration_unavailable: '关系校准功能暂不可用',
  relationship_context_ready: '关系上下文已就绪',
  relationship_direct: '当前为直接互动',
  relationship_guidance: '关系提示',
  relationship_high_hostility: '敌意偏高，暂不主动接近',
  relationship_hostility_pending: '存在未消化的冲突',
  relationship_low_trust: '信任度偏低',
  relationship_review_required: '需要人工复核后再结算',
  relationship_trust_pending: '信任尚在观察',
  scoped_affinity_projection_unavailable: '好感投影暂不可用',

  // 信念
  belief_anchor_unavailable: '信念锚点暂不可用',
  belief_edit_command_unavailable: '暂时无法编辑该信念',
  belief_restore_command_unavailable: '暂时无法恢复该信念',
  anchored_belief_command_unavailable: '暂时无法为该信念挂接锚点',
  physical_delete_disabled: '出于数据安全，物理删除已禁用',

  // 黑话
  anchored_jargon_command_unavailable: '暂时无法为该黑话挂接锚点',

  // 人物登记
  registry_empty: '暂无登记的人物记录',
  registry_read_failed: '人物登记读取失败，请重试',
  registry_unavailable: '人物登记服务暂不可用',
  person_timeline_unavailable: '印象时间线暂时不可读',
  invalid_timeline_kind: '时间线类型无效',
  invalid_pagination: '分页参数无效',

  // 标签 / 索引 / 模型
  provider_not_configured: '标签提取模型未配置',
  tag_extractor_unavailable: '标签提取器未启动',
  embedding_unavailable: '向量服务不可用',
  tag_index_unavailable: '标签向量索引不可用',
  tag_index_empty: '标签向量索引为空',
  manifest_invalid: '索引清单验证失败',
  manifest_unavailable: '索引尚未生成版本清单',
  catalog_runtime_derivation_required: '目录需在运行时派生后才能使用',

  // 查询 / 运行时
  historical_audit_query_failed: '历史审计查询失败，请重试',
  query_debug_execution_failed: '查询调试执行失败',
  supervised_task_failed: '后台任务执行失败',
  network_timeout: '请求超时，请检查网络或服务端响应健康',
  durable_jobs_unavailable: '后台任务服务暂不可用',
  agent_feedback_store_unavailable: '智能体反馈存储暂不可用',
  beliefs_table_not_found: '信念数据表不存在',
  belief_not_found: '找不到这条信念',
  jargon_not_found: '找不到这条黑话',
  'beliefs table not found': '信念数据表不存在',
  'belief not found': '找不到这条信念',
  'Source not found': '找不到指定来源',
  'API unavailable': '接口暂时不可用',
}

const GENERIC_UNAVAILABLE = '该数据暂时不可用'

// 判定一段字符串是否是"未本地化的技术串"（纯 ASCII 且像 code 或 HTTP 状态）。
// 真实 reason_code 都是无空格的 snake_case / 点分 token；含空格的多词 ASCII 视为可读句子放行。
function looksLikeRawCode(value: string): boolean {
  const v = value.trim()
  if (!v) return false
  if (/[一-鿿]/.test(v)) return false // 含中文，视为已本地化
  // 单个 snake_case / 点分 code（不含空格），或 HTTP xxx 状态串
  return /^[a-z][a-z0-9_.]*$/i.test(v) || /^HTTP \d{3}$/i.test(v)
}

// 把后端 reason_code 转成中文；未收录的技术串返回通用兜底，不裸显。
export function humanizeReason(code?: string | null, fallback = GENERIC_UNAVAILABLE): string {
  if (!code) return fallback
  const trimmed = code.trim()
  const mapped = REASON_LABELS[trimmed] ?? REASON_LABELS[trimmed.toLowerCase()]
  if (mapped) return mapped
  if (!looksLikeRawCode(trimmed)) return trimmed
  return fallback
}

// 供 title/tooltip：把原始 code 保留给需要排查的人，但绝不作为主视觉文案。
export function rawReasonForDebug(code?: string | null): string | undefined {
  if (!code) return undefined
  return looksLikeRawCode(code.trim()) ? code.trim() : undefined
}

// 从未知 error（ApiError/Error/string）提取可读中文。
// 后端 client.ts 会把 payload.message 塞进 Error.message，这里对其再做一次收口。
export function humanizeApiError(err: unknown, fallback = '操作失败，请稍后重试'): string {
  let message = ''
  if (err instanceof Error) message = err.message
  else if (typeof err === 'string') message = err
  if (!message) return fallback
  const mapped = REASON_LABELS[message.trim()] ?? REASON_LABELS[message.trim().toLowerCase()]
  if (mapped) return mapped
  if (!looksLikeRawCode(message)) return message
  return fallback
}

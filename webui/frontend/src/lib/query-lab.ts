import type {
  QueryDebugPayload,
  QueryDebugResponse,
  QueryDebugResultItem,
  QueryDebugStageState,
  QueryStageName,
} from '@/api/memories'

export const QUERY_STAGE_NAMES: QueryStageName[] = ['epa', 'pyramid', 'spike', 'geodesic']

export const QUERY_STAGE_LABELS: Record<QueryStageName, string> = {
  epa: 'EPA 投影分析',
  pyramid: '残差金字塔',
  spike: '脉冲传播',
  geodesic: '测地线重排',
}

export const QUERY_STAGE_HINTS: Record<QueryStageName, string> = {
  epa: '判断查询聚焦度，决定后续阶段的激进度',
  pyramid: '多层语义分解，复杂问题召回更全',
  spike: '沿共现图传播能量，发现间接关联',
  geodesic: '非欧流形上的测地线距离重排结果',
}

/** 服务端白名单内的可调参数，范围与 QueryOptions 一致。 */
export interface QueryParamSpec {
  name: keyof QueryDebugParamsMap
  label: string
  min: number
  max: number
  step: number
  integer: boolean
  hint: string
}

export interface QueryDebugParamsMap {
  pyramid_max_levels: number
  pyramid_top_k: number
  spike_max_hops: number
  spike_firing_threshold: number
  geodesic_alpha: number
}

export const QUERY_PARAM_SPECS: QueryParamSpec[] = [
  { name: 'pyramid_max_levels', label: '金字塔层数', min: 1, max: 10, step: 1, integer: true, hint: '残差分解的最大层数' },
  { name: 'pyramid_top_k', label: '金字塔 Top-K', min: 1, max: 50, step: 1, integer: true, hint: '每层保留的候选数' },
  { name: 'spike_max_hops', label: '脉冲跳数', min: 0, max: 16, step: 1, integer: true, hint: '共现图上能量传播的最大跳数' },
  { name: 'spike_firing_threshold', label: '脉冲点火阈值', min: 0, max: 1, step: 0.01, integer: false, hint: '低于该能量的节点不点火' },
  { name: 'geodesic_alpha', label: '测地线权重', min: 0, max: 1, step: 0.01, integer: false, hint: '测地线距离与原相似度的混合比例' },
]

export interface StageRunState {
  enabled: boolean
  available: boolean
  reasonCode: string | null
}

export function readStageState(debug: QueryDebugPayload | undefined, stage: QueryStageName): StageRunState {
  const raw: QueryDebugStageState | undefined = debug?.[stage]
  if (!raw || typeof raw !== 'object') {
    return { enabled: false, available: false, reasonCode: 'not_reported' }
  }
  return {
    enabled: Boolean(raw.enabled),
    available: Boolean(raw.available),
    reasonCode: raw.reason_code ? String(raw.reason_code) : null,
  }
}

export type StageOutcome = 'ok' | 'disabled' | 'degraded' | 'not_reported'

/** 把阶段状态压成一个可读结论：启用且可用=ok，启用但降级=degraded。 */
export function stageOutcome(state: StageRunState): StageOutcome {
  if (state.reasonCode === 'not_reported' && !state.enabled) return 'not_reported'
  if (!state.enabled) return 'disabled'
  return state.available ? 'ok' : 'degraded'
}

export const STAGE_OUTCOME_LABELS: Record<StageOutcome, string> = {
  ok: '生效',
  disabled: '未启用',
  degraded: '降级',
  not_reported: '无数据',
}

function idOf(item: QueryDebugResultItem, index: number): string {
  if (item.id !== undefined && item.id !== null) return String(item.id)
  return `#${index}`
}

export interface CompareRow {
  type: 'common' | 'a_only' | 'b_only'
  id: string
  /** A 组的排名（1 起，0 表示未命中） */
  rankA: number
  rankB: number
  similarityA: number | null
  similarityB: number | null
  content: string
}

export interface CompareSummary {
  /** 只出现在 A 组的结果 */
  aOnly: number
  /** 只出现在 B 组的结果 */
  bOnly: number
  common: number
  /** 两组排名完全一致则为 true */
  sameOrder: boolean
  /** 两组命中了同一批 id（顺序可不同） */
  sameSet: boolean
  overlapRatio: number
}

export interface CompareResult {
  rows: CompareRow[]
  summary: CompareSummary
}

/**
 * 对比两轮只读检索的结果：按 id 归类为共同命中 / A 独有 / B 独有。
 * 共同命中按 A 组排名排序，独有项排在后面，便于一眼看出差异来自哪里。
 */
export function compareQueryRuns(a: QueryDebugResponse, b: QueryDebugResponse): CompareResult {
  const indexA = new Map<string, { rank: number; item: QueryDebugResultItem }>()
  const indexB = new Map<string, { rank: number; item: QueryDebugResultItem }>()
  a.results.forEach((item, index) => indexA.set(idOf(item, index), { rank: index + 1, item }))
  b.results.forEach((item, index) => indexB.set(idOf(item, index), { rank: index + 1, item }))

  const rows: CompareRow[] = []
  const allIds = new Set<string>([...indexA.keys(), ...indexB.keys()])
  for (const id of allIds) {
    const left = indexA.get(id)
    const right = indexB.get(id)
    rows.push({
      type: left && right ? 'common' : left ? 'a_only' : 'b_only',
      id,
      rankA: left?.rank ?? 0,
      rankB: right?.rank ?? 0,
      similarityA: typeof left?.item.similarity === 'number' ? left.item.similarity : null,
      similarityB: typeof right?.item.similarity === 'number' ? right.item.similarity : null,
      content: String(left?.item.content ?? right?.item.content ?? ''),
    })
  }

  const rankByA = [...indexA.keys()]
  const rankByB = [...indexB.keys()]
  const sameOrder = rankByA.length === rankByB.length && rankByA.every((id, i) => id === rankByB[i])
  const common = rows.filter((row) => row.type === 'common')

  rows.sort((x, y) => {
    const weight = (row: CompareRow) => (row.type === 'common' ? 0 : 1)
    if (weight(x) !== weight(y)) return weight(x) - weight(y)
    if (x.type === 'common' && y.type === 'common') return x.rankA - y.rankA
    return x.rankA - y.rankA || x.rankB - y.rankB
  })

  const union = allIds.size
  return {
    rows,
    summary: {
      aOnly: rows.filter((row) => row.type === 'a_only').length,
      bOnly: rows.filter((row) => row.type === 'b_only').length,
      common: common.length,
      sameOrder,
      sameSet: aOnlyAndBOnlyEmpty(rows),
      overlapRatio: union ? Math.round((common.length / union) * 100) : 100,
    },
  }
}

function aOnlyAndBOnlyEmpty(rows: CompareRow[]): boolean {
  return rows.every((row) => row.type === 'common')
}

export interface StageDiffRow {
  stage: QueryStageName
  label: string
  outcomeA: StageOutcome
  outcomeB: StageOutcome
  changed: boolean
}

export function diffStages(a: QueryDebugResponse, b: QueryDebugResponse): StageDiffRow[] {
  return QUERY_STAGE_NAMES.map((stage) => {
    const outcomeA = stageOutcome(readStageState(a.debug, stage))
    const outcomeB = stageOutcome(readStageState(b.debug, stage))
    return { stage, label: QUERY_STAGE_LABELS[stage], outcomeA, outcomeB, changed: outcomeA !== outcomeB }
  })
}

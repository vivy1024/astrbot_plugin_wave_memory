/**
 * 关系图谱统一类型配色：知识图谱（kg）与标签图（tags）同一类型同一颜色。
 *
 * - 8 个分类色相按固定顺序分配给固定类型（不随数据循环、不随排名重排）；第 9 类起并入「其他」中性灰。
 * - 浅色 / 深色是同一色相的两档亮度，分别对白底卡片与深色卡片校验过
 *   （dataviz validate_palette：相邻 CVD ΔE ≥ 8.4、正常视觉 ΔE ≥ 19.3；浅色主题中 3 个色相对白底 < 3:1，
 *   因此图例与节点文字常显，颜色从不单独承载含义）。
 * - sigma 的 WebGL 着色器只认 hex / rgb(a)，所以这里给出 hex，而不是 oklch CSS 变量。
 */

export type GraphTheme = 'light' | 'dark'

interface CategorySlot {
  /** 归入该色的类型键（小写）。 */
  types: readonly string[]
  label: string
  light: string
  dark: string
}

const SLOTS: readonly CategorySlot[] = [
  { types: ['topic'], label: '话题', light: '#2a78d6', dark: '#3987e5' },
  { types: ['person', 'user', 'bot'], label: '人物', light: '#eb6834', dark: '#d95926' },
  { types: ['entity'], label: '实体', light: '#1baf7a', dark: '#199e70' },
  { types: ['event'], label: '事件', light: '#eda100', dark: '#c98500' },
  { types: ['emotion', 'mood'], label: '情绪', light: '#e87ba4', dark: '#d55181' },
  { types: ['location'], label: '地点', light: '#008300', dark: '#008300' },
  { types: ['keyword'], label: '关键词', light: '#4a3aa7', dark: '#9085e9' },
  { types: ['jargon'], label: '黑话', light: '#e34948', dark: '#e66767' },
]

const OTHER = { light: '#8b8f96', dark: '#80858d' }

/** 其余已知类型的中文名（颜色统一为「其他」灰）。 */
const EXTRA_LABELS: Record<string, string> = {
  fact: '事实',
  time: '时间',
  memory: '记忆',
  belief: '信念',
  concern: '关切',
  timeline: '时间线',
  community: '标签簇',
  default: '其他',
}

const SLOT_BY_TYPE = new Map<string, CategorySlot>()
for (const slot of SLOTS) for (const type of slot.types) SLOT_BY_TYPE.set(type, slot)

/** 统一的类型键：小写、去空白，空值归为 default。 */
export function normalizeNodeType(type: unknown): string {
  const text = typeof type === 'string' ? type.trim().toLowerCase() : ''
  return text || 'default'
}

export function nodeTypeLabel(type: string): string {
  const key = normalizeNodeType(type)
  if (key === 'user' || key === 'bot') return key === 'bot' ? 'Bot' : '人物'
  return SLOT_BY_TYPE.get(key)?.label ?? EXTRA_LABELS[key] ?? type
}

export function nodeColor(type: string, theme: GraphTheme): string {
  const slot = SLOT_BY_TYPE.get(normalizeNodeType(type))
  return slot ? slot[theme] : OTHER[theme]
}

/** 该类型是否拥有独立色相（否则落在「其他」灰）。 */
export function hasDedicatedColor(type: string): boolean {
  return SLOT_BY_TYPE.has(normalizeNodeType(type))
}

export interface GraphChrome {
  background: string
  label: string
  labelHalo: string
  edge: string
  edgeStrong: string
  /** 非邻居、非路径元素淡出时使用的颜色。 */
  dimNode: string
  dimEdge: string
  highlight: string
  path: string
  hoverBox: string
  hoverBorder: string
}

/**
 * 图谱底色、文字、连线与高亮色：文字只用前景/背景色，不借用类型色。
 * 给 WebGL 用的颜色（edge / dim* / path / highlight）必须是不透明 hex：sigma 按预乘 alpha 混合，rgba 会发白。
 */
export const GRAPH_CHROME: Record<GraphTheme, GraphChrome> = {
  light: {
    background: '#ffffff',
    label: '#1b2433',
    labelHalo: 'rgba(255,255,255,0.92)', // 画在 Canvas2D 标签层，alpha 正常
    // 连线必须实色、够深：1px 的半透明线在白底上几乎看不见
    edge: '#8e9aab',
    edgeStrong: '#57657a',
    dimNode: '#c4cbd6',
    dimEdge: '#e2e6ec',
    highlight: '#0f5f8a',
    path: '#c2410c',
    hoverBox: '#ffffff',
    hoverBorder: 'rgba(27,36,51,0.18)',
  },
  dark: {
    background: '#141b24',
    label: '#e7edf4',
    labelHalo: 'rgba(20,27,36,0.92)',
    edge: '#4d5a6b',
    edgeStrong: '#8b9aae',
    dimNode: '#39424e',
    dimEdge: '#2a323c',
    highlight: '#7dd3fc',
    path: '#fb923c',
    hoverBox: '#1c2531',
    hoverBorder: 'rgba(231,237,244,0.2)',
  },
}

export interface LegendItem {
  type: string
  label: string
  count: number
  color: string
}

/**
 * 图例：只列出图中实际出现的类型。order 非空时按配置顺序在前（标签图的 Tag_Settings.graph_legend_types），
 * 其余按数量降序补在后面，保证不会有类型从图例里消失。
 */
export function buildLegendItems(types: Iterable<string>, theme: GraphTheme, order: readonly string[] = []): LegendItem[] {
  const counts = new Map<string, number>()
  for (const raw of types) {
    const type = normalizeNodeType(raw)
    counts.set(type, (counts.get(type) ?? 0) + 1)
  }
  const ordered: string[] = []
  for (const raw of order) {
    const type = normalizeNodeType(raw)
    if (counts.has(type) && !ordered.includes(type)) ordered.push(type)
  }
  const rest = [...counts.keys()].filter((type) => !ordered.includes(type))
  rest.sort((a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0) || a.localeCompare(b))
  return [...ordered, ...rest].map((type) => ({ type, label: nodeTypeLabel(type), count: counts.get(type) ?? 0, color: nodeColor(type, theme) }))
}

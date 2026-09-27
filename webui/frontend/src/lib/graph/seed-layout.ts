import type { RelationGraph } from './types'

const GOLDEN = 0.6180339887498949

function compareIds(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

/**
 * 确定性初始位置：按类型分扇区（数量越多扇区越宽），扇区内按重要度排序，
 * 用黄金比例在扇区里做「向日葵」式铺点——越重要越靠近圆心。
 * 同一份数据每次得到完全相同的坐标，ForceAtlas2 也是确定性的，所以布局每次打开都一样。
 */
export function seedPositions(graph: RelationGraph): void {
  const order = graph.order
  if (!order) return
  const byType = new Map<string, string[]>()
  graph.forEachNode((id, attrs) => {
    const list = byType.get(attrs.type)
    if (list) list.push(id)
    else byType.set(attrs.type, [id])
  })
  const types = [...byType.keys()].sort((a, b) => (byType.get(b)!.length - byType.get(a)!.length) || compareIds(a, b))
  const radius = 12 * Math.sqrt(order)
  // 每个扇区至少占 8°，避免稀有类型被挤成一条线。
  const minShare = Math.min(1 / types.length, 8 / 360)
  const shares = types.map((type) => Math.max(minShare, byType.get(type)!.length / order))
  const shareSum = shares.reduce((sum, value) => sum + value, 0)
  let start = 0
  types.forEach((type, typeIndex) => {
    const width = (shares[typeIndex] / shareSum) * Math.PI * 2
    const ids = byType.get(type)!
    ids.sort((a, b) => {
      const left = graph.getNodeAttributes(a)
      const right = graph.getNodeAttributes(b)
      return (right.importance - left.importance) || (right.degree - left.degree) || compareIds(a, b)
    })
    const count = ids.length
    ids.forEach((id, index) => {
      const r = radius * Math.sqrt((index + 0.5) / count) * (0.35 + 0.65 * Math.min(1, count / order + 0.5))
      const t = (index * GOLDEN) % 1
      const angle = start + width * (0.08 + 0.84 * t)
      graph.mergeNodeAttributes(id, { x: r * Math.cos(angle), y: r * Math.sin(angle) })
    })
    start += width
  })
}

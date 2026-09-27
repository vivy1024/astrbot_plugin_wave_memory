import { describe, expect, it } from 'vitest'

import { kgFixture, NOW, tagFixture } from './fixtures'
import { kgPayloadToGraph } from './kg-adapter'
import { seedPositions } from './seed-layout'
import { tagPayloadToGraph } from './tag-adapter'
import { buildView, formatWindow } from './views'

const base = { now: NOW, windowHours: 168, maxNodes: 300 }

describe('buildView · 全部', () => {
  it('按重要度截取 maxNodes，并保留它们之间的边', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'all', maxNodes: 3 })
    // memory_count：政治人物信息 60 > 羽书 50 > 烤洋芋 30
    expect(graph.nodes().sort()).toEqual(['tag:1', 'tag:3', 'tag:4'])
    expect(graph.edges().sort()).toEqual(['cooccurrence:1:4', 'cooccurrence:4:3'])
    expect(summary.sentence).toContain('前 3 个标签')
    expect(summary.sentence).toContain('本群共 53649 个')
    expect(summary).toMatchObject({ nodeCount: 3, edgeCount: 2 })
  })

  it('类型过滤与最小权重同时生效', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph } = buildView(bundle, { ...base, view: 'all', types: new Set(['topic', 'keyword']), minWeight: 0.5 })
    expect(graph.nodes().sort()).toEqual(['tag:1', 'tag:2', 'tag:3'])
    // 3→2 权重 0.4 被过滤；2→3 保留
    expect(graph.edges()).toEqual(['cooccurrence:2:3'])
  })
})

describe('buildView · 最强关系', () => {
  it('只保留权重最高的前 N 条边及端点，往返边合并为一条并标记 reciprocal', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'strongest', topEdges: 2 })
    // 最强两条：2→3（1.0）、4→2（0.9）；3→2 是 2→3 的反向边，合并
    expect(graph.edges().sort()).toEqual(['cooccurrence:2:3', 'relation:7'])
    expect(graph.getEdgeAttribute('cooccurrence:2:3', 'reciprocal')).toBe(true)
    expect(graph.nodes().sort()).toEqual(['tag:2', 'tag:3', 'tag:4'])
    expect(summary.sentence).toContain('最强的 2 条关系')
    // 视图内连接最多的节点常显标签
    expect(graph.getNodeAttribute('tag:2', 'forceLabel')).toBe(true)
    // 边粗细随权重
    expect(graph.getEdgeAttribute('cooccurrence:2:3', 'size')).toBeGreaterThan(graph.getEdgeAttribute('relation:7', 'size'))
  })

  it('maxNodes 限制端点数量', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { graph } = buildView(bundle, { ...base, view: 'strongest', maxNodes: 2 })
    expect(graph.order).toBeLessThanOrEqual(2)
    expect(graph.size).toBe(1)
  })
})

describe('buildView · 新出现', () => {
  it('知识图谱：窗口内创建的关系 + 端点，端点都常显名字', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'new', windowHours: 72 })
    expect(graph.edges()).toEqual(['fact:1'])
    expect(graph.nodes().sort()).toEqual(['entity:测试群友甲', 'entity:烤洋芋'])
    // 两个端点最早的关系分别在 50 天前 / 60 天前，都是旧节点：不淡出，但常显名字
    expect(graph.getNodeAttribute('entity:烤洋芋', 'context')).toBe(false)
    expect(graph.getNodeAttribute('entity:烤洋芋', 'forceLabel')).toBe(true)
    expect(summary.sentence).toBe('最近 3 天新出现的 1 条关系，连接的都是已有的人物/实体。')
    expect(summary.note).toContain('创建时间')
  })

  it('标签：按 created_at 选出新标签，并带上与之共现的旧标签', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'new', windowHours: 72 })
    expect(graph.getNodeAttribute('tag:2', 'context')).toBe(false)
    expect(graph.getNodeAttribute('tag:2', 'forceLabel')).toBe(true)
    expect(graph.getNodeAttribute('tag:3', 'context')).toBe(true)
    expect(graph.getNodeAttribute('tag:4', 'context')).toBe(true)
    expect(graph.hasNode('tag:5')).toBe(false)
    expect(summary.sentence).toBe('最近 3 天新出现的 1 个标签；淡色小点是与它们共现的已有标签。')
  })

  it('旧后端没有 created_at 时明确不可用，而不是拿别的字段冒充', () => {
    const bundle = tagPayloadToGraph(tagFixture(false))
    const { graph, summary } = buildView(bundle, { ...base, view: 'new' })
    expect(graph.order).toBe(0)
    expect(summary.unavailable).toContain('created_at')
  })

  it('窗口内什么都没有时给出放宽窗口的提示', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { summary } = buildView(bundle, { ...base, view: 'new', windowHours: 24 })
    expect(summary.nodeCount).toBe(0)
    expect(summary.sentence).toContain('可以把时间窗口放宽')
  })
})

describe('buildView · 当前话题', () => {
  it('标签：按窗口内记忆数排序，点的大小跟随活跃度', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'current' })
    expect(graph.nodes().sort()).toEqual(['tag:2', 'tag:3', 'tag:4'])
    expect(graph.getNodeAttribute('tag:4', 'size')).toBeGreaterThan(graph.getNodeAttribute('tag:3', 'size'))
    expect(summary.sentence).toContain('最近 7 天最活跃的 3 个标签')
    expect(summary.note).toContain('记忆条数')
  })

  it('知识图谱：活跃度 = 窗口内新增或更新的关系数', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'current' })
    // 7 天内更新过的关系：fact:1、tagrel:3 → 测试群友甲 2 条，羽书 / 烤洋芋 各 1 条
    expect(graph.nodes().sort()).toEqual(['entity:测试群友甲', 'entity:烤洋芋', 'entity:羽书'])
    expect(graph.getNodeAttribute('entity:测试群友甲', 'size')).toBeGreaterThan(graph.getNodeAttribute('entity:羽书', 'size'))
    expect(summary.note).toContain('关系条数')
  })

  it('窗口内活跃不足 3 个时回退到最后出现时间最近的节点', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { graph, summary } = buildView(bundle, { ...base, view: 'current', windowHours: 36 })
    // 36 小时内只有 tagrel:3 → 2 个节点 → 回退（孤点没有时间，不出现）
    expect(summary.note).toContain('不足 3 个')
    expect(graph.hasNode('entity:孤点')).toBe(false)
    expect(graph.order).toBe(4)
  })
})

describe('buildView · 聚焦与路径', () => {
  it('pinned 节点即使不在视图里也会带着最强邻居加入并常显标签', () => {
    const bundle = tagPayloadToGraph(tagFixture())
    const { graph } = buildView(bundle, { ...base, view: 'all', maxNodes: 1, pinned: ['tag:2'] })
    expect(graph.hasNode('tag:2')).toBe(true)
    expect(graph.getNodeAttribute('tag:2', 'forceLabel')).toBe(true)
    expect(graph.hasNode('tag:3')).toBe(true)
    expect(graph.hasEdge('cooccurrence:2:3')).toBe(true)
  })

  it('路径节点与边强制加入', () => {
    const bundle = kgPayloadToGraph(kgFixture())
    const { graph } = buildView(bundle, { ...base, view: 'strongest', topEdges: 1, path: { nodes: ['entity:空气炸锅', 'entity:羽书'], edges: ['tagrel:4'] } })
    expect(graph.hasEdge('tagrel:4')).toBe(true)
    expect(graph.getNodeAttribute('entity:空气炸锅', 'forceLabel')).toBe(true)
  })
})

describe('确定性', () => {
  it('同一份数据、不同输入顺序，得到完全相同的视图与初始坐标', () => {
    const left = buildView(kgPayloadToGraph(kgFixture()), { ...base, view: 'all' }).graph
    const reversed = kgFixture()
    reversed.nodes = [...reversed.nodes].reverse()
    // 重复 id 的边是「先到先得」，这里去掉它再打乱顺序
    reversed.edges = reversed.edges.filter((edge) => edge.l !== 'dup').reverse()
    const right = buildView(kgPayloadToGraph(reversed), { ...base, view: 'all' }).graph
    const positions = (graph: typeof left) => graph.nodes().sort().map((id) => [id, graph.getNodeAttribute(id, 'x').toFixed(6), graph.getNodeAttribute(id, 'y').toFixed(6)])
    expect(positions(left)).toEqual(positions(right))
  })

  it('seedPositions 按类型分扇区，越重要越靠近圆心', () => {
    const { graph } = kgPayloadToGraph(kgFixture())
    seedPositions(graph)
    const radius = (id: string) => Math.hypot(graph.getNodeAttribute(id, 'x'), graph.getNodeAttribute(id, 'y'))
    expect(radius('entity:羽书')).toBeLessThan(radius('entity:测试群友甲'))
    graph.forEachNode((_id, attrs) => {
      expect(Number.isFinite(attrs.x)).toBe(true)
      expect(Number.isFinite(attrs.y)).toBe(true)
    })
  })

  it('formatWindow', () => {
    expect(formatWindow(24)).toBe('1 天')
    expect(formatWindow(168)).toBe('7 天')
    expect(formatWindow(6)).toBe('6 小时')
  })
})

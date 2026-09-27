import { describe, expect, it } from 'vitest'

import { nodeColor } from '@/lib/graph-palette'
import { kgFixture, NOW, tagFixture } from './fixtures'
import { kgPayloadToGraph } from './kg-adapter'
import { tagPayloadToGraph } from './tag-adapter'

describe('kgPayloadToGraph', () => {
  it('把 /api/kg/full 转成统一属性的 graphology 图，丢弃坏边', () => {
    const bundle = kgPayloadToGraph(kgFixture(), 'dark')
    const { graph } = bundle
    expect(bundle.layer).toBe('kg')
    expect(graph.order).toBe(5)
    // fact:bad（端点不存在）、fact:self（自环）、重复的 fact:1 都被丢弃
    expect(graph.size).toBe(4)
    expect(graph.hasEdge('fact:bad')).toBe(false)
    expect(graph.source('fact:1')).toBe('entity:测试群友甲')

    const person = graph.getNodeAttributes('entity:羽书')
    expect(person).toMatchObject({ label: '羽书', type: 'person', degree: 3, importance: 3 })
    expect(person.color).toBe(nodeColor('person', 'dark'))
    // 类型规范化为小写
    expect(graph.getNodeAttribute('entity:孤点', 'type')).toBe('entity')

    const edge = graph.getEdgeAttributes('fact:1')
    expect(edge).toMatchObject({ weight: 0.85, label: '喜欢', kind: 'fact', confidence: 0.85, type: 'arrow' })
    expect(edge.createdAt).toBe(NOW - 2 * 86_400)
    expect(bundle.rawEdges.get('fact:1')?.source_memory_id).toBe(656540)
  })

  it('节点时间由关联边推导：createdAt 取最早 created_ts，lastSeenAt 取最晚 ts', () => {
    const { graph, timeFields } = kgPayloadToGraph(kgFixture())
    const yushu = graph.getNodeAttributes('entity:羽书')
    expect(yushu.createdAt).toBe(NOW - 90 * 86_400) // tagrel:4 没有 created_ts，回退到它的 ts
    expect(yushu.lastSeenAt).toBe(NOW - 86_400)
    expect(graph.getNodeAttribute('entity:孤点', 'createdAt')).toBeUndefined()
    expect(timeFields).toMatchObject({ nodeCreated: true, nodeCreatedExact: true, edgeTime: true, nodeRecentCount: false })
  })

  it('旧后端没有 created_ts 时标记为近似', () => {
    const payload = kgFixture()
    payload.edges = payload.edges.map(({ created_ts: _created, ...edge }) => edge)
    expect(kgPayloadToGraph(payload).timeFields.nodeCreatedExact).toBe(false)
  })
})

describe('tagPayloadToGraph', () => {
  it('节点 importance = memory_count，带上新版后端的时间字段', () => {
    const bundle = tagPayloadToGraph(tagFixture(), 'light')
    const { graph } = bundle
    expect(bundle.layer).toBe('tags')
    expect(bundle.total).toBe(53_649)
    expect(bundle.legendOrder).toEqual(['keyword', 'topic'])
    expect(graph.order).toBe(5)
    expect(graph.size).toBe(5)
    const node = graph.getNodeAttributes('tag:2')
    expect(node).toMatchObject({ label: '空气炸锅', type: 'keyword', importance: 12, recentCount: 5, createdAt: NOW - 86_400 })
    expect(node.color).toBe(nodeColor('keyword', 'light'))
    expect(graph.getEdgeAttributes('relation:7')).toMatchObject({ kind: 'relation', type: 'arrow', label: 'likes' })
    expect(graph.getEdgeAttributes('cooccurrence:2:3')).toMatchObject({ kind: 'cooccurrence', type: 'line', weight: 1 })
    expect(bundle.timeFields).toMatchObject({ nodeCreated: true, nodeRecentCount: true, nodeLastSeen: true })
    expect(bundle.recentWindowHours).toBe(168)
  })

  it('旧后端：createdAt 缺省，lastSeenAt 回退到关联记忆与边的最近时间', () => {
    const bundle = tagPayloadToGraph(tagFixture(false))
    expect(bundle.timeFields).toMatchObject({ nodeCreated: false, nodeRecentCount: false, nodeLastSeen: true })
    const graph = bundle.graph
    expect(graph.getNodeAttribute('tag:2', 'createdAt')).toBeUndefined()
    // tag:1 的关联记忆时间 NOW-30d，关联边 1→4 latest_ts NOW-10d，取较新的
    expect(graph.getNodeAttribute('tag:1', 'lastSeenAt')).toBe(NOW - 10 * 86_400)
    expect(graph.getNodeAttribute('tag:2', 'lastSeenAt')).toBe(NOW - 3600)
  })

  it('同类型在两个数据层里颜色一致', () => {
    const kg = kgPayloadToGraph(kgFixture(), 'dark').graph
    const tags = tagPayloadToGraph(tagFixture(), 'dark').graph
    expect(kg.getNodeAttribute('entity:烤洋芋', 'color')).toBe(tags.getNodeAttribute('tag:3', 'color'))
    expect(kg.getNodeAttribute('entity:羽书', 'color')).toBe(tags.getNodeAttribute('tag:4', 'color'))
  })
})

describe('mergeTagPath', () => {
  it('把不在当前截取里的路径节点 / 边并入新 bundle，不修改原对象', async () => {
    const { mergeTagPath } = await import('./tag-adapter')
    const bundle = tagPayloadToGraph(tagFixture())
    const far = { ...tagFixture().nodes[0], id: 'tag:99', locator: 99, name: '远处的标签', ref: 'oref.tag-99' }
    const edge = { ...tagFixture().edges[0], id: 'cooccurrence:5:99', source: 'tag:5', target: 'tag:99' }
    const merged = mergeTagPath(bundle, { nodes: [bundle.rawNodes.get('tag:5')!, far], edges: [edge] })
    expect(bundle.graph.hasNode('tag:99')).toBe(false)
    expect(merged.graph.hasNode('tag:99')).toBe(true)
    expect(merged.graph.hasEdge('cooccurrence:5:99')).toBe(true)
    expect(merged.rawNodes.get('tag:99')?.name).toBe('远处的标签')
    expect(mergeTagPath(bundle, { nodes: [], edges: [] })).toBe(bundle)
  })
})

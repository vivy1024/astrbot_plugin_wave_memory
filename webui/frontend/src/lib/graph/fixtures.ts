import type { KgFullPayload } from '@/api/kg'
import type { TagGraphEdge, TagGraphNode, TagGraphPayload } from '@/api/tagGraph'

/** 测试用：秒级时间基准（2026-09-27 00:00 UTC 附近）。 */
export const NOW = 1_790_467_200
const DAY = 86_400

export function kgFixture(overrides: Partial<KgFullPayload> = {}): KgFullPayload {
  return {
    nodes: [
      { id: 'entity:羽书', name: '羽书', type: 'person', layer: 'facts', degree: 3 },
      { id: 'entity:测试群友甲', name: '测试群友甲', type: 'person', layer: 'facts', degree: 2 },
      { id: 'entity:烤洋芋', name: '烤洋芋', type: 'topic', layer: 'facts', degree: 2 },
      { id: 'entity:空气炸锅', name: '空气炸锅', type: 'entity', layer: 'facts', degree: 1 },
      { id: 'entity:孤点', name: '孤点', type: 'Entity', layer: 'facts', degree: 0 },
    ],
    edges: [
      { id: 'fact:1', s: 'entity:测试群友甲', t: 'entity:烤洋芋', l: '喜欢', weight: 0.85, confidence: 0.85, ts: NOW - 2 * DAY, created_ts: NOW - 2 * DAY, layer: 'facts', kind: 'fact', source_memory_id: 656540, provenance: { evidence: '说自己用空气炸锅烤土豆', source_quote: '我用喷油壶喷过油了' } },
      { id: 'tagrel:2', s: 'entity:羽书', t: 'entity:烤洋芋', l: 'discusses', weight: 1, confidence: 0.7, ts: NOW - 40 * DAY, created_ts: NOW - 60 * DAY, layer: 'facts', kind: 'tag_relation' },
      { id: 'tagrel:3', s: 'entity:羽书', t: 'entity:测试群友甲', l: 'knows', weight: 1, confidence: 0.7, ts: NOW - 1 * DAY, created_ts: NOW - 50 * DAY, layer: 'facts', kind: 'tag_relation' },
      { id: 'tagrel:4', s: 'entity:羽书', t: 'entity:空气炸锅', l: 'mentions', weight: 0.5, confidence: 0.6, ts: NOW - 90 * DAY, layer: 'facts', kind: 'tag_relation' },
      // 端点不存在 / 自环 / 重复 id 都应被丢弃
      { id: 'fact:bad', s: 'entity:不存在', t: 'entity:羽书', l: 'x', weight: 1, layer: 'facts', kind: 'fact' },
      { id: 'fact:self', s: 'entity:羽书', t: 'entity:羽书', l: 'x', weight: 1, layer: 'facts', kind: 'fact' },
      { id: 'fact:1', s: 'entity:羽书', t: 'entity:空气炸锅', l: 'dup', weight: 1, layer: 'facts', kind: 'fact' },
    ],
    node_total: 5,
    layers: ['facts'],
    read_only: true,
    ...overrides,
  }
}

function tagNode(id: number, name: string, type: string, memoryCount: number, extra: Partial<TagGraphNode> = {}): TagGraphNode {
  return {
    id: `tag:${id}`,
    locator: id,
    name,
    type,
    description: '',
    confidence: 1,
    metadata: {},
    status: 'active',
    revision: 1,
    memory_count: memoryCount,
    frequency: memoryCount,
    source_counts: { automatic: memoryCount },
    sources: ['automatic'],
    associated_memories: [],
    in_degree: 0,
    out_degree: 0,
    in_weight: 0,
    out_weight: 0,
    ref: `oref.tag-${id}`,
    read_only: true,
    ...extra,
  }
}

function tagEdge(source: number, target: number, weight: number, extra: Partial<TagGraphEdge> = {}): TagGraphEdge {
  return {
    id: `cooccurrence:${source}:${target}`,
    source: `tag:${source}`,
    target: `tag:${target}`,
    layer: 'cooccurrence',
    kind: 'directed_cooccurrence',
    type: 'ordinal_cooccurrence',
    label: '序位共现',
    weight,
    frequency: 1,
    confidence: 0.6,
    latest_ts: NOW - 10 * DAY,
    source_kind: 'effective_memory_tags',
    read_only: true,
    ...extra,
  }
}

/** withTime=false 模拟线上旧后端（没有 created_at / last_seen_ts / recent_memory_count）。 */
export function tagFixture(withTime = true): TagGraphPayload {
  const time = (created: number, lastSeen: number, recent: number) => withTime ? { created_at: created, updated_at: lastSeen, last_seen_ts: lastSeen, recent_memory_count: recent } : {}
  return {
    nodes: [
      tagNode(1, '政治人物信息', 'topic', 60, { ...time(NOW - 100 * DAY, NOW - 30 * DAY, 0), associated_memories: [{ id: 9, content: '旧记忆', sender: 'a', timestamp: NOW - 30 * DAY, importance: 1, version: 1, tag_source: 'automatic', relevance: 0.6 }] }),
      tagNode(2, '空气炸锅', 'keyword', 12, time(NOW - 1 * DAY, NOW - 3600, 5)),
      tagNode(3, '烤洋芋', 'topic', 30, time(NOW - 80 * DAY, NOW - 2 * DAY, 3)),
      tagNode(4, '羽书', 'person', 50, time(NOW - 200 * DAY, NOW - 7200, 9)),
      tagNode(5, '无关标签', 'jargon', 1, time(NOW - 300 * DAY, NOW - 300 * DAY, 0)),
    ],
    edges: [
      tagEdge(2, 3, 1, { latest_ts: NOW - 3600 }),
      tagEdge(3, 2, 0.4, { latest_ts: NOW - 3600 }),
      tagEdge(4, 3, 0.7),
      tagEdge(1, 4, 0.2),
      { ...tagEdge(4, 2, 0.9), id: 'relation:7', layer: 'relations', kind: 'tag_relation', type: 'likes', label: 'likes', latest_ts: NOW - 2 * DAY },
    ],
    layers: ['cooccurrence', 'relations'],
    available_layers: ['cooccurrence', 'relations'],
    layer_counts: {},
    tag_total: 53_649,
    legend: { enabled: true, types: ['keyword', 'topic'], show_count: true, known_types: [] },
    scope: { bot_id: 'yushu', session_id: '羽书:group:42', visibility: 'group' },
    read_only: true,
    generated_at: NOW,
    warnings: [],
    pulse: { enabled: false, half_life_hours: 72 },
    ...(withTime ? { rank_by: 'links' as const, recent_window_hours: 168 } : {}),
  }
}

import { describe, expect, it } from 'vitest'

import type { RecallReplayEvent, RecallReplayPayload, TagGraphEdge, TagGraphNode } from '@/api/tagGraph'
import { buildShowcaseData, decayIntensity, eventPulse, replayDelay, REPLAY_STEP_MS } from './showcase'

function node(id: number, memoryCount: number, type = 'topic'): TagGraphNode {
  return {
    id: `tag:${id}`, locator: id, name: `标签${id}`, type, description: '', confidence: 0.9, metadata: {},
    status: 'active', revision: 1, memory_count: memoryCount, frequency: memoryCount, source_counts: {}, sources: [],
    associated_memories: [{ id: id * 10, content: `记忆${id}`, sender: 'A', timestamp: 0, importance: 0.5, version: 1, tag_source: 'automatic', relevance: 1 }],
    in_degree: 0, out_degree: 0, in_weight: 0, out_weight: 0, ref: `oref.${id}`, read_only: true,
  }
}

function edge(source: number, target: number, weight: number): TagGraphEdge {
  return {
    id: `cooccurrence:${source}:${target}`, source: `tag:${source}`, target: `tag:${target}`, layer: 'cooccurrence',
    kind: 'directed_cooccurrence', type: 'ordinal_cooccurrence', label: '', weight, frequency: 1, confidence: 1,
    latest_ts: 0, source_kind: 'effective_memory_tags', read_only: true,
  }
}

function event(tags: number[], id = 't'): RecallReplayEvent {
  return { trace_id: id, timestamp: 0, sender_name: 'A', message_preview: '一句话', memory_count: tags.length, tags: tags.map((tag) => `tag:${tag}`), memories: [] }
}

function payload(events: RecallReplayEvent[]): RecallReplayPayload {
  return {
    graph: {
      nodes: [node(1, 100), node(2, 25), node(3, 1), node(4, 4, 'person')],
      edges: [edge(1, 2, 0.9), edge(2, 1, 0.4), edge(2, 3, 0.5), edge(1, 4, 0.2), edge(1, 99, 1)],
      layers: ['cooccurrence'], available_layers: ['cooccurrence'], layer_counts: {},
      scope: { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group' }, read_only: true, generated_at: 0, warnings: [],
      pulse: { enabled: false, half_life_hours: 72 },
    },
    events,
    window: { from: 0, to: 1, hours: 24 },
    stats: { event_count: events.length, memory_hits: 0, tagged_memory_hits: 0, recalled_tags: 0, recalled_tags_on_graph: 0, events_with_tags: 0 },
    read_only: true,
    generated_at: 0,
  }
}

describe('buildShowcaseData', () => {
  it('同一对节点只留一条最强边，丢弃图外端点；大小看记忆数、亮度看被想起的次数', () => {
    const data = buildShowcaseData(payload([event([2]), event([2, 3]), event([2])]))
    expect(data.links.map((link) => link.id).sort()).toHaveLength(3)
    const pair12 = data.links.find((link) => [link.source, link.target].sort().join() === 'tag:1,tag:2')
    expect(pair12?.weight).toBe(0.9)
    expect(data.links.some((link) => link.target === 'tag:99')).toBe(false)

    const byId = data.nodeById
    expect(byId.get('tag:1')!.size).toBeGreaterThan(byId.get('tag:3')!.size)
    expect(byId.get('tag:2')!.recallCount).toBe(3)
    expect(byId.get('tag:2')!.glow).toBe(1)
    expect(byId.get('tag:1')!.glow).toBeLessThan(byId.get('tag:3')!.glow)
    expect(byId.get('tag:1')!.memories).toEqual(['记忆1'])
    // 常显标签：先按被想起次数，再按记忆数
    expect([...buildShowcaseData(payload([event([3])]), { labelCount: 2 }).labelIds]).toEqual(['tag:3', 'tag:1'])
  })

  it('每个节点最多保留若干条最强边', () => {
    const data = buildShowcaseData(payload([]), { maxLinksPerNode: 1 })
    // tag:1 只留 0.9 的那条；tag:3 只有 2-3 一条，tag:4 只有 1-4 一条，各自保留
    expect(data.links.map((link) => link.weight).sort()).toEqual([0.2, 0.5, 0.9])
  })
})

describe('eventPulse', () => {
  it('点亮这次想起的节点，只在它们之间已有的边上发脉冲，邻居作为余晖', () => {
    const data = buildShowcaseData(payload([]))
    const pulse = eventPulse(event([1, 2, 77]), data)
    expect(pulse.lit).toEqual(['tag:1', 'tag:2'])
    expect(pulse.links.map((link) => link.weight)).toEqual([0.9])
    expect(pulse.halo.sort()).toEqual(['tag:3', 'tag:4'])
    expect(pulse.labels).toEqual(['tag:1', 'tag:2'])
  })
})

describe('回放节奏', () => {
  it('亮度按半衰期衰减，过低归零', () => {
    expect(decayIntensity(1, 1000, 1000)).toBeCloseTo(0.5)
    expect(decayIntensity(0.02, 5000, 1000)).toBe(0)
  })

  it('没点亮任何节点的事件停留更短，倍速成比例缩短', () => {
    expect(replayDelay(event([1]), 1)).toBe(REPLAY_STEP_MS)
    expect(replayDelay(event([1]), 2)).toBe(REPLAY_STEP_MS / 2)
    expect(replayDelay(event([]), 1)).toBeLessThan(REPLAY_STEP_MS)
  })
})

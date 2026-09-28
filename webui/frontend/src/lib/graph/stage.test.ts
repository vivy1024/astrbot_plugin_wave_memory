import { describe, expect, it } from 'vitest'

import type { RecallReplayEvent } from '@/api/tagGraph'
import type { ShowcaseData } from './showcase'
import { describeEvent, idleCandidates, isPublicSession, mergeEvents, sourceLabel } from './stage'

function event(id: string, ts: number, session: string, tags: string[] = ['tag:1']): RecallReplayEvent {
  return {
    trace_id: id, timestamp: ts, sender_name: '小明', message_preview: '今天吃什么', memory_count: 3, tags,
    memories: [{ id: 1, channel: 'memory', preview: '上次说想吃火锅', tags }], session_id: session,
  }
}

const data = {
  nodes: [], links: [], linkByPair: new Map(), neighbors: new Map(), labelIds: new Set(),
  nodeById: new Map([['tag:1', { id: 'tag:1', name: '火锅' }]]),
} as unknown as ShowcaseData

describe('舞台页隐私', () => {
  it('B 站弹幕公开显示原话与发送者；群聊默认只显示点亮的标签', () => {
    expect(isPublicSession('bilibili:group:24292304')).toBe(true)
    const live = describeEvent(event('a', 1, 'bilibili:group:1'), data, 'strict')
    expect(live).toMatchObject({ source: '直播弹幕', speaker: '小明', message: '今天吃什么', litNames: ['火锅'], memories: [] })

    const group = describeEvent(event('b', 1, '羽书:group:398291136'), data, 'strict')
    expect(group).toMatchObject({ source: '群聊', speaker: null, message: null, litNames: ['火锅'], memories: [] })

    expect(describeEvent(event('c', 1, '羽书:group:1'), data, 'open').memories).toEqual(['上次说想吃火锅'])
    expect(sourceLabel(event('d', 1, '羽书:private:9'))).toBe('私聊')
  })
})

describe('事件合并与空闲回放', () => {
  it('按 trace_id 去重、按时间排序、保留最近若干条', () => {
    const merged = mergeEvents([event('a', 2, 's'), event('b', 1, 's')], [event('a', 2, 's'), event('c', 3, 's')], 2)
    expect(merged.map((item) => item.trace_id)).toEqual(['a', 'c'])
  })

  it('空闲回放只挑点亮过节点的事件', () => {
    expect(idleCandidates([event('a', 1, 's', []), event('b', 2, 's')]).map((item) => item.trace_id)).toEqual(['b'])
  })
})

/**
 * 直播舞台页（/stage）的纯逻辑：事件合并、隐私脱敏、空闲回放节奏。
 *
 * 隐私：直播是公开画面。B 站弹幕本来就公开，照常显示原话与发送者；其它会话（QQ 群、私聊）
 * 默认只显示"群聊里有人说了一句话"和点亮的标签，不显示原话、昵称与记忆内容。
 */
import type { RecallReplayEvent } from '@/api/tagGraph'
import type { ShowcaseData } from './showcase'

export type StagePrivacy = 'strict' | 'open'
export type CaptionCorner = 'bl' | 'br' | 'tl' | 'tr' | 'none'

/** 没有新对话多久后开始空闲回放。 */
export const IDLE_AFTER_MS = 40_000
/** 空闲回放：一轮放几条、每条停多久、两轮之间留多久纯星空。 */
export const IDLE_BATCH = 5
export const IDLE_STEP_MS = 4_500
export const IDLE_REST_MS = 25_000
/** 实时事件点亮后保持聚焦（压暗其余节点）的时间。 */
export const LIVE_FOCUS_MS = 7_000
export const POLL_MS = 3_000
/** 标签图多久整体重建一次（新标签进星空）。 */
export const GRAPH_REFRESH_MS = 15 * 60_000
export const MAX_EVENTS = 400

export function isPublicSession(sessionId: string | null | undefined): boolean {
  return typeof sessionId === 'string' && sessionId.startsWith('bilibili:')
}

export function sourceLabel(event: RecallReplayEvent): string {
  if (isPublicSession(event.session_id)) return '直播弹幕'
  if (event.session_id?.includes(':private:')) return '私聊'
  return '群聊'
}

export interface StageCaption {
  source: string
  /** 说话的人；隐私模式下的非公开会话为 null。 */
  speaker: string | null
  /** 原话；隐私模式下的非公开会话为 null。 */
  message: string | null
  memoryCount: number
  /** 点亮的标签名（星空上可见的，最多 6 个）。 */
  litNames: string[]
  /** 想起的记忆内容；隐私模式下为空（记忆多来自群聊）。 */
  memories: string[]
}

export function describeEvent(event: RecallReplayEvent, data: ShowcaseData, privacy: StagePrivacy): StageCaption {
  const open = privacy === 'open' || isPublicSession(event.session_id)
  const litNames = event.tags.map((id) => data.nodeById.get(id)?.name).filter((name): name is string => Boolean(name)).slice(0, 6)
  return {
    source: sourceLabel(event),
    speaker: open ? event.sender_name || '观众' : null,
    message: open ? event.message_preview : null,
    memoryCount: event.memory_count,
    litNames,
    memories: privacy === 'open' ? event.memories.slice(0, 2).map((memory) => memory.preview).filter(Boolean) : [],
  }
}

/** 按 trace_id 去重、按时间排序，只留最近 cap 条。 */
export function mergeEvents(existing: RecallReplayEvent[], incoming: RecallReplayEvent[], cap = MAX_EVENTS): RecallReplayEvent[] {
  const byId = new Map(existing.map((event) => [event.trace_id, event]))
  for (const event of incoming) byId.set(event.trace_id, event)
  return [...byId.values()].sort((a, b) => a.timestamp - b.timestamp || (a.trace_id < b.trace_id ? -1 : 1)).slice(-cap)
}

/** 空闲回放挑选：最近的、点亮过节点的事件，从旧到新。 */
export function idleCandidates(events: RecallReplayEvent[], limit = 60): RecallReplayEvent[] {
  return events.filter((event) => event.tags.length > 0).slice(-limit)
}

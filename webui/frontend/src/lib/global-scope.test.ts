import { describe, expect, it } from 'vitest'

import type { ScopeOptionsPayload } from '@/api/options'
import { resolveScopeDefault, type StoredGlobalScope } from '@/lib/global-scope'

const payload: ScopeOptionsPayload = {
  bots: [
    { db_id: 'yushu', name: '羽书' },
    { db_id: 'baizz', name: '白真真' },
  ],
  sessions: [
    { id: '羽书:group:1', bot_id: 'yushu', platform_id: '羽书', kind: 'group', conversation_id: '1', label: '小群', count: 10 },
    { id: '羽书:group:2', bot_id: 'yushu', platform_id: '羽书', kind: 'group', conversation_id: '2', label: '大群', count: 900 },
    { id: '羽书:private:u1', bot_id: 'yushu', platform_id: '羽书', kind: 'private', conversation_id: 'u1', label: '私聊' },
    { id: '白真真:group:3', bot_id: 'baizz', platform_id: '白真真', kind: 'group', conversation_id: '3', label: '白群', count: 5 },
  ],
  legacy_groups: [{ bot_id: 'yushu', group_id: '99', label: '旧群' }],
  channels: [],
  generated_at: 0,
  source: { health: 'healthy', reason_code: null },
}

const none = { botId: '', sessionId: '', visibility: '' }

describe('resolveScopeDefault', () => {
  it('不需要作用域的页面从不自动填充', () => {
    expect(resolveScopeDefault({ payload, level: 'none', current: none })).toBeNull()
  })

  it('没有 URL 和记忆时选第一个有群的 Bot 及其最活跃的群', () => {
    expect(resolveScopeDefault({ payload, level: 'session', current: none })).toEqual({
      botId: 'yushu', sessionId: '羽书:group:2', visibility: 'group',
    })
  })

  it('首次进入时选消息量最多的 Bot，而不是列表第一个', () => {
    const reordered = { ...payload, bots: [payload.bots[1], payload.bots[0]] }
    expect(resolveScopeDefault({ payload: reordered, level: 'session', current: none })?.botId).toBe('yushu')
  })

  it('URL 里只有 Bot 时优先回到该 Bot 上次选过的群', () => {
    const stored: StoredGlobalScope = { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group', sessions_by_bot: { baizz: '白真真:group:3' } }
    expect(resolveScopeDefault({ payload, level: 'session', current: { botId: 'baizz', sessionId: '', visibility: '' }, stored })).toEqual({
      botId: 'baizz', sessionId: '白真真:group:3', visibility: 'group',
    })
  })

  it('URL 已是合法作用域时不改动', () => {
    expect(resolveScopeDefault({ payload, level: 'session', current: { botId: 'yushu', sessionId: '羽书:group:1', visibility: 'group' } })).toBeNull()
  })

  it('不认识的 Bot 退回上次选择', () => {
    const stored: StoredGlobalScope = { bot_id: 'yushu', session_id: '羽书:group:1', visibility: 'group', sessions_by_bot: {} }
    expect(resolveScopeDefault({ payload, level: 'session', current: { botId: 'ghost', sessionId: '', visibility: '' }, stored })).toEqual({
      botId: 'yushu', sessionId: '羽书:group:1', visibility: 'group',
    })
  })

  it('私聊与未绑定旧群只在页面声明允许时接受', () => {
    const privateScope = { botId: 'yushu', sessionId: '羽书:private:u1', visibility: 'private' }
    expect(resolveScopeDefault({ payload, level: 'session', current: privateScope })?.sessionId).toBe('羽书:group:2')
    expect(resolveScopeDefault({ payload, level: 'session', allow: { private: true }, current: privateScope })).toBeNull()

    const legacyScope = { botId: 'yushu', sessionId: 'legacy:yushu:99', visibility: 'group' }
    expect(resolveScopeDefault({ payload, level: 'session', current: legacyScope })?.sessionId).toBe('羽书:group:2')
    expect(resolveScopeDefault({ payload, level: 'session', allow: { legacy: true }, current: legacyScope })).toBeNull()
  })

  it('Bot 级页面只补 Bot，不主动挑群', () => {
    expect(resolveScopeDefault({ payload, level: 'bot', current: none })).toEqual({ botId: 'yushu', sessionId: '', visibility: 'group' })
  })
})

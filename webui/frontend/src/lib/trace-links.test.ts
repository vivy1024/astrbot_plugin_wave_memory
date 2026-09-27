import { describe, expect, it } from 'vitest'

import type { SessionOptionDto } from '@/api/options'
import { channelConfigHref, memoryHref, observatoryTraceHref, sessionForTrace, traceItemLink } from './trace-links'

const ctx = { botId: 'yushu', sessionId: '羽书:group:398291136' }

function query(to: string): URLSearchParams {
  return new URLSearchParams(to.split('?')[1] ?? '')
}

describe('trace-links 链接规则', () => {
  it('memory / fts5 条目用记忆行自己的作用域精确打开记忆', () => {
    const link = traceItemLink('memory', {
      id: 397782,
      link: { kind: 'memory', memory_id: 397782, memory_scope: { bot_id: 'yushu', session_id: '羽书:group:150727649', visibility: 'group' } },
    }, ctx)
    expect(link?.precise).toBe(true)
    expect(link?.to.startsWith('/memories?')).toBe(true)
    const params = query(link!.to)
    expect(params.get('memory_id')).toBe('397782')
    expect(params.get('session_id')).toBe('羽书:group:150727649')
    expect(params.get('bot_id')).toBe('yushu')
    expect(params.get('visibility')).toBe('group')

    const fts = traceItemLink('fts5', { id: 188634 }, ctx)
    expect(query(fts!.to).get('session_id')).toBe(ctx.sessionId)
    expect(query(fts!.to).get('memory_id')).toBe('188634')
  })

  it('非数字编号的记忆条目不生成链接', () => {
    expect(traceItemLink('memory', { id: 'abc' }, ctx)).toBeNull()
  })

  it('事实、黑话、书设定、人物只带作用域与搜索词', () => {
    const fact = traceItemLink('facts', { link: { rowid: 8, subject: 'vivy1024', object: '羽书提审偷懒' } }, ctx)!
    expect(fact.precise).toBe(false)
    expect(fact.to.startsWith('/facts?')).toBe(true)
    expect(query(fact.to).get('search')).toBe('羽书提审偷懒')
    expect(query(fact.to).get('session_id')).toBe(ctx.sessionId)

    const jargon = traceItemLink('jargon', { word: 'v我50', link: { word: 'v我50' } }, ctx)!
    expect(jargon.to.startsWith('/jargon?')).toBe(true)
    expect(query(jargon.to).get('search')).toBe('v我50')

    const lore = traceItemLink('book_lore', { link: { community_id: 'c1', title: '张羽、羽书与土木圣体' } }, ctx)!
    expect(lore.to.startsWith('/knowledge/book-lore?')).toBe(true)
    expect(query(lore.to).get('tab')).toBe('communities')
    expect(query(lore.to).get('bot_id')).toBeNull()

    const person = traceItemLink('affinity', { link: { subject_principal_id: '羽书:user:10000005' } }, ctx)!
    expect(person.to.startsWith('/people?')).toBe(true)
    expect(query(person.to).get('search')).toBe('10000005')
  })

  it('信念、风格样例、心智状态只带作用域；人设类通道没有对象页', () => {
    expect(traceItemLink('belief', {}, ctx)?.to).toBe('/beliefs?bot_id=yushu&session_id=%E7%BE%BD%E4%B9%A6%3Agroup%3A398291136&visibility=group')
    expect(traceItemLink('fewshot', {}, ctx)?.to.startsWith('/knowledge/style-examples?')).toBe(true)
    expect(traceItemLink('soul_state', {}, { botId: 'yushu' })?.to).toBe('/soul?bot_id=yushu')
    expect(traceItemLink('persona', {}, ctx)).toBeNull()
    expect(traceItemLink('holyman_persona', {}, ctx)).toBeNull()
  })

  it('通道、记忆、观测台链接格式', () => {
    expect(channelConfigHref('memory')).toBe('/channels?channel=memory')
    expect(memoryHref(5, { botId: 'yushu' })).toBe('/memories?bot_id=yushu&memory_id=5')
    expect(observatoryTraceHref('active-1', 'yushu')).toBe('/observatory?bot_id=yushu&trace_id=active-1')
  })

  it('按 Bot 与群号找 canonical group session，优先主别名', () => {
    const sessions = [
      { id: 'qq:group:1', bot_id: 'yushu', platform_id: 'qq', kind: 'group', conversation_id: '1', label: '', is_primary_alias: false },
      { id: '羽书:group:1', bot_id: 'yushu', platform_id: '羽书', kind: 'group', conversation_id: '1', label: '', is_primary_alias: true },
      { id: '白真真:group:1', bot_id: 'baizz', platform_id: '白真真', kind: 'group', conversation_id: '1', label: '' },
    ] satisfies SessionOptionDto[]
    expect(sessionForTrace(sessions, 'yushu', '1')).toBe('羽书:group:1')
    expect(sessionForTrace(sessions, 'baizz', '1')).toBe('白真真:group:1')
    expect(sessionForTrace(sessions, 'yushu', '')).toBeUndefined()
  })
})

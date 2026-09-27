import { describe, expect, it } from 'vitest'

import { legacyBotIdsFrom, locateConfig, locationHref, settingsTabFor } from '@/pages/config/config-location'

describe('配置定位规则', () => {
  it('schema 静态配置跳到系统配置对应标签并带上完整键', () => {
    const restart = locateConfig('Memory_Index_Settings.hot_max_vectors', { applyMode: 'restart' })
    expect(restart).toMatchObject({ target: 'settings', pathname: '/settings', params: { key: 'Memory_Index_Settings.hot_max_vectors', tab: 'restart' } })
    expect(restart.label).toBe('系统配置 · 需重启参数')
    expect(locationHref(restart)).toBe('/settings?key=Memory_Index_Settings.hot_max_vectors&tab=restart')

    expect(locateConfig('Query_Settings.inject_top_k', { applyMode: 'next_run' }).params.tab).toBe('static')
    expect(locateConfig('Lifecycle_Settings.enable_dream', { applyMode: 'service' }).params.tab).toBe('static')
    // schema 里 apply_mode=hot 的项只在「全部高级设置」出现
    expect(locateConfig('Query_Settings.min_similarity', { applyMode: 'hot' }).params.tab).toBe('advanced')
    expect(locateConfig('embedding_provider_id').params).toEqual({ key: 'embedding_provider_id', tab: 'advanced' })
  })

  it('热参数跳到系统配置「实时热参数」，键去掉 hot: 前缀', () => {
    const location = locateConfig('hot:spike.max_hops')
    expect(location.params).toEqual({ key: 'spike.max_hops', tab: 'hot' })
    expect(locationHref(location)).toBe('/settings?key=spike.max_hops&tab=hot')
  })

  it('注入通道跳到通道配置对应通道与字段', () => {
    const location = locateConfig('channel:holyman_persona.enabled')
    expect(location).toMatchObject({ target: 'channels', pathname: '/channels', params: { channel: 'holyman_persona', field: 'enabled' } })
    expect(location.label).toBe('通道配置 · holyman_persona')
    expect(locationHref(location)).toBe('/channels?channel=holyman_persona&field=enabled')
  })

  it('Channel_Settings 是通道配置的底层存储，也跳通道配置', () => {
    const location = locateConfig('Channel_Settings.layers', { applyMode: 'next_run' })
    expect(location.target).toBe('channels')
    expect(locationHref(location)).toBe('/channels')
    expect(location.note).toContain('底层存储')
  })

  it('Bot 通道覆盖跳到 Bot 管理对应 Bot 的「通道与工具」', () => {
    const location = locateConfig('bot:yushu.channels.jargon.enabled')
    expect(location).toMatchObject({ target: 'bots', params: { bot: 'yushu', tab: 'advanced' } })
    expect(location.label).toBe('Bot 管理 · yushu · 通道与工具')
    expect(locationHref(location)).toBe('/bots?bot=yushu&tab=advanced')
  })

  it('MetaThinking_BotN 旧槽位跳到 Bot 管理，并提示改旧槽位不生效', () => {
    const ids = legacyBotIdsFrom([
      { key: 'MetaThinking_Bot1.db_id', effective: 'yushu' },
      { key: 'MetaThinking_Bot2.db_id', effective: '  ' },
      { key: 'MetaThinking_Bot1.name', effective: '羽书' },
    ])
    expect(ids).toEqual({ MetaThinking_Bot1: 'yushu' })

    const known = locateConfig('MetaThinking_Bot1.meta_prompt', { legacyBotIds: ids })
    expect(known).toMatchObject({ target: 'bots', params: { bot: 'yushu', tab: 'behavior' } })
    expect(known.note).toContain('旧版静态槽位')

    const unknown = locateConfig('MetaThinking_Bot2.qq_id', { legacyBotIds: ids })
    expect(unknown.params).toEqual({ tab: 'identity' })
    expect(locationHref(unknown)).toBe('/bots?tab=identity')
  })

  it('MetaThinking_Settings（全局对话规则）不是旧槽位，仍在系统配置', () => {
    expect(locateConfig('MetaThinking_Settings.enabled', { applyMode: 'restart' }).target).toBe('settings')
  })

  it('查询参数会被正确转义', () => {
    expect(locationHref(locateConfig('channel:a b.top_k'))).toBe('/channels?channel=a+b&field=top_k')
  })

  it('settingsTabFor 与 SettingsPage 的过滤一致', () => {
    expect(settingsTabFor('restart')).toBe('restart')
    expect(settingsTabFor('next_run')).toBe('static')
    expect(settingsTabFor('service')).toBe('static')
    expect(settingsTabFor('unknown')).toBe('advanced')
    expect(settingsTabFor(undefined)).toBe('advanced')
  })
})

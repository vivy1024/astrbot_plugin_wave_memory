import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { ConfigGroup, HotParam } from '@/api/config'
import type { ConfigInventoryRow } from '@/api/configInventory'
import { buildConfigEntries, displayValue, type ConfigEntry } from '@/pages/config/config-catalog'
import { FEATURE_GROUPS, featureOf, groupByFeature, searchConfig } from '@/pages/config/feature-groups'

const SCHEMA_PATH = resolve(__dirname, '../../../../../_conf_schema.json')

function entry(key: string, overrides: Partial<ConfigEntry> = {}): ConfigEntry {
  return {
    key,
    kind: 'static',
    section: key.split('.')[0],
    title: key,
    hint: '',
    type: 'int',
    defaultValue: 1,
    savedValue: 1,
    effectiveValue: 1,
    applyMode: 'next_run',
    restartRequired: false,
    layer: 'static',
    source: 'plugin_config',
    changed: false,
    error: null,
    ...overrides,
  }
}

const state = {
  saved: 1,
  value: 1,
  source: 'plugin_config',
  effective_source: 'runtime_startup_snapshot',
  apply_mode: 'next_run' as const,
  restart_required: false,
  restart_requirement: 'not_required' as const,
}

describe('按功能分组规则', () => {
  it('_conf_schema.json 的每个章节都归入具体功能组，不落进「其他设置」', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, { type?: string; items?: Record<string, unknown> }>
    const unassigned: string[] = []
    for (const [section, meta] of Object.entries(schema)) {
      if (section === '_system_status') continue
      const keys = meta.type === 'object' ? Object.keys(meta.items ?? {}).map((field) => `${section}.${field}`) : [section]
      for (const key of keys) if (featureOf(key) === 'other') unassigned.push(key)
    }
    expect(unassigned).toEqual([])
  })

  it('章节、单键覆盖、热参数前缀与通道/Bot 前缀各自命中', () => {
    expect(featureOf('Query_Settings.inject_top_k')).toBe('recall')
    expect(featureOf('Tag_Settings.max_tags_per_message')).toBe('learning')
    expect(featureOf('WebUI_Settings.webui_host')).toBe('security')
    expect(featureOf('backup_max_count')).toBe('maintenance')
    expect(featureOf('embedding_provider_id')).toBe('index')
    // 单键覆盖优先于章节
    expect(featureOf('Inject_Settings.facts_max')).toBe('lore')
    expect(featureOf('Inject_Settings.skip_recent_minutes')).toBe('reply')
    expect(featureOf('Memory_Index_Settings.cold_recall_enabled')).toBe('recall')
    expect(featureOf('Memory_Index_Settings.hot_max_vectors')).toBe('index')
    // 旧 Bot 槽位按对话规则
    expect(featureOf('MetaThinking_Bot3.meta_prompt')).toBe('reply')
    // 热参数前缀
    expect(featureOf('hot:spike.max_hops')).toBe('recall')
    expect(featureOf('hot:social.abuse_trigger_count')).toBe('mind')
    expect(featureOf('hot:ingress.debounce_seconds')).toBe('reply')
    expect(featureOf('hot:injection.slow_warning_ms')).toBe('maintenance')
    expect(featureOf('hot:brand_new.x')).toBe('other')
    expect(featureOf('channel:jargon.enabled')).toBe('reply')
    expect(featureOf('bot:yushu.channels.jargon.enabled')).toBe('reply')
    // 未登记的新章节兜底，而不是消失
    expect(featureOf('Brand_New_Settings.x')).toBe('other')
  })

  it('groupByFeature 按固定顺序分桶、统计改过的项并跳过空组', () => {
    const buckets = groupByFeature([
      entry('Tag_Settings.max_tags_per_message', { changed: true }),
      entry('Query_Settings.inject_top_k'),
      entry('Brand_New_Settings.x'),
      entry('Tag_Settings.tag_batch_size'),
    ])
    expect(buckets.map((bucket) => bucket.group.id)).toEqual(['recall', 'learning', 'other'])
    const learning = buckets.find((bucket) => bucket.group.id === 'learning')!
    expect(learning.entries.map((item) => item.key)).toEqual(['Tag_Settings.max_tags_per_message', 'Tag_Settings.tag_batch_size'])
    expect(learning.changedCount).toBe(1)
  })

  it('每个功能组都有一句「这组管什么」', () => {
    for (const group of FEATURE_GROUPS) expect(group.description.length).toBeGreaterThan(5)
  })
})

describe('配置搜索', () => {
  const entries = [
    entry('Query_Settings.inject_top_k', { title: '单次最多注入几条记忆' }),
    entry('Tag_Settings.max_tags_per_message', { title: '单条消息最大标签数' }),
    entry('Tag_Settings.tag_batch_size', { title: '标签提炼实时队列批次大小', changed: true }),
    entry('Tag_Settings.graph_legend_enabled', { title: '显示神经星云图例' }),
    entry('WebUI_Settings.webui_port', { title: '控制台访问端口' }),
  ]

  it('按键名、中文名称搜索，直接命中排在症状命中之前', () => {
    expect(searchConfig(entries, 'webui_port').map((hit) => hit.entry.key)).toEqual(['WebUI_Settings.webui_port'])
    const hits = searchConfig(entries, '标签')
    expect(hits.slice(0, 2).map((hit) => hit.bySymptom)).toEqual([false, false])
  })

  it('按症状搜索：只带出有该症状词的键，不把同组无关的改动项带出来', () => {
    const hits = searchConfig(entries, '标签太多')
    const keys = hits.map((hit) => hit.entry.key)
    expect(keys).toEqual(['Tag_Settings.max_tags_per_message'])
    expect(hits[0]).toMatchObject({ bySymptom: true, symptom: '标签太多' })
    expect(hits[0].group.title).toBe('标签与学习')
    // 搜分组名带出整组
    expect(searchConfig(entries, '标签与学习').length).toBe(3)
    // 用户多打几个字也能命中
    expect(searchConfig(entries, '回复太长了').map((hit) => hit.entry.key)).toContain('Query_Settings.inject_top_k')
  })

  it('空搜索不返回结果，忽略空白', () => {
    expect(searchConfig(entries, '   ')).toEqual([])
    expect(searchConfig(entries, 'webui _port').length).toBe(1)
  })
})

describe('配置条目构建', () => {
  it('合并 schema、热参数与 inventory：来源层、已改、共用存储与通道条目', () => {
    const groups: ConfigGroup[] = [
      { key: 'embedding_dimension', kind: 'scalar', type: 'int', description: '特征向量维度', hint: '', default: 1024, effective: 1024, ...state, apply_mode: 'restart', restart_required: true },
      { key: '_system_status', kind: 'scalar', type: 'string', description: '说明', hint: '', default: '', effective: '', ...state },
      {
        key: 'WebUI_Settings', kind: 'object', description: '管理控制台', hint: '',
        items: [{ key: 'webui_password', type: 'string', description: '访问登录密码', hint: '', special: '', default: '', effective: 'hunter2', ...state }],
      },
    ]
    const hot: HotParam[] = [{ key: 'query.group_weight_cross', type: 'float', min: 0.1, max: 1, default: 0.8, current: 0.7, effective: 0.7, description: '跨群权重' }]
    const rows: ConfigInventoryRow[] = [
      { key: 'embedding_dimension', layer: 'static', title: '', scope: 'global', default: 1024, saved: 1024, effective: 1024, source: 'plugin_config', apply_mode: 'restart', changed: false, warning: null },
      { key: 'hot:query.group_weight_cross', layer: 'static', title: '', scope: 'global', default: 0.8, saved: 0.7, effective: 0.7, source: 'plugin_config.Social_Settings.group_weight_cross', apply_mode: 'hot', changed: true, warning: null },
      { key: 'channel:holyman_persona.enabled', layer: 'override', title: '注入通道 holyman_persona · enabled', scope: 'global', default: false, saved: null, effective: true, source: 'Channel_Settings', apply_mode: 'hot', changed: true, warning: null },
    ]
    const built = buildConfigEntries({ schemaGroups: groups, hotParams: hot, inventoryRows: rows })
    expect(built.map((item) => item.key)).toEqual(['embedding_dimension', 'WebUI_Settings.webui_password', 'hot:query.group_weight_cross', 'channel:holyman_persona.enabled'])
    const [dimension, password, weight, channel] = built
    expect(dimension).toMatchObject({ layer: 'static', changed: false, restartRequired: true })
    expect(password).toMatchObject({ layer: null, changed: true })
    expect(weight).toMatchObject({ kind: 'hot', mirrorOf: 'Social_Settings.group_weight_cross', changed: true })
    expect(channel).toMatchObject({ kind: 'channel', layer: 'override', changed: true })
  })

  it('密码类只显示是否已设置', () => {
    expect(displayValue('WebUI_Settings.webui_password', 'hunter2')).toBe('已设置（已隐藏）')
    expect(displayValue('WebUI_Settings.webui_password', '')).toBe('未设置')
    expect(displayValue('Query_Settings.enable_auto_inject', false)).toBe('关闭')
    expect(displayValue('x', '')).toBe('（空）')
  })
})

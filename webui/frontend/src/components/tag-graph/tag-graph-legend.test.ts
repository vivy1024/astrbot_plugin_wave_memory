import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { buildLegendItems, typeLabel, paletteFor } from '@/components/tag-graph/TagGraphCanvas'

const ROOT = resolve(__dirname, '../../..')
// ROOT 指向 webui/frontend，插件根目录还要再上两级。
const PLUGIN_ROOT = resolve(ROOT, '../..')
const read = (relative: string) => readFileSync(resolve(ROOT, relative), 'utf-8')
const readPlugin = (relative: string) => readFileSync(resolve(PLUGIN_ROOT, relative), 'utf-8')

describe('标签图谱图例配置化', () => {
  it('按配置顺序输出图例项，且只保留图中实际出现的类型', () => {
    const nodes = [{ type: 'fact' }, { type: 'keyword' }, { type: 'keyword' }]
    const items = buildLegendItems(nodes, { types: ['keyword', 'entity', 'fact'] })
    // entity 图里没有 => 不出现；顺序跟随配置
    expect(items.map((item) => item.type)).toEqual(['keyword', 'fact'])
    expect(items[0].count).toBe(2)
    expect(items[1].count).toBe(1)
  })

  it('配置为空时回退到图中出现的顺序，不丢类型', () => {
    const nodes = [{ type: 'topic' }, { type: 'jargon' }]
    expect(buildLegendItems(nodes, { types: [] }).map((item) => item.type)).toEqual(['topic', 'jargon'])
    expect(buildLegendItems(nodes, null).map((item) => item.type)).toEqual(['topic', 'jargon'])
  })

  it('类型大小写与未知类型都能稳定处理', () => {
    const items = buildLegendItems([{ type: 'KEYWORD' }, { type: undefined }], null)
    expect(items.map((item) => item.type)).toEqual(['keyword', 'default'])
  })

  it('每个图例项都带类型对应颜色，颜色不脱钩', () => {
    const items = buildLegendItems([{ type: 'emotion' }], null)
    expect(items[0].color).toBe(paletteFor('emotion').core)
    expect(typeLabel('emotion')).toBe('情绪')
    // 未知类型回退到 default 调色板，不抛错
    expect(paletteFor('不存在的类型')).toBe(paletteFor('default'))
  })

  it('schema 暴露了图例的类型顺序、显隐与计数三项配置', () => {
    const schema = JSON.parse(readPlugin('_conf_schema.json'))
    const items = schema.Tag_Settings.items
    expect(items.graph_legend_enabled.type).toBe('bool')
    expect(items.graph_legend_types.type).toBe('string')
    expect(items.graph_legend_show_count.type).toBe('bool')
    // 顺序配置的默认值必须覆盖全部已知类型
    const known = ['keyword', 'entity', 'topic', 'emotion', 'fact', 'jargon', 'default']
    for (const type of known) expect(items.graph_legend_types.default).toContain(type)
  })

  it('图谱蓝图会过滤未知类型名并把图例配置塞进 payload', () => {
    const blueprint = readPlugin('webui/blueprints/tag_graph.py')
    expect(blueprint).toContain('_LEGEND_KNOWN_TYPES')
    expect(blueprint).toContain('_legend_settings')
    expect(blueprint).toContain('payload["legend"]')
  })
})

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { buildLegendItems, hasDedicatedColor, nodeColor, nodeTypeLabel, normalizeNodeType } from '@/lib/graph-palette'

const PLUGIN_ROOT = resolve(__dirname, '../../../..')
const readPlugin = (relative: string) => readFileSync(resolve(PLUGIN_ROOT, relative), 'utf-8')

// 两种主题分别对白底卡片 / 深色卡片校验过的 8 个分类色（dataviz validate_palette）：
// 相邻 CVD ΔE ≥ 8.4、正常视觉 ΔE ≥ 19.3。改动这里必须重新跑校验器。
const EXPECTED: Record<string, { light: string; dark: string }> = {
  topic: { light: '#2a78d6', dark: '#3987e5' },
  person: { light: '#eb6834', dark: '#d95926' },
  entity: { light: '#1baf7a', dark: '#199e70' },
  event: { light: '#eda100', dark: '#c98500' },
  emotion: { light: '#e87ba4', dark: '#d55181' },
  location: { light: '#008300', dark: '#008300' },
  keyword: { light: '#4a3aa7', dark: '#9085e9' },
  jargon: { light: '#e34948', dark: '#e66767' },
}

const HEX = /^#[0-9a-f]{6}$/i

describe('graph-palette', () => {
  it('同一类型在浅色 / 深色下都返回校验过的色相', () => {
    for (const [type, expected] of Object.entries(EXPECTED)) {
      expect(nodeColor(type, 'light'), `${type} light`).toBe(expected.light)
      expect(nodeColor(type, 'dark'), `${type} dark`).toBe(expected.dark)
      expect(hasDedicatedColor(type)).toBe(true)
    }
  })

  it('未知类型与空值都落到「其他」灰，且两个主题都有值', () => {
    for (const value of ['default', '', undefined, null, '做梦']) {
      expect(nodeColor(value as string, 'light')).toMatch(HEX)
      expect(nodeColor(value as string, 'light')).toBe(nodeColor('default', 'light'))
      expect(hasDedicatedColor(value as string)).toBe(false)
    }
  })

  it('类型键大小写与空白不敏感（entity / Entity / " entity "）', () => {
    expect(normalizeNodeType(' ENTITY ')).toBe('entity')
    expect(nodeColor('Entity', 'dark')).toBe(nodeColor('entity', 'dark'))
    expect(nodeTypeLabel('PERSON')).toBe('人物')
  })

  it('中文类型名覆盖知识图谱与标签图的全部已知类型', () => {
    expect(nodeTypeLabel('entity')).toBe('实体')
    expect(nodeTypeLabel('person')).toBe('人物')
    expect(nodeTypeLabel('emotion')).toBe('情绪')
    expect(nodeTypeLabel('fact')).toBe('事实')
    expect(nodeTypeLabel('bot')).toBe('Bot')
    expect(nodeTypeLabel('没见过的类型')).toBe('没见过的类型')
  })
})

describe('buildLegendItems', () => {
  it('按配置顺序输出，只保留图中实际出现的类型，并带上数量与颜色', () => {
    const items = buildLegendItems(['topic', 'keyword', 'keyword', 'Entity'], 'light', ['keyword', 'entity', 'topic'])
    expect(items.map((item) => item.type)).toEqual(['keyword', 'entity', 'topic'])
    expect(items.map((item) => item.count)).toEqual([2, 1, 1])
    expect(items[0].color).toBe(nodeColor('keyword', 'light'))
    expect(items.map((item) => item.label)).toEqual(['关键词', '实体', '话题'])
  })

  it('没有配置顺序时按数量降序补齐，不丢类型', () => {
    const items = buildLegendItems(['topic', 'jargon', 'topic'], 'dark')
    expect(items.map((item) => item.type)).toEqual(['topic', 'jargon'])
    expect(items[0].count).toBe(2)
  })
})

describe('图例配置仍然来自后端 schema 与蓝图', () => {
  it('schema 暴露图例顺序 / 显隐 / 计数三项，且默认值覆盖全部已知类型', () => {
    const schema = JSON.parse(readPlugin('_conf_schema.json'))
    const items = schema.Tag_Settings.items
    expect(items.graph_legend_enabled.type).toBe('bool')
    expect(items.graph_legend_types.type).toBe('string')
    expect(items.graph_legend_show_count.type).toBe('bool')
    const known = ['keyword', 'entity', 'topic', 'emotion', 'fact', 'person', 'event', 'location', 'time', 'jargon', 'default']
    for (const type of known) expect(items.graph_legend_types.default).toContain(type)
  })

  it('图谱蓝图过滤未知类型名并把图例配置塞进 payload', () => {
    const blueprint = readPlugin('webui/blueprints/tag_graph.py')
    expect(blueprint).toContain('_LEGEND_KNOWN_TYPES')
    expect(blueprint).toContain('_legend_settings')
    expect(blueprint).toContain('payload["legend"]')
  })
})

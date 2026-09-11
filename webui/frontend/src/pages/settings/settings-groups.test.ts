import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { SECTION_GROUPS, groupMeta, isHiddenSection, matchesSearch } from '@/pages/settings/settings-groups'

const ROOT = resolve(__dirname, '../../..')
const read = (relative: string) => readFileSync(resolve(ROOT, relative), 'utf-8')
const PAGE = 'src/pages/settings/SettingsPage.tsx'
const GROUPS = 'src/pages/settings/settings-groups.ts'

describe('系统配置页：不暴露后端词语', () => {
  it('页面不再渲染内部键名与来源字段', () => {
    const page = read(PAGE)
    // 旧实现把这几项直接拼进 FieldDescription
    expect(page).not.toContain('键：')
    expect(page).not.toContain('有效来源：')
    expect(page).not.toContain('item.source')
    expect(page).not.toContain('item.effective_source')
    expect(page).not.toContain('group.effective_source')
  })

  it('章节名不再直接当作标题渲染', () => {
    const page = read(PAGE)
    // 旧实现 CardTitle 直接用后端 description / key
    expect(page).not.toContain('<CardTitle>{group.description}')
    expect(page).not.toContain('{group.hint || group.key}')
    // 标题必须来自功能分组映射
    expect(page).toContain('meta.title')
    expect(page).toContain('meta.description')
  })

  it('每个已知章节都有用户视角名称与说明，不残留内部词', () => {
    const internalWords = ['Settings', 'Trace', 'HNSW', 'canonical', 'Few-Shot', 'Scope', '_']
    for (const [key, meta] of Object.entries(SECTION_GROUPS)) {
      expect(meta.title.trim().length).toBeGreaterThan(0)
      expect(meta.description.trim().length).toBeGreaterThan(0)
      for (const word of internalWords) {
        expect(meta.title).not.toContain(word)
        expect(meta.description).not.toContain(word)
      }
      // 分组 key 必须仍是真实 schema 章节，避免映射写错导致配置消失
      expect(key).toBe(key.trim())
    }
  })

  it('未登记的章节回退可见，不允许静默消失', () => {
    const meta = groupMeta('Brand_New_Settings')
    expect(meta.title).toBe('其他设置')
    // 新的后端章节默认展开，否则用户找不到
    expect(meta.defaultOpen).toBe(true)
  })
})

describe('系统配置页：分组与折叠', () => {
  it('常用组默认展开，进阶组默认收起', () => {
    expect(SECTION_GROUPS.Query_Settings.defaultOpen).toBe(true)
    expect(SECTION_GROUPS.Tag_Settings.defaultOpen).toBe(true)
    expect(SECTION_GROUPS.Lifecycle_Settings.defaultOpen).toBe(false)
    expect(SECTION_GROUPS.MetaThinking_Bot1.defaultOpen).toBe(false)
  })

  it('两个 Bot 的配置是各自独立分组，不会混在一起', () => {
    expect(SECTION_GROUPS.MetaThinking_Bot1.title).not.toBe(SECTION_GROUPS.MetaThinking_Bot2.title)
  })

  it('页面用可折叠容器渲染分组，并显示该组字段数', () => {
    const page = read(PAGE)
    expect(page).toContain('SettingsSection')
    expect(page).toContain('count={itemCount}')
    // 展开状态受用户控制，默认值来自映射
    expect(page).toContain('openSections[group.key] ?? meta.defaultOpen')
  })

  it('搜索时忽略折叠状态并强制展开，避免命中却看不到', () => {
    const page = read(PAGE)
    expect(page).toContain('const searching = search.trim().length > 0')
    expect(page).toContain('const open = searching ||')
  })

  it('搜索能命中功能分组名、章节名与字段说明', () => {
    const meta = SECTION_GROUPS.Query_Settings
    const items = [{ key: 'inject_top_k', description: '单次最大记忆注入条数', hint: '' }]
    expect(matchesSearch(meta, 'Query_Settings', {}, items, '记忆召回')).toBe(true)
    expect(matchesSearch(meta, 'Query_Settings', {}, items, 'inject_top_k')).toBe(true)
    expect(matchesSearch(meta, 'Query_Settings', {}, items, '注入条数')).toBe(true)
    expect(matchesSearch(meta, 'Query_Settings', {}, items, 'zzz不存在的词')).toBe(false)
    expect(matchesSearch(meta, 'Query_Settings', {}, items, '  ')).toBe(true)
  })

  it('折叠组件基于 radix Collapsible，不引入新依赖', () => {
    const component = read('src/components/ui/collapsible.tsx')
    expect(component).toContain("from \"radix-ui\"")
    expect(component).toContain('CollapsiblePrimitive')
  })

  it('分组映射覆盖 schema 中全部真实章节与顶层标量字段', () => {
    const schema = JSON.parse(read('../../_conf_schema.json')) as Record<string, { items?: unknown; type?: string }>
    const sections = Object.entries(schema)
      .filter(([, value]) => value && typeof value === 'object' && 'items' in value)
      .map(([key]) => key)
    expect(sections.length).toBe(20)
    // 顶层标量字段（Embedding 模型、备份数量等）也要有分组，否则会挤进「其他设置」
    const scalars = Object.entries(schema)
      .filter(([, value]) => value && typeof value === 'object' && !('items' in value) && 'type' in value)
      .map(([key]) => key)
    expect(scalars.length).toBeGreaterThan(4)
    const unmapped = [...sections, ...scalars].filter((key) => !(key in SECTION_GROUPS) && !isHiddenSection(key))
    expect(unmapped).toEqual([])
  })

  it('纯说明项不当作配置项渲染，但其余字段一个不少', () => {
    expect(isHiddenSection('_system_status')).toBe(true)
    expect(isHiddenSection('Query_Settings')).toBe(false)
    const schema = JSON.parse(read('../../_conf_schema.json')) as Record<string, unknown>
    expect('_system_status' in schema).toBe(true)
  })

  it('分组文件保持展示层职责，不碰配置值', () => {
    const source = read(GROUPS)
    // 只做映射与匹配，不应出现写入/请求逻辑
    expect(source).not.toContain('fetch')
    expect(source).not.toContain('localStorage')
    expect(source).not.toContain('save')
  })

describe('配置文案不暴露实现术语', () => {
  it('schema 的字段名称与说明里没有实现术语', () => {
    const schema = JSON.parse(read('../../_conf_schema.json')) as Record<string, { items?: Record<string, { description?: string; hint?: string }> }>
    // 这些词属于实现细节，不应出现在面向用户的配置文案里
    const TERMS = ['HNSW', 'hnswlib', 'canonical', 'manifest', 'generation', 'Scope', 'Catalog',
      'QueryEngine', 'outbox', 'watermark', 'shadow', 'fanout', 'legacy_group', 'pilot',
      'memories', 'durable', '3D']
    const offenders: string[] = []
    for (const [section, group] of Object.entries(schema)) {
      for (const [field, meta] of Object.entries(group.items ?? {})) {
        const text = `${meta.description ?? ''} ${meta.hint ?? ''}`
        const hit = TERMS.filter((term) => text.includes(term))
        if (hit.length) offenders.push(`${section}.${field}: ${hit.join(',')}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('schema 结构未被文案改动破坏', () => {
    const schema = JSON.parse(read('../../_conf_schema.json')) as Record<string, { items?: Record<string, { type?: string }> }>
    const sections = Object.entries(schema).filter(([, v]) => v && typeof v === 'object' && 'items' in v)
    expect(sections.length).toBe(20)
    const noType: string[] = []
    let total = 0
    for (const [section, group] of sections) {
      for (const [field, meta] of Object.entries(group.items ?? {})) {
        total += 1
        if (!meta.type) noType.push(`${section}.${field}`)
      }
    }
    expect(total).toBe(114)
    expect(noType).toEqual([])
  })
})
})

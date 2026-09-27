/**
 * 配置中心的统一条目：把 SettingsPage 用的 schema / 热参数，与配置来源页用的 inventory
 * 摊平成同一种结构，供「按功能」「高风险」与全局搜索共用。只读，不参与保存。
 */
import type { ConfigGroup, HotParam } from '@/api/config'
import type { ConfigInventoryRow, ConfigLayer } from '@/api/configInventory'
import { stableStringify } from '@/lib/stable-json'
import { isHiddenSection } from '@/pages/settings/settings-groups'

export type ConfigEntryKind = 'static' | 'hot' | 'channel' | 'bot'

export interface ConfigEntry {
  /** 与 inventory 一致的键：`Section.item` / `scalar` / `hot:x` / `channel:n.f` / `bot:id.channels.n.f` */
  key: string
  kind: ConfigEntryKind
  /** schema 章节键；热参数为 `hot`，通道为 `channel`，Bot 覆盖为 `bot` */
  section: string
  title: string
  hint: string
  type: string
  defaultValue: unknown
  savedValue: unknown
  effectiveValue: unknown
  applyMode: string
  restartRequired: boolean
  /** 四层来源；inventory 不可用时为 null */
  layer: ConfigLayer | null
  source: string
  changed: boolean
  error: string | null
  /** 热参数与哪个静态键共用存储（inventory source = `plugin_config.<键>`） */
  mirrorOf?: string
}

export const LAYER_LABELS: Record<ConfigLayer, string> = {
  builtin: '内置默认',
  static: 'AstrBot 静态配置',
  bot: 'Bot Profile',
  override: '9876 覆盖',
}

export const APPLY_LABELS: Record<string, string> = {
  hot: '热生效',
  next_run: '保存即生效',
  service: '重建服务生效',
  restart: '需重启',
  unknown: '生效方式未知',
}

export function sameValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true
  try {
    return stableStringify(left) === stableStringify(right)
  } catch {
    return false
  }
}

const SECRET_FIELD = /(password|passwd|secret|token|api_?key)/i

export function isSecretKey(key: string): boolean {
  const field = key.slice(key.lastIndexOf('.') + 1)
  return SECRET_FIELD.test(field)
}

/** 展示值；密码类只显示「已设置 / 未设置」，不把明文渲染到页面上。 */
export function displayValue(key: string, value: unknown): string {
  if (isSecretKey(key)) return value === null || value === undefined || value === '' ? '未设置' : '已设置（已隐藏）'
  if (value === null || value === undefined) return '未配置'
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if (typeof value === 'string') return value === '' ? '（空）' : value
  if (typeof value === 'number') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export interface CatalogInput {
  schemaGroups: ConfigGroup[]
  hotParams?: HotParam[] | null
  inventoryRows?: ConfigInventoryRow[] | null
}

export function buildConfigEntries({ schemaGroups, hotParams, inventoryRows }: CatalogInput): ConfigEntry[] {
  const rowsByKey = new Map((inventoryRows ?? []).map((row) => [row.key, row]))
  const entries: ConfigEntry[] = []

  for (const group of schemaGroups) {
    if (isHiddenSection(group.key)) continue
    const items = group.kind === 'object' ? (group.items ?? []) : [group]
    for (const item of items) {
      const key = group.kind === 'object' ? `${group.key}.${item.key}` : group.key
      const row = rowsByKey.get(key)
      entries.push({
        key,
        kind: 'static',
        section: group.key,
        title: item.description || key,
        hint: item.hint ?? '',
        type: item.type ?? '',
        defaultValue: item.default,
        savedValue: item.saved,
        effectiveValue: item.effective,
        applyMode: item.apply_mode ?? 'unknown',
        restartRequired: item.restart_required === true,
        layer: row?.layer ?? null,
        source: row?.source ?? item.source ?? '',
        changed: !sameValue(item.effective, item.default),
        error: item.error ?? row?.warning ?? null,
      })
    }
  }

  for (const param of hotParams ?? []) {
    const key = `hot:${param.key}`
    const row = rowsByKey.get(key)
    const source = row?.source ?? param.source ?? ''
    const mirror = /^plugin_config\.([^+]+)/.exec(source)?.[1]
    const effective = param.effective ?? param.current
    entries.push({
      key,
      kind: 'hot',
      section: 'hot',
      title: param.description || param.key,
      hint: `实时热参数 ${param.key}；范围 ${param.min}–${param.max}`,
      type: param.type,
      defaultValue: param.default,
      savedValue: param.saved,
      effectiveValue: effective,
      applyMode: 'hot',
      restartRequired: false,
      layer: row?.layer ?? null,
      source,
      changed: !sameValue(effective, param.default),
      error: param.error ?? row?.warning ?? null,
      ...(mirror ? { mirrorOf: mirror } : {}),
    })
  }

  for (const row of inventoryRows ?? []) {
    const kind: ConfigEntryKind | null = row.key.startsWith('channel:') ? 'channel' : row.key.startsWith('bot:') ? 'bot' : null
    if (!kind) continue
    entries.push({
      key: row.key,
      kind,
      section: kind,
      title: row.title,
      hint: row.scope !== 'global' ? row.scope : '',
      type: '',
      defaultValue: row.default,
      savedValue: row.saved,
      effectiveValue: row.effective,
      applyMode: row.apply_mode,
      restartRequired: row.apply_mode === 'restart',
      layer: row.layer,
      source: row.source,
      changed: row.changed,
      error: row.warning,
    })
  }

  return entries
}

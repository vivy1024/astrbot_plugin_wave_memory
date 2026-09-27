import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { ConfigEntry } from '@/pages/config/config-catalog'
import { RISK_RULES, assessRisks, isLoopbackHost } from '@/pages/config/risk'

const SCHEMA_PATH = resolve(__dirname, '../../../../../_conf_schema.json')

function entry(key: string, effectiveValue: unknown, defaultValue: unknown): ConfigEntry {
  return {
    key,
    kind: 'static',
    section: key.split('.')[0],
    title: key,
    hint: '',
    type: '',
    defaultValue,
    savedValue: effectiveValue,
    effectiveValue,
    applyMode: 'restart',
    restartRequired: true,
    layer: 'static',
    source: 'plugin_config',
    changed: effectiveValue !== defaultValue,
    error: null,
  }
}

function statusOf(entries: ConfigEntry[], key: string) {
  return assessRisks(entries).find((item) => item.rule.key === key)!
}

describe('高风险判定', () => {
  it('webui_host=0.0.0.0 且无密码：host 与 password 都标危险并说明后果', () => {
    const entries = [entry('WebUI_Settings.webui_host', '0.0.0.0', '127.0.0.1'), entry('WebUI_Settings.webui_password', '', '')]
    const host = statusOf(entries, 'WebUI_Settings.webui_host')
    expect(host.status).toBe('danger')
    expect(host.message).toContain('无需登录')
    expect(statusOf(entries, 'WebUI_Settings.webui_password').status).toBe('danger')
    // 危险项排在最前
    expect(assessRisks(entries).slice(0, 2).map((item) => item.status)).toEqual(['danger', 'danger'])
  })

  it('webui_host=0.0.0.0 但已设密码：只提示确认网络，密码不再危险', () => {
    const entries = [entry('WebUI_Settings.webui_host', '0.0.0.0', '127.0.0.1'), entry('WebUI_Settings.webui_password', 'x', '')]
    expect(statusOf(entries, 'WebUI_Settings.webui_host').status).toBe('warn')
    expect(statusOf(entries, 'WebUI_Settings.webui_password').status).toBe('normal')
  })

  it('仅本机监听时空密码是正常状态', () => {
    const entries = [entry('WebUI_Settings.webui_host', '127.0.0.1', '127.0.0.1'), entry('WebUI_Settings.webui_password', '', '')]
    expect(statusOf(entries, 'WebUI_Settings.webui_host').status).toBe('normal')
    expect(statusOf(entries, 'WebUI_Settings.webui_password').status).toBe('normal')
  })

  it('偏离默认标记为 deviated；默认只是占位的 Provider 设置后不算偏离', () => {
    const entries = [
      entry('Memory_Index_Settings.hot_max_vectors', 100000, 40000),
      entry('embedding_provider_id', 'siliconflow/Qwen3-Embedding-0.6B', ''),
      entry('tag_llm_provider_id', 'nug/gemini', ''),
    ]
    expect(statusOf(entries, 'Memory_Index_Settings.hot_max_vectors').status).toBe('deviated')
    expect(statusOf(entries, 'embedding_provider_id').status).toBe('normal')
    expect(statusOf(entries, 'tag_llm_provider_id').status).toBe('normal')
  })

  it('Provider 留空、自动注入关闭都是危险', () => {
    const entries = [
      entry('embedding_provider_id', '', ''),
      entry('tag_llm_provider_id', '', ''),
      entry('Tag_Settings.tag_extraction_enabled', true, true),
      entry('Query_Settings.enable_auto_inject', false, true),
    ]
    expect(statusOf(entries, 'embedding_provider_id').status).toBe('danger')
    expect(statusOf(entries, 'tag_llm_provider_id').status).toBe('danger')
    expect(statusOf(entries, 'Query_Settings.enable_auto_inject').status).toBe('danger')
    // 标签提炼关闭时，标签模型留空不算危险
    const off = [entry('tag_llm_provider_id', '', ''), entry('Tag_Settings.tag_extraction_enabled', false, true)]
    expect(statusOf(off, 'tag_llm_provider_id').status).toBe('normal')
  })

  it('跨项判定：compat_only 下强行开原生注入、噪声保留期缩短', () => {
    const entries = [
      entry('Runtime_Settings.runtime_mode', 'compat_only', 'full'),
      entry('Compatibility_Settings.compat_only_auto_inject_enabled', true, false),
      entry('Eviction_Settings.noise_ttl_days', 3, 7),
    ]
    expect(statusOf(entries, 'Runtime_Settings.runtime_mode').status).toBe('warn')
    expect(statusOf(entries, 'Compatibility_Settings.compat_only_auto_inject_enabled').status).toBe('warn')
    expect(statusOf(entries, 'Eviction_Settings.noise_ttl_days').message).toContain('3 天')
  })

  it('当前配置里没有的键标为 missing 并排在最后', () => {
    const results = assessRisks([entry('WebUI_Settings.webui_host', '0.0.0.0', '127.0.0.1')])
    expect(results[0].rule.key).toBe('WebUI_Settings.webui_host')
    expect(results.at(-1)!.status).toBe('missing')
  })

  it('isLoopbackHost', () => {
    expect(isLoopbackHost('127.0.0.1')).toBe(true)
    expect(isLoopbackHost('127.0.1.1')).toBe(true)
    expect(isLoopbackHost('localhost')).toBe(true)
    expect(isLoopbackHost('::1')).toBe(true)
    expect(isLoopbackHost('')).toBe(true)
    expect(isLoopbackHost('0.0.0.0')).toBe(false)
    expect(isLoopbackHost('192.168.1.2')).toBe(false)
  })

  it('风险表里的键都真实存在于 _conf_schema.json，且需重启标记与 schema 一致', () => {
    const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Record<string, { type?: string; restart_required?: boolean; items?: Record<string, { restart_required?: boolean }> }>
    const unknown: string[] = []
    const mismatched: string[] = []
    for (const rule of RISK_RULES) {
      const [section, field] = rule.key.split('.')
      const meta = field ? schema[section]?.items?.[field] : schema[section]
      if (!meta) { unknown.push(rule.key); continue }
      if (typeof meta.restart_required === 'boolean' && meta.restart_required !== rule.restart) mismatched.push(rule.key)
    }
    expect(unknown).toEqual([])
    expect(mismatched).toEqual([])
    for (const rule of RISK_RULES) {
      expect(rule.reason.length).toBeGreaterThan(4)
      expect(rule.impact.length).toBeGreaterThan(4)
    }
  })
})

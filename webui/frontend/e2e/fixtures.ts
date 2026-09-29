import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test as base, expect, type Page } from '@playwright/test'

interface ApiFixture {
  databasePath: string
  get(pathname: string): unknown
}

interface FrontendFixtures {
  apiFixture: ApiFixture
  authenticatedPage: Page
}

export const test = base.extend<FrontendFixtures>({
  apiFixture: async ({ browserName: _browserName }, provide) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'wavememory-frontend-'))
    const databasePath = path.join(directory, 'api-fixture.sqlite3')
    const database = new DatabaseSync(databasePath)
    database.exec('CREATE TABLE api_responses (pathname TEXT PRIMARY KEY, payload_json TEXT NOT NULL)')
    const insert = database.prepare('INSERT INTO api_responses(pathname, payload_json) VALUES (?, ?)')
    const emptyPage = {
      items: [],
      page: { total: 0, total_status: 'exact', reason_code: null, limit: 50, offset: 0, page: 1, page_count: 0, has_more: false },
    }
    const unavailable = { available: false, reason_code: 'e2e_fixture_empty' }
    const responses: Record<string, unknown> = {
      '/api/auth/check': { requires_auth: true },
      '/api/options/scopes': { bots: [], sessions: [], channels: [], generated_at: 0, source: { health: 'empty', reason_code: 'e2e_fixture_empty' } },
      '/api/system': { memories: { total: 0 }, coverage: { vector_pct: 0, tag_pct: 0 }, tags: { structured: 0 }, todos: { untagged_count: 0, pending_fewshot: 0, has_errors: false }, services_health: [] },
      '/api/errors': { errors: [], total: 0 },
      '/api/metrics/injection': { count: 0, series: [], ranking: [], range: '7d' },
      '/api/memories': emptyPage,
      '/api/memories/senders': { senders: [], source: { status: 'empty', reason_code: 'e2e_fixture_empty' } },
      '/api/import/sources': { sources: [] },
      '/api/maintenance/jobs': emptyPage,
      '/api/observatory/traces': emptyPage,
      '/api/config/channels': { current: { channels: {} }, defaults: { channels: {} }, descriptors: [], diff: [], editable_fields: [] },
      '/api/learning-center/sources': { items: [], total: 0 },
      '/api/learning-center/jobs': { items: [], total: 0 },
      '/api/learning-center/candidates': { items: [], total: 0 },
      '/api/learning-center/promotions': { items: [], total: 0 },
      '/api/learning-center/few-shot': { items: [], total: 0 },
      '/api/learning-center/experiences': { items: [], total: 0 },
      '/api/beliefs': { bot_id: '', groups: [], counts: {}, policy: { form_min_days: 2, stable_min_days: 4, decay_days: 30, max_active: 20 }, reflection: { enabled: false, every_n_days: null, last_run: null, recent_runs: [] }, capabilities: { veto: unavailable, restore: unavailable, create: unavailable, edit: unavailable, physical_delete: unavailable }, generated_at: 0 },
      '/api/jargon': { ...emptyPage, scope: { kind: 'RuntimeScope', payload: null }, capabilities: { review: unavailable, batch_review: unavailable, edit: unavailable, archive: unavailable, evidence: unavailable, create: unavailable, delete: unavailable, toggle_global: unavailable, select_all_matching: unavailable } },
      '/api/jargon/blocklist': { items: [], total: 0 },
      '/api/people': emptyPage,
      '/api/knowledge/facts': { ...emptyPage, scope: { bot_id: '', session_id: '', visibility: 'group' } },
      '/api/knowledge/few-shot': emptyPage,
      '/api/knowledge/book-lore/summary': { scope: { catalog_id: 'fixture', corpus_id: 'fixture', version: 'fixture' }, counts: { entities: 0, communities: 0, relations: 0, notes: 0 }, schema: { fingerprint: '', tables: {}, missing_tables: [] }, read_only: true },
      '/api/knowledge/book-lore/entities': { ...emptyPage, scope: { catalog_id: 'fixture', corpus_id: 'fixture', version: 'fixture' }, resource: 'entities', read_only: true },
      '/api/diagnostics/indexes': { health: 'empty', source: 'wave_memory_readonly_diagnostics', checked_at: '', evidence: { read_only: true, probe_count: 0, health_counts: {} }, checks: [] },
      '/api/tags/': { items: [{ id: 1, name: '共同记忆', type: 'topic', frequency: 12, confidence: 0.86 }], total: 1, available_types: ['person', 'topic'], readonly: true, capabilities: { mutation: { available: false, reason_code: 'mutation_disabled' } } },
      '/api/tags/quality': { total_tags: 1, total_memories: 10, tagged_memories: 8, untagged_memories: 2, extractable_untagged_memories: 1, skipped_short_untagged_memories: 1, orphan_memory_tag_refs: 0, coverage: 0.8, runtime: { capabilities: { extract: { available: true, reason_code: null }, mutation: { available: false, reason_code: 'mutation_disabled' } }, index: { available: true, health: 'ready', reason_code: null, count: 42, generation: 7, db_watermark: 123 }, rag: { mode: 'semantic', semantic_available: true, fallback_reason: null, provider_configured: true, reference_refresh_interval: 200 } } },
      '/api/compat/status': { status: 'not_detected', source: 'e2e_fixture', checked_at: '', error: null, evidence: [], probe: { status: 'not_detected', source: 'e2e_fixture', checked_at: '', error: null, evidence: [], plugins: [] }, facade: { id: 'facade', enabled: false, configured: false, status: 'not_configured', source: 'e2e_fixture', checked_at: '', error: null, evidence: [] }, tool_aliases: {}, capabilities: [], detected_plugins: [], duplicate_warnings: [], recommended_settings: [], documentation: { kind: 'static', facade_interfaces: [], tool_aliases: {}, notice: '' } },
      '/api/channels/config': { current: { channels: {} } },
    }
    Object.entries(responses).forEach(([pathname, payload]) => insert.run(pathname, JSON.stringify(payload)))

    await provide({
      databasePath,
      get(pathname) {
        const row = database.prepare('SELECT payload_json FROM api_responses WHERE pathname = ?').get(pathname) as { payload_json?: string } | undefined
        return row?.payload_json ? JSON.parse(row.payload_json) : {}
      },
    })

    database.close()
    await rm(directory, { recursive: true, force: true })
  },

  authenticatedPage: async ({ page, apiFixture }, provide) => {
    await page.addInitScript(() => localStorage.setItem('wavememory.webui.token', 'e2e-auth-token'))
    await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
      const pathname = new URL(route.request().url()).pathname
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(apiFixture.get(pathname)) })
    })
    await provide(page)
  },
})

export { expect }

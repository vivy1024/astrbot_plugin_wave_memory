import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { appRoutes } from '@/app/routes'
import { setViewport } from '@/test/setup'
import { IndexesPage } from '../../diagnostics/IndexesPage'
import { JargonPage } from '../../jargon/JargonPage'
import { PeoplePage } from '../../people/PeoplePage'
import { BookLorePage } from '../BookLorePage'
import { FactsPage } from '../FactsPage'
import { FewShotPage } from '../FewShotPage'

const mocks = vi.hoisted(() => ({
  getBookLoreSummary: vi.fn(),
  getBookLoreItems: vi.fn(),
  getApprovedFewShot: vi.fn(),
  getScopedFacts: vi.fn(),
  getPeople: vi.fn(),
  getRelationships: vi.fn(),
  getIndexDiagnostics: vi.fn(),
  listJargons: vi.fn(),
  getCatalogAudit: vi.fn(),
  getJargonEvidence: vi.fn(),
  batchReviewJargons: vi.fn(),
  updateJargonMeaning: vi.fn(),
  archiveJargon: vi.fn(),
  checkHolymanUpdate: vi.fn(),
  previewHolymanSync: vi.fn(),
  getScopeOptions: vi.fn(),
}))

vi.mock('@/api/knowledge', () => ({
  getBookLoreSummary: mocks.getBookLoreSummary,
  getBookLoreItems: mocks.getBookLoreItems,
  getApprovedFewShot: mocks.getApprovedFewShot,
  getScopedFacts: mocks.getScopedFacts,
}))
vi.mock('@/api/people', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/people')>()
  return {
    ...actual,
    getPeople: mocks.getPeople,
    getRelationships: mocks.getRelationships,
    getLegacyPeople: vi.fn(),
    getPersonTimeline: vi.fn(),
  }
})
vi.mock('@/api/jargon', () => ({ listJargons: mocks.listJargons, getCatalogAudit: mocks.getCatalogAudit, getJargonEvidence: mocks.getJargonEvidence, batchReviewJargons: mocks.batchReviewJargons, updateJargonMeaning: mocks.updateJargonMeaning, archiveJargon: mocks.archiveJargon, checkHolymanUpdate: mocks.checkHolymanUpdate, previewHolymanSync: mocks.previewHolymanSync, reviewJargon: vi.fn() }))
vi.mock('@/api/diagnostics', () => ({ getIndexDiagnostics: mocks.getIndexDiagnostics }))
vi.mock('@/api/options', () => ({
  getScopeOptions: mocks.getScopeOptions,
  scopeOptionsFor: (_payload: unknown, kinds: string[]) => kinds.includes('bot')
    ? [{ value: 'bot-real', label: '真实 Bot', kind: 'bot', description: 'Bot ID bot-real' }]
    : [{ value: 'qq:group:42', label: '群 42', kind: 'session', description: 'bot-real · group · runtime' }],
  groupSessionOptions: (options: Array<{ kind?: string }>) => options.filter((option) => option.kind === 'session'),
}))

const page = { total: 1, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false }

beforeEach(() => {
  setViewport(1280)
  mocks.getScopeOptions.mockResolvedValue({ bots: [], sessions: [], channels: [], generated_at: 1, source: { health: 'healthy', reason_code: null } })
  mocks.getRelationships.mockResolvedValue({ items: [], page: { ...page, total: 0 } })
  mocks.listJargons.mockResolvedValue({ items: [], page, capabilities: { review: { available: false, reason_code: 'readonly' } } })
  mocks.getCatalogAudit.mockResolvedValue({ asset_status: 'ready' })
  mocks.getJargonEvidence.mockResolvedValue({ ok: true, jargon: { id: 7, word: 'v我50', meaning: '疯狂星期四', revision: 2 }, scope: { kind: 'RuntimeScope', payload: {} }, anchor: null, messages: [], fallback_contexts: [], used_fallback: true })
  mocks.batchReviewJargons.mockResolvedValue({ ok: true, operation: { status: 'succeeded' }, reviewed_count: 1, items: [{ id: 7, status: 'rejected' }] })
  mocks.updateJargonMeaning.mockResolvedValue({ ok: true, operation: { status: 'succeeded' }, item: {} })
  mocks.archiveJargon.mockResolvedValue({ ok: true, operation: { status: 'succeeded' }, item: { id: 7, status: 'archived' } })
  mocks.checkHolymanUpdate.mockResolvedValue({ local_version: 'v1', remote_version: 'v2', has_update: true, asset_status: 'ready', checked_at: 'now', cached: false })
  mocks.previewHolymanSync.mockResolvedValue({ ok: true, will_update: true, asset_status: 'ready', local_version: 'v1', remote_version: 'v2', local_content_hash: 'a', remote_content_hash: 'b', local_counts: { phrases: 1 }, remote_counts: { phrases: 2 }, delta_counts: { phrases: 1 }, samples: { added_phrases: ['新口癖'] } })
})

describe('知识、人物与诊断页面关键约束', () => {
  it('Jargon 产品术语保持为黑话与口癖，不再改名为本地表达', () => {
    expect(appRoutes.find((route) => route.path === '/jargon')).toMatchObject({ title: '黑话与口癖' })
  })

  it('Jargon 仅展示当前正式 Scope 数据', async () => {
    render(<MemoryRouter initialEntries={['/jargon?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><JargonPage /></MemoryRouter>)

    expect(await screen.findByText('群聊黑话清单')).toBeVisible()
    expect(screen.queryByText(/只读审计/)).not.toBeInTheDocument()
    expect(screen.queryByText(/非当前 Scope/)).not.toBeInTheDocument()
    expect(mocks.listJargons).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }))
  })

  it('BookLore 使用服务端摘要返回的真实 catalog scope', async () => {
    mocks.getBookLoreSummary.mockResolvedValue({
      scope: { catalog_id: 'configured-catalog', corpus_id: 'canon-corpus', version: 'v7' },
      counts: { entities: 0, communities: 0, relations: 0, notes: 0 },
      schema: { fingerprint: 'x', tables: {}, missing_tables: [] },
      read_only: true,
    })
    mocks.getBookLoreItems.mockResolvedValue({ items: [], page: { ...page, total: 0 }, scope: { catalog_id: 'configured-catalog', corpus_id: 'canon-corpus', version: 'v7' }, resource: 'entities', read_only: true })

    render(<MemoryRouter><BookLorePage /></MemoryRouter>)

    expect(await screen.findByText(/configured-catalog/)).toBeVisible()
    await waitFor(() => expect(mocks.getBookLoreItems).toHaveBeenCalledWith('entities', expect.objectContaining({ catalog_id: 'configured-catalog', corpus_id: 'canon-corpus', version: 'v7' })))
  })

  it('FewShot 未选择真实 Bot 时不请求跨 Bot 列表', async () => {
    render(<MemoryRouter><FewShotPage /></MemoryRouter>)

    expect(await screen.findByText(/不会跨群汇总/)).toBeVisible()
    expect(mocks.getApprovedFewShot).not.toHaveBeenCalled()
  })

  it('Jargon 只在切到广域资产 Tab 后读取 Catalog', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/jargon?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><JargonPage /></MemoryRouter>)

    await waitFor(() => expect(mocks.listJargons).toHaveBeenCalledTimes(1))
    expect(mocks.getCatalogAudit).not.toHaveBeenCalled()
    await user.click(screen.getByRole('tab', { name: /内置广域资产/ }))
    await waitFor(() => expect(mocks.getCatalogAudit).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('button', { name: '同步预览' }))
    await waitFor(() => expect(mocks.previewHolymanSync).toHaveBeenCalledWith(true))
    expect(await screen.findByText('新口癖')).toBeVisible()
    expect(screen.getByRole('button', { name: '确认同步并写入' })).toBeDisabled()
  })

  it('Jargon 证据按钮使用 ObjectRef 与当前 Scope 还原聊天上下文', async () => {
    const user = userEvent.setup()
    mocks.listJargons.mockResolvedValue({
      items: [{
        id: 7,
        word: 'v我50',
        meaning: '疯狂星期四',
        frequency: 3,
        confidence: 0.9,
        status: 'pending',
        review_status: 'pending',
        bot_id: 'bot-real',
        session_id: 'qq:group:42',
        visibility: 'group',
        source: 'wave_memory',
        rule_version: 'v1',
        promotion: null,
        anchors: [{ type: 'memory', id: '11', source_scope: 'qq:group:42', availability: 'available', summary: '同作用域锚点', object_ref: null }],
        object_ref: { ref: 'opaque-jargon-ref', kind: 'jargon', locator: 7, scope_key: 'qq:group:42', scope_query: { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }, version: 2 },
        revision: 2,
      }],
      page,
      capabilities: { review: { available: true, reason_code: null } },
    })
    mocks.getJargonEvidence.mockResolvedValue({
      ok: true,
      jargon: { id: 7, word: 'v我50', meaning: '疯狂星期四', revision: 2 },
      scope: { kind: 'RuntimeScope', payload: {} },
      anchor: { id: 11, group_id: '42', sender_id: 'u1', sender_name: '用户甲', content: '今天疯狂星期四 v我50', timestamp: 1000, role: 'anchor' },
      messages: [
        { id: 10, group_id: '42', sender_id: 'u2', sender_name: '用户乙', content: '前文消息', timestamp: 990, role: 'before' },
        { id: 11, group_id: '42', sender_id: 'u1', sender_name: '用户甲', content: '今天疯狂星期四 v我50', timestamp: 1000, role: 'anchor' },
        { id: 12, group_id: '42', sender_id: 'u3', sender_name: '用户丙', content: '后文消息', timestamp: 1010, role: 'after' },
      ],
      fallback_contexts: [],
      used_fallback: false,
    })

    render(<MemoryRouter initialEntries={['/jargon?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><JargonPage /></MemoryRouter>)

    await user.click(await screen.findByRole('button', { name: '证据' }))
    await waitFor(() => expect(mocks.getJargonEvidence).toHaveBeenCalledWith(expect.objectContaining({ id: 7, object_ref: expect.objectContaining({ ref: 'opaque-jargon-ref' }) }), { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }, 15, 15))
    expect(await screen.findByText('前文消息')).toBeVisible()
    expect(screen.getByText('今天疯狂星期四 v我50')).toBeVisible()
    expect(screen.getByText('后文消息')).toBeVisible()
    expect(screen.getByText('同作用域动态上下文')).toBeVisible()
  })

  it('Jargon 恢复当前页选择、批量审核与释义编辑回待审', async () => {
    const user = userEvent.setup()
    const item = {
      id: 7, word: 'v我50', meaning: '旧释义', frequency: 3, confidence: 0.9, status: 'pending', review_status: 'pending',
      bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group', source: 'wave_memory', rule_version: 'v1', promotion: null,
      anchors: [{ type: 'memory', id: '11', source_scope: 'qq:group:42', availability: 'available', summary: '同作用域锚点', object_ref: null }],
      object_ref: { ref: 'opaque-jargon-ref', kind: 'jargon', locator: 7, scope_key: 'qq:group:42', scope_query: { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }, version: 2 }, revision: 2,
    }
    mocks.listJargons.mockResolvedValue({ items: [item], page, capabilities: {
      review: { available: true, reason_code: null }, batch_review: { available: true, reason_code: null }, edit: { available: true, reason_code: null }, archive: { available: true, reason_code: null }, evidence: { available: true, reason_code: null }, create: { available: false, reason_code: 'anchored_jargon_command_unavailable' }, delete: { available: false, reason_code: 'physical_delete_disabled' }, toggle_global: { available: false, reason_code: 'scoped_global_toggle_unsupported' }, select_all_matching: { available: false, reason_code: 'server_signed_object_refs_required' },
    } })

    render(<MemoryRouter initialEntries={['/jargon?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><JargonPage /></MemoryRouter>)

    await user.click(await screen.findByLabelText('选择黑话 v我50'))
    expect(screen.getByText('已选 1 条')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '批量拒绝并全局拉黑' }))
    await waitFor(() => expect(mocks.batchReviewJargons).toHaveBeenCalledWith([expect.objectContaining({ id: 7 })], 'reject', { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }))

    await user.click(screen.getByRole('button', { name: '编辑黑话 v我50' }))
    const meaning = screen.getByLabelText('黑话释义')
    await user.clear(meaning)
    await user.type(meaning, '新释义')
    await user.click(screen.getByRole('button', { name: '保存并回到待审核' }))
    await waitFor(() => expect(mocks.updateJargonMeaning).toHaveBeenCalledWith(expect.objectContaining({ id: 7 }), '新释义', { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }))
  })

  it('Facts 在桌面保留表格，窄屏改为包含完整关系和详情入口的卡片', async () => {
    mocks.getScopedFacts.mockResolvedValue({
      items: [{ id: 7, subject: '一段很长的主体名称', predicate: 'relates_to', object: '这是一段不能在移动端被截断的完整事实客体内容', confidence: 0.82, evidence_status: 'available', evidence: [], bot_id: 'bot-real', session_id: 'qq:group:42' }],
      page,
      scope: { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' },
    })

    const desktop = render(<MemoryRouter initialEntries={['/facts?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><FactsPage /></MemoryRouter>)
    expect(await screen.findByText('一段很长的主体名称')).toBeVisible()
    expect(desktop.container.querySelector('[data-responsive-table="table"] table')).toBeInTheDocument()
    desktop.unmount()

    setViewport(390)
    const mobile = render(<MemoryRouter initialEntries={['/facts?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><FactsPage /></MemoryRouter>)
    expect(await screen.findByText('这是一段不能在移动端被截断的完整事实客体内容')).toBeVisible()
    await waitFor(() => expect(mobile.container.querySelector('[data-responsive-table="cards"]')).toBeInTheDocument())
    expect(mobile.container.querySelector('[data-responsive-table="cards"] table')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看详情' })).toBeVisible()
  })

  it('FewShot 窄屏展示完整范例并保留详情入口', async () => {
    setViewport(390)
    mocks.getApprovedFewShot.mockResolvedValue({
      items: [{ id: 9, bot_id: 'bot-real', content: '移动端必须完整可读的范例正文，不依赖横向滚动或截断。', score: 0.9, traits: ['自然', '克制'], created_at: 1, approved_at: 1 }],
      page,
    })

    const view = render(<MemoryRouter initialEntries={['/few-shot?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><FewShotPage /></MemoryRouter>)
    expect(await screen.findByText('移动端必须完整可读的范例正文，不依赖横向滚动或截断。')).toBeVisible()
    await waitFor(() => expect(view.container.querySelector('[data-responsive-table="cards"]')).toBeInTheDocument())
    expect(view.container.querySelector('[data-responsive-table="cards"] table')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看详情' })).toBeVisible()
  })

  it('Facts 的筛选草稿在提交前不触发请求，提交时一次写入条件', async () => {
    const user = userEvent.setup()
    mocks.getScopedFacts.mockResolvedValue({ items: [], page, scope: { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' } })

    render(<MemoryRouter initialEntries={['/facts?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><FactsPage /></MemoryRouter>)

    await waitFor(() => expect(mocks.getScopedFacts).toHaveBeenCalledTimes(1))
    await user.selectOptions(screen.getByLabelText('事实状态'), 'pending')
    expect(mocks.getScopedFacts).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '搜索' }))
    await waitFor(() => expect(mocks.getScopedFacts).toHaveBeenCalledTimes(2))
    expect(mocks.getScopedFacts).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'pending', offset: 0 }))
  })

  it('People 对缺失 Affinity 明示不可用且不回填 50', async () => {
    mocks.getPeople.mockResolvedValue({
      items: [{
        user_id: 'user-1', group_id: '42', bot_id: 'bot-real', nickname: '小羽', display_name: '小羽', aliases: ['羽毛'], interaction_count: 8,
        scope: { user_id: 'user-1', group_id: '42', bot_id: 'bot-real' }, scope_key: 'user-1|42|bot-real', metadata: {}, registry_metadata: {}, person_registry: {},
        affinity: null, affinity_status: 'unavailable', affinity_reason_code: 'scoped_affinity_projection_unavailable',
      }],
      page,
    })

    render(<MemoryRouter initialEntries={['/people?bot_id=bot-real&session_id=qq%3Agroup%3A42']}><PeoplePage /></MemoryRouter>)

    expect(await screen.findByText('小羽')).toBeVisible()
    expect(screen.getAllByText('不可用').length).toBeGreaterThan(0)
    expect(screen.queryByText('50')).not.toBeInTheDocument()
  })

  it('Indexes 解释 drift 并只提供 Maintenance 安全入口', async () => {
    const user = userEvent.setup()
    mocks.getIndexDiagnostics.mockResolvedValue({
      health: 'drift', source: 'wave_memory_readonly_diagnostics', checked_at: '2026-01-01T00:00:00Z',
      evidence: { read_only: true, probe_count: 1, health_counts: { drift: 1 } },
      checks: [{ name: 'fts', health: 'drift', source: 'sqlite:C:\\private\\memory.db', checked_at: '2026-01-01T00:00:00Z', evidence: { missing_rows: 2, path: 'C:\\private\\memory.db' } }],
    })

    render(<MemoryRouter><IndexesPage /></MemoryRouter>)

    await user.click(await screen.findByText('查看影响与安全边界'))
    expect(await screen.findByText(/关键词或语义召回漏项/)).toBeVisible()
    expect(screen.getByRole('link', { name: '进入 Maintenance 修复任务' })).toHaveAttribute('href', '/maintenance?source=diagnostics&panel=indexes')
    expect(screen.queryByRole('button', { name: /rebuild|重建/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/C:\\private/)).not.toBeInTheDocument()
  })
})

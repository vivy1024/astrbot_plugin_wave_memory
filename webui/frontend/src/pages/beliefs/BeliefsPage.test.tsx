import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setViewport } from '@/test/setup'
import { BeliefsPage } from './BeliefsPage'

const mocks = vi.hoisted(() => ({
  listBeliefs: vi.fn(),
  getBeliefEvidence: vi.fn(),
  batchTransitionBeliefs: vi.fn(),
  approveBelief: vi.fn(),
  archiveBelief: vi.fn(),
  getScopeOptions: vi.fn(),
  fetchJson: vi.fn(),
}))

vi.mock('@/api/beliefs', () => ({
  listBeliefs: mocks.listBeliefs,
  getBeliefEvidence: mocks.getBeliefEvidence,
  batchTransitionBeliefs: mocks.batchTransitionBeliefs,
  approveBelief: mocks.approveBelief,
  archiveBelief: mocks.archiveBelief,
}))
vi.mock('@/api/client', () => ({ fetchJson: mocks.fetchJson }))
vi.mock('@/api/options', () => ({
  getScopeOptions: mocks.getScopeOptions,
  scopeOptionsFor: (_payload: unknown, kinds: string[]) => kinds.includes('bot')
    ? [{ value: 'bot-real', label: '真实 Bot', kind: 'bot', description: 'Bot ID bot-real' }]
    : [{ value: 'qq:group:42', label: '群 42', kind: 'session', description: 'bot-real · group · runtime' }],
}))

const page = { total: 1, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false }
const available = { available: true, reason_code: null }
const disabled = { available: false, reason_code: 'command_unavailable' }
const belief = {
  id: 21,
  belief_key: 'care-for-pets',
  content: '要温柔对待宠物',
  type: 'world_view',
  status: 'pending',
  confidence: 0.82,
  confidence_components: { evidence: 0.9, consistency: 0.8 },
  confidence_policy_version: 'belief-v2',
  confidence_evidence: { support_windows: 2 },
  gating: { decision: 'direct', reason_code: 'relationship_direct', trust: 80, hostility: 10, subjects: ['qq:user:u1'], policy_version: 'relationship-gating-v1', review_required: false, unknown_subjects: [], relationship_revisions: { 'qq:user:u1': 1 } },
  anchor_sentence: '它们也会害怕',
  evidence_health: 'available',
  quarantine_reason: null,
  bot_id: 'bot-real',
  session_id: 'qq:group:42',
  visibility: 'group',
  evidence: [{ type: 'memory', id: '31', source_scope: 'qq:group:42', availability: 'available', summary: '来源消息', object_ref: null }],
  object_ref: { ref: 'opaque-belief-21', kind: 'belief', locator: 21, scope_key: 'qq:group:42', version: 2 },
  revision: 2,
  actions: {
    approve: available,
    archive: available,
    restore: disabled,
    delete: disabled,
  },
  updated_at: 1000,
} as const

beforeEach(() => {
  setViewport(1280)
  vi.clearAllMocks()
  mocks.getScopeOptions.mockResolvedValue({ bots: [], sessions: [], channels: [], generated_at: 1, source: { health: 'healthy', reason_code: null } })
  mocks.listBeliefs.mockResolvedValue({
    items: [belief],
    page,
    scope: { kind: 'RuntimeScope', payload: {} },
    capabilities: {
      lifecycle: { ...available, actions: ['approve', 'archive'] },
      batch_lifecycle: { ...available, actions: ['approve', 'archive'] },
      evidence: available,
      create: { available: false, reason_code: 'anchored_belief_command_unavailable' },
      edit: { available: false, reason_code: 'belief_edit_command_unavailable' },
      physical_delete: { available: false, reason_code: 'physical_delete_disabled' },
      select_all_matching: { available: false, reason_code: 'object_ref_batch_required' },
    },
  })
  mocks.getBeliefEvidence.mockResolvedValue({
    ok: true,
    belief: { id: 21, content: belief.content, type: belief.type, revision: 2 },
    scope: { kind: 'RuntimeScope', payload: {} },
    anchor: { id: 31, group_id: '42', sender_id: 'u1', sender_name: '用户甲', content: '它们也会害怕', timestamp: 1000, role: 'anchor' },
    messages: [
      { id: 30, group_id: '42', sender_id: 'u2', sender_name: '用户乙', content: '前文消息', timestamp: 990, role: 'before' },
      { id: 31, group_id: '42', sender_id: 'u1', sender_name: '用户甲', content: '它们也会害怕', timestamp: 1000, role: 'anchor' },
    ],
    memories: [],
    relationship_events: [],
    episodes: [],
    used_fallback: false,
    reason_code: null,
  })
  mocks.batchTransitionBeliefs.mockResolvedValue({ ok: true, operation: { kind: 'belief.batch.approve', status: 'succeeded' }, transitioned_count: 1, items: [{ id: 21, status: 'active' }] })
  mocks.approveBelief.mockResolvedValue({ ok: true, operation: { status: 'succeeded' } })
  mocks.archiveBelief.mockResolvedValue({ ok: true, operation: { status: 'succeeded' } })
})

function renderPage() {
  return render(<MemoryRouter initialEntries={['/beliefs?bot_id=bot-real&session_id=qq%3Agroup%3A42&visibility=group']}><BeliefsPage /></MemoryRouter>)
}

describe('BeliefsPage scoped 审核恢复', () => {
  it('使用当前 Scope 与 ObjectRef 打开聊天证据，保留不可用的多阶层级', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '证据' }))

    await waitFor(() => expect(mocks.getBeliefEvidence).toHaveBeenCalledWith(expect.objectContaining({ id: 21, object_ref: expect.objectContaining({ ref: 'opaque-belief-21' }) }), { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }, 15, 15))
    expect(await screen.findByText('前文消息')).toBeVisible()
    expect(screen.getByText('信念来源锚点')).toBeVisible()
    expect(screen.getByRole('tab', { name: '关系变化' })).toBeDisabled()
    expect(screen.getByRole('tab', { name: '自省独白' })).toBeDisabled()
  })

  it('详情展示关系门禁与 evidence-v1 保护说明', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '查看信念 21 详情' }))

    expect(await screen.findByText('关系门禁')).toBeVisible()
    expect(screen.getByText('关系门禁：自动放行')).toBeVisible()
    expect(screen.getByText(/高信任仅表示可以跳过人工复核/)).toBeVisible()
  })

  it('当前页选择后批量通过会提交逐项 ObjectRef，不启用跨页猜测', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('checkbox', { name: '选择当前页全部信念' }))
    expect(screen.getByText(/跨页全部匹配暂不可用/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: '批量确认通过' }))

    await waitFor(() => expect(mocks.batchTransitionBeliefs).toHaveBeenCalledWith([expect.objectContaining({ id: 21, revision: 2, object_ref: expect.objectContaining({ ref: 'opaque-belief-21' }) })], 'approve', { bot_id: 'bot-real', session_id: 'qq:group:42', visibility: 'group' }))
  })

  it('新建与物理删除保持可见禁用', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('button', { name: '新增信念' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '通过信念 21' })).toBeVisible()
    expect(screen.getByRole('button', { name: '归档信念 21' })).toBeVisible()

    await user.click(screen.getByRole('checkbox', { name: '选择信念 21' }))
    expect(screen.getByRole('button', { name: '批量删除' })).toBeDisabled()
  })

  it('窄屏降级为信念卡片，不保留桌面表格', async () => {
    setViewport(375)
    renderPage()

    expect(await screen.findByText('要温柔对待宠物')).toBeVisible()
    expect(document.querySelector('article[data-slot="belief-card"]')).toBeInTheDocument()
    expect(document.querySelector('table')).not.toBeInTheDocument()
  })
})

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getRelationshipPortrait,
  getRelationshipPortraitHistory,
  keepRelationshipPortrait,
  regenerateRelationshipPortrait,
  saveRelationshipPortrait,
  type RelationshipPortraitView,
} from '@/api/people'
import { RelationshipPortraitPanel } from './RelationshipPortraitPanel'

vi.mock('@/api/people', () => ({
  getRelationshipPortrait: vi.fn(),
  getRelationshipPortraitHistory: vi.fn(),
  saveRelationshipPortrait: vi.fn(),
  regenerateRelationshipPortrait: vi.fn(),
  keepRelationshipPortrait: vi.fn(),
}))

const getMock = vi.mocked(getRelationshipPortrait)
const historyMock = vi.mocked(getRelationshipPortraitHistory)
const saveMock = vi.mocked(saveRelationshipPortrait)
const regenerateMock = vi.mocked(regenerateRelationshipPortrait)
const keepMock = vi.mocked(keepRelationshipPortrait)

const query = { bot_id: 'yushu', session_id: 'qq:group:900000001', visibility: 'group' as const }

function view(overrides: Partial<RelationshipPortraitView> = {}, item: Partial<NonNullable<RelationshipPortraitView['item']>> | null = {}): RelationshipPortraitView {
  return {
    user_id: '10000001',
    user_ids: ['10000001'],
    owner_user_id: '10000001',
    version: item === null ? 0 : (item.version ?? 2),
    item: item === null ? null : {
      bot_id: 'yushu',
      user_id: '10000001',
      content: '阿木是群里的攻略达人，你们从接冷笑话开始熟起来。',
      version: 2,
      status: 'auto',
      source_max_event_id: 128,
      source_event_count: 45,
      provider: 'fake-provider',
      edited_by: '',
      generated_at: 1_790_000_000,
      reviewed_through_event_id: 128,
      reviewed_at: 1_790_000_000,
      pending_update: false,
      pending_since: null,
      last_error: '',
      last_error_at: null,
      last_attempt_at: 1_790_000_000,
      created_at: 1_789_000_000,
      updated_at: 1_790_000_000,
      ...item,
    },
    last_error: null,
    last_error_at: null,
    history_count: 1,
    timeline: { non_private_total: 60, max_event_id: 131, new_since_portrait: 3, new_relational_since_portrait: 2 },
    settings: { auto_enabled: true, injection_enabled: true, service_available: true, max_chars: 300, min_events: 3 },
    ...overrides,
  }
}

function mutation(next: RelationshipPortraitView) {
  return { ok: true, operation: { kind: 'people.portrait', status: 'succeeded' }, revision: next.version, item: next }
}

describe('RelationshipPortraitPanel 关系小传', () => {
  beforeEach(() => {
    getMock.mockResolvedValue(view())
  })

  it('展示正文、状态、所用模型、依据范围与之后新增的记录数', async () => {
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    expect(await screen.findByText('阿木是群里的攻略达人，你们从接冷笑话开始熟起来。')).toBeInTheDocument()
    expect(screen.getByText('模型生成')).toBeInTheDocument()
    expect(screen.getByText('模型 fake-provider')).toBeInTheDocument()
    expect(screen.getByText('依据截至事件#128（共 45 条）')).toBeInTheDocument()
    expect(screen.getByText('第 2 版')).toBeInTheDocument()
    expect(screen.getByText('之后新增 3 条公开记录')).toBeInTheDocument()
    expect(screen.queryByText('有新记录待更新')).not.toBeInTheDocument()
    expect(screen.queryByText(/自动生成与注入还没开启/)).not.toBeInTheDocument()
    expect(getMock).toHaveBeenCalledWith({ ...query, user_id: '10000001' }, expect.anything())
  })

  it('编辑后带版本号保存，成功后显示新正文与「管理员改过」', async () => {
    const user = userEvent.setup()
    saveMock.mockResolvedValue(mutation(view({ version: 3 }, { version: 3, status: 'edited', edited_by: 'webui', content: '阿木是老朋友。' })))
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    await user.click(await screen.findByRole('button', { name: '编辑' }))
    const box = screen.getByLabelText('编辑关系小传')
    await user.clear(box)
    await user.type(box, '阿木是老朋友。')
    expect(screen.getByText('7 / 300 字')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledWith(query, { user_id: '10000001', version: 2, content: '阿木是老朋友。' }))
    expect(await screen.findByText('阿木是老朋友。')).toBeInTheDocument()
    expect(screen.getByText('管理员改过')).toBeInTheDocument()
    expect(screen.getByText('编辑者 webui')).toBeInTheDocument()
  })

  it('版本冲突时提示并重新读取最新版本', async () => {
    const user = userEvent.setup()
    const conflict = Object.assign(new Error('portrait_version_conflict（期望版本 2，当前 3）'), {
      status: 409,
      payload: { error: { code: 'portrait_version_conflict', message: 'x', retryable: false } },
    })
    saveMock.mockRejectedValue(conflict)
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    await user.click(await screen.findByRole('button', { name: '编辑' }))
    await user.type(screen.getByLabelText('编辑关系小传'), '补一句')
    getMock.mockResolvedValue(view({ version: 3 }, { version: 3, content: '别处刚保存的一版。' }))
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('小传刚被修改过')
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2))
  })

  it('超出字数上限时不提交', async () => {
    const user = userEvent.setup()
    getMock.mockResolvedValue(view({ settings: { auto_enabled: true, injection_enabled: true, service_available: true, max_chars: 5, min_events: 3 } }))
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    await user.click(await screen.findByRole('button', { name: '编辑' }))
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('小传最多 5 字')
    expect(saveMock).not.toHaveBeenCalled()
  })

  it('改过的小传有新记录：可保留当前或确认后重新生成，并查看历史版本', async () => {
    const user = userEvent.setup()
    getMock.mockResolvedValue(view({}, { status: 'edited', edited_by: 'webui', pending_update: true }))
    keepMock.mockResolvedValue(mutation(view({}, { status: 'edited', edited_by: 'webui', pending_update: false })))
    regenerateMock.mockResolvedValue(mutation(view({ version: 3, history_count: 2 }, { version: 3, content: '重新写的一版。' })))
    historyMock.mockResolvedValue({
      owner_user_id: '10000001',
      current_version: 3,
      items: [{
        id: 1, bot_id: 'yushu', user_id: '10000001', version: 2, content: '管理员改过的旧版。', status: 'edited',
        source_max_event_id: 100, source_event_count: 40, provider: '', edited_by: 'webui',
        generated_at: 1_789_500_000, replaced_at: 1_790_000_000, replaced_by: 'model',
      }],
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    expect(await screen.findByText('有新记录待更新')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '保留当前' }))
    await waitFor(() => expect(keepMock).toHaveBeenCalledWith(query, { user_id: '10000001', version: 2 }))
    await waitFor(() => expect(screen.queryByText('有新记录待更新')).not.toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: '重新生成' }))
    expect(confirm).toHaveBeenCalled()
    await waitFor(() => expect(regenerateMock).toHaveBeenCalledWith(query, { user_id: '10000001', version: 2 }))
    expect(await screen.findByText('重新写的一版。')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '历史版本（2）' }))
    expect(await screen.findByText('管理员改过的旧版。')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: '关系小传历史版本' })).toHaveTextContent('第 2 版')
    confirm.mockRestore()
  })

  it('没有小传时展示空态；功能未开启与没有模型时给出说明', async () => {
    getMock.mockResolvedValue(view({
      last_error: 'identity_contamination',
      last_error_at: 1_790_000_000,
      history_count: 0,
      timeline: { non_private_total: 2, max_event_id: 9, new_since_portrait: 2, new_relational_since_portrait: 2 },
      settings: { auto_enabled: false, injection_enabled: false, service_available: false, max_chars: 300, min_events: 3 },
    }, null))
    render(<RelationshipPortraitPanel query={query} userId="10000001" />)
    expect(await screen.findByText(/还没有关系小传。公开场合的记录 2 条/)).toBeInTheDocument()
    expect(screen.getByText(/自动生成与注入还没开启/)).toBeInTheDocument()
    expect(screen.getByText(/最近一次生成失败：写出的内容疑似身份污染/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '生成' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '手写一版' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /历史版本/ })).not.toBeInTheDocument()
  })
})

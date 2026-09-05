import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listMemories, type MemoryItem, type MemoriesResponse } from '@/api/memories'
import { calibrateRelationship } from '@/api/people'
import { RelationshipCalibrationPanel } from './RelationshipCalibrationPanel'

vi.mock('@/api/memories', () => ({
  listMemories: vi.fn(),
}))

vi.mock('@/api/people', () => ({
  calibrateRelationship: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}))

const listMemoriesMock = vi.mocked(listMemories)
const calibrateRelationshipMock = vi.mocked(calibrateRelationship)

const baseQuery = {
  bot_id: 'bot-1',
  session_id: 'qq:group:123',
  visibility: 'group' as const,
  user_id: 'user-42',
}

const baseItem = {
  subject_principal_id: 'qq:user:user-42',
  revision: 3,
  values: {},
  object_ref: { ref: 'relationship-ref', kind: 'relationship' as const },
  calibration: { available: true, reason_code: null },
}

function memory(id: number, content = `记忆 ${id}`): MemoryItem {
  return {
    id,
    content,
    sender_id: 'user-42',
    sender_name: '测试用户',
    group_id: '123',
    bot_id: 'bot-1',
    session_id: 'qq:group:123',
    visibility: 'group',
    timestamp: id,
    has_vector: true,
    version: 1,
    ref: `memory-ref-${id}`,
    detail_url: `/api/memories/${id}`,
    mutation_url: `/api/memories/${id}`,
  }
}

function response(items: MemoryItem[], options: { total?: number; offset?: number; hasMore?: boolean } = {}): MemoriesResponse {
  const total = options.total ?? items.length
  const offset = options.offset ?? 0
  const limit = 100 as const
  return {
    items,
    page: {
      total,
      total_status: 'exact',
      reason_code: null,
      limit,
      offset,
      page: Math.floor(offset / limit) + 1,
      page_count: Math.ceil(total / limit),
      has_more: options.hasMore ?? false,
    },
  }
}

describe('RelationshipCalibrationPanel 证据选择', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    calibrateRelationshipMock.mockResolvedValue({ ok: true, operation: { kind: 'adjust', status: 'committed' }, revision: 4 })
  })

  it('按 page.has_more 请求下一页并保留 page.total 计数', async () => {
    listMemoriesMock
      .mockResolvedValueOnce(response([memory(1)], { total: 101, hasMore: true }))
      .mockResolvedValueOnce(response([memory(101)], { total: 101, offset: 100 }))

    render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledWith(expect.objectContaining({
      sender_id: 'user-42',
      limit: 100,
      offset: 0,
      search: undefined,
    })))
    expect(screen.getByText('已加载 1 / 101 条')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '加载更多' }))

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledWith(expect.objectContaining({
      sender_id: 'user-42',
      limit: 100,
      offset: 100,
    })))
    expect(screen.getByRole('option', { name: /101 · 记忆 101/ })).toBeInTheDocument()
    expect(screen.getByText('已加载 2 / 101 条')).toBeInTheDocument()
  })

  it('把筛选词传给服务端并替换当前证据页，而不是前端截断', async () => {
    listMemoriesMock.mockImplementation(async (filters) => filters.search === 'older'
      ? response([memory(88, 'older server result')], { total: 1 })
      : response([memory(1)], { total: 120, hasMore: true }))

    render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledWith(expect.objectContaining({ offset: 0, limit: 100 })))
    fireEvent.change(screen.getByLabelText('筛选当前群友记忆'), { target: { value: 'older' } })

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledWith(expect.objectContaining({
      search: 'older',
      offset: 0,
      limit: 100,
    })))
    expect(screen.getByRole('option', { name: /88 · older server result/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /1 · 记忆 1/ })).not.toBeInTheDocument()
  })

  it('切换群作用域时清空旧选择并重新加载证据', async () => {
    listMemoriesMock.mockImplementation(async (filters) => filters.session_id === 'qq:group:456'
      ? response([memory(2, '新群记忆')])
      : response([memory(1, '旧群记忆')]))

    const { rerender } = render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)
    const evidenceSelect = screen.getByLabelText('记忆证据')

    await waitFor(() => expect(screen.getByRole('option', { name: /1 · 旧群记忆/ })).toBeInTheDocument())
    fireEvent.change(evidenceSelect, { target: { value: '1' } })
    expect(evidenceSelect).toHaveValue('1')

    const nextQuery = { ...baseQuery, session_id: 'qq:group:456' }
    rerender(<RelationshipCalibrationPanel item={baseItem} query={nextQuery} />)

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledWith(expect.objectContaining({
      session_id: 'qq:group:456',
      offset: 0,
    })))
    await waitFor(() => expect(screen.getByRole('option', { name: /2 · 新群记忆/ })).toBeInTheDocument())
    // 方案 B：切换作用域后会自动预选新作用域最新一条，绝不沿用旧选择
    await waitFor(() => expect(evidenceSelect).toHaveValue('2'))
    expect(evidenceSelect).not.toHaveValue('1')
  })

  it('首次加载完成后自动预选最新一条记忆作为证据', async () => {
    listMemoriesMock.mockResolvedValue(response([memory(5, '最近记忆'), memory(4, '更早记忆')]))

    render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)

    await waitFor(() => expect(screen.getByLabelText('记忆证据')).toHaveValue('5'))
  })

  it('提交时把选中的真实记忆组装成服务端可校验的证据描述符', async () => {
    listMemoriesMock.mockResolvedValue(response([memory(17, '支持校准的真实消息')]))

    render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)
    await waitFor(() => expect(screen.getByRole('option', { name: /17 · 支持校准的真实消息/ })).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('数值'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('理由'), { target: { value: '这条消息体现了明显的信任变化' } })
    fireEvent.change(screen.getByLabelText('记忆证据'), { target: { value: '17' } })
    fireEvent.change(screen.getByLabelText('补充证据说明（可选）'), { target: { value: '  这句话对应信任提升的关键节点  ' } })
    fireEvent.click(screen.getByRole('button', { name: '提交人工校准' }))

    await waitFor(() => expect(calibrateRelationshipMock).toHaveBeenCalledTimes(1))
    const [, payload] = calibrateRelationshipMock.mock.calls[0]
    expect(payload.evidence).toEqual([{
      kind: 'memory',
      id: '17',
      summary: '这句话对应信任提升的关键节点',
      source_scope: {
        bot_id: 'bot-1',
        visibility: 'group',
        session: {
          id: 'qq:group:123',
          platform_id: 'qq',
          kind: 'group',
          conversation_id: '123',
        },
        subject_principal_id: 'qq:user:user-42',
      },
    }])
  })

  it('证据搜索防抖，仅按最终关键词发一次请求', async () => {
    listMemoriesMock.mockImplementation(async (filters) => filters.search === '最终'
      ? response([memory(9, '最终记忆')])
      : response([memory(1)], { total: 1 }))

    render(<RelationshipCalibrationPanel item={baseItem} query={baseQuery} />)
    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledTimes(1))
    expect(listMemoriesMock).toHaveBeenLastCalledWith(expect.objectContaining({ search: undefined }))

    fireEvent.change(screen.getByLabelText('筛选当前群友记忆'), { target: { value: '中' } })
    fireEvent.change(screen.getByLabelText('筛选当前群友记忆'), { target: { value: '最终' } })

    await waitFor(() => expect(listMemoriesMock).toHaveBeenCalledTimes(2))
    expect(listMemoriesMock).toHaveBeenLastCalledWith(expect.objectContaining({ search: '最终' }))
    expect(screen.getByRole('option', { name: /9 · 最终记忆/ })).toBeInTheDocument()
  })
})

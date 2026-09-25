import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { TagCoveragePanel } from '@/components/tag/TagCoveragePanel'

const api = vi.hoisted(() => ({ getTagCoverage: vi.fn(), requeueTagCategory: vi.fn(), wakeTagWorker: vi.fn() }))
vi.mock('@/api/tags', () => api)
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const counts = { tagged: 288000, pending: 800, lost: 8850, failed: 7, skipped: 4370, too_short: 4362, not_eligible: 4530 }
const labels = { tagged: '已有标签', too_short: '太短（按规则不提取）', skipped: '模型判定无标签', failed: '提取失败', lost: '标签丢失（需重新提取）', pending: '排队中', not_eligible: '不在提取范围（私聊/旧行）' }

beforeEach(() => {
  vi.clearAllMocks()
  api.getTagCoverage.mockResolvedValue({
    active_memories: Object.values(counts).reduce((a, b) => a + b, 0),
    counts, labels,
    coverage: 0.9395, effective_coverage: 0.9683, backlog: 9650, failed_exhausted: 7,
    by_bot: { yushu: counts },
    samples: { lost: [{ id: 650001, preview: '昨天张羽师兄来过', bot_id: 'yushu', visibility: 'group' }] },
    throughput: { last_24h: 201, last_7d: 51000 },
    worker: { running: true, batch_size: 100, interval_seconds: 300, last_cycle_at: Date.now() / 1000 - 30, last_cycle_count: 12 },
    worker_capacity_per_hour: 1200, backlog_eta_hours: 8, requeueable: ['lost', 'skipped', 'failed'],
    min_content_length: 10, elapsed_ms: 1200, generated_at: Date.now() / 1000,
  })
  api.requeueTagCategory.mockResolvedValue({ ok: true, category: 'lost', selected: 5000, requeued: 5000 })
  api.wakeTagWorker.mockResolvedValue({ ok: true })
})

describe('TagCoveragePanel', () => {
  it('分开显示应打已打与整体覆盖，展开样例', async () => {
    const user = userEvent.setup()
    render(<TagCoveragePanel />)
    expect((await screen.findAllByText('96.8%'))[0]).toBeVisible()
    expect(screen.getByText('94%')).toBeVisible()
    expect(screen.getByText(/约 8 小时/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: /标签丢失/ }))
    expect(screen.getByText('昨天张羽师兄来过')).toBeVisible()
  })

  it('重新提取需要确认，完成后强制刷新', async () => {
    const user = userEvent.setup()
    render(<TagCoveragePanel />)
    await user.click(await screen.findByRole('button', { name: '重新提取' }))
    expect(api.requeueTagCategory).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() => expect(api.requeueTagCategory).toHaveBeenCalledWith('lost'))
    await waitFor(() => expect(api.getTagCoverage).toHaveBeenLastCalledWith(expect.objectContaining({ refresh: true })))
  })

  it('可以叫醒 TagWorker', async () => {
    const user = userEvent.setup()
    render(<TagCoveragePanel />)
    await user.click(await screen.findByRole('button', { name: /立即处理一批/ }))
    await waitFor(() => expect(api.wakeTagWorker).toHaveBeenCalled())
  })
})

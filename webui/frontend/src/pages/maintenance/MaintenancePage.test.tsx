import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MaintenancePage } from './MaintenancePage'

const api = vi.hoisted(() => ({ list: vi.fn(), job: vi.fn(), logs: vi.fn(), checkpoint: vi.fn(), cancel: vi.fn(), outbox: vi.fn() }))
vi.mock('@/api/maintenance', () => ({
  listMaintenanceJobs: api.list,
  getMaintenanceJob: api.job,
  getMaintenanceLogs: api.logs,
  getMaintenanceCheckpoint: api.checkpoint,
  cancelMaintenanceJob: api.cancel,
  getOutboxStatus: api.outbox,
}))
vi.mock('@/pages/maintain/MaintainPage', () => ({ MaintainPage: () => <div>维护工作台</div> }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), info: vi.fn(), success: vi.fn() } }))

const jobA = { run_id: 'job-a', request_id: 'req-a', status: 'succeeded', kind: 'maintenance.import.run', error_message: '旧任务错误详情' }
const jobB = { run_id: 'job-b', request_id: 'req-b', status: 'failed', kind: 'maintenance.tag_audit.run' }

function jobPage(items: Array<typeof jobA | typeof jobB>) {
  return {
    items,
    page: { total: items.length, total_status: 'exact', reason_code: null, limit: 25, offset: 0, page: 1, page_count: 1, has_more: false },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

beforeEach(() => {
  api.outbox.mockResolvedValue({
    status: 'healthy',
    outbox_items: 0,
    outbox_deliveries: 0,
    scope_recovery_items: 0,
    job_requests: 0,
    write_gateway_wired: true,
  })
  api.list.mockResolvedValue(jobPage([jobA, jobB]))
  api.job.mockImplementation((id: string) => id === 'job-a' ? Promise.resolve({ item: jobA }) : Promise.reject(new Error('detail denied')))
  api.logs.mockResolvedValue({ items: [] })
  api.checkpoint.mockResolvedValue({ job_id: 'job-a', status: 'succeeded', checkpoint: { phase: 'done' } })
})

describe('MaintenancePage job 深链切换', () => {
  it('选择新 job 立即清空旧详情，失败在页面内可重试/清除', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/maintenance?tab=jobs&job_id=job-a']}><MaintenancePage /></MemoryRouter>)

    expect(await screen.findByText('旧任务错误详情')).toBeVisible()
    const buttons = await screen.findAllByRole('button', { name: '查看进度与日志' })
    await user.click(buttons[1])
    expect(screen.queryByText('旧任务错误详情')).not.toBeInTheDocument()

    expect(await screen.findByText('任务详情读取失败')).toBeVisible()
    expect(screen.getByText('detail denied')).toBeVisible()
    expect(screen.getByRole('button', { name: '重试' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: '清除 URL 中的任务引用' }))
    await waitFor(() => expect(screen.queryByText('任务详情读取失败')).not.toBeInTheDocument())
  })

  it('日志分区失败时仍展示核心任务与 checkpoint', async () => {
    api.logs.mockRejectedValue(new Error('logs offline'))

    render(<MemoryRouter initialEntries={['/maintenance?tab=jobs&job_id=job-a']}><MaintenancePage /></MemoryRouter>)

    expect(await screen.findByText('旧任务错误详情')).toBeVisible()
    expect(await screen.findByText(/任务日志读取失败：logs offline/)).toBeVisible()
    expect(screen.getByText('done')).toBeVisible()
    expect(screen.queryByText('任务详情读取失败')).not.toBeInTheDocument()
  })

  it('刷新任务列表时旧响应不会覆盖新结果', async () => {
    const first = deferred<ReturnType<typeof jobPage>>()
    const second = deferred<ReturnType<typeof jobPage>>()
    api.list.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const user = userEvent.setup()

    render(<MemoryRouter initialEntries={['/maintenance?tab=jobs']}><MaintenancePage /></MemoryRouter>)
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('button', { name: '刷新任务历史' }))
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))

    second.resolve(jobPage([jobB]))
    expect(await screen.findByText('标签质量审计')).toBeVisible()
    first.resolve(jobPage([jobA]))
    await waitFor(() => expect(screen.queryByText('外部记忆导入')).not.toBeInTheDocument())
  })
})

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ServicesPage } from '@/pages/services/ServicesPage'

const api = vi.hoisted(() => ({ getServices: vi.fn(), controlService: vi.fn(), setToolEnabled: vi.fn(), reloadExtensions: vi.fn() }))
vi.mock('@/api/services', () => api)
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

beforeEach(() => {
  vi.clearAllMocks()
  api.getServices.mockResolvedValue({
    services_available: true,
    services: [
      { name: 'dream', title: '做梦', description: '回放记忆', created: true, running: true, stoppable: true, generation: 0, tasks: [], history: [] },
      { name: 'eviction', title: '记忆淘汰', description: '', created: false, running: null, stoppable: true, generation: 0, tasks: [], history: [] },
    ],
    tools: [{ name: 'wave_memory_search', description: '搜索', group: 'memory', writes: false, enabled: true, runtime_exposed: true, stats: { calls: 3, errors: 0, last_latency_ms: 1 } }],
    tool_registry: { registered: 1, built: 1, build_errors: {}, extension_errors: { 'bad.py': 'RuntimeError: x' }, extensions: { 'weather.py': ['weather_tool'] } },
    channels: ['safety', 'memory'],
  })
  api.controlService.mockResolvedValue({ ok: true })
  api.setToolEnabled.mockResolvedValue({ ok: true })
  api.reloadExtensions.mockResolvedValue({ ok: true, loaded: ['weather.py'], errors: {}, tools: { removed: ['weather_tool'], added: ['weather_tool'] }, channels: { removed: [], added: [] } })
})

describe('ServicesPage', () => {
  it('显示服务状态，停止服务并切换工具', async () => {
    const user = userEvent.setup()
    render(<ServicesPage />)
    expect(await screen.findByText('运行中')).toBeVisible()
    expect(screen.getByText('未创建')).toBeVisible()
    expect(screen.getByText(/bad\.py/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: /停止/ }))
    await waitFor(() => expect(api.controlService).toHaveBeenCalledWith('dream', 'stop'))
    await user.click(screen.getByRole('switch', { name: '启用 wave_memory_search' }))
    await waitFor(() => expect(api.setToolEnabled).toHaveBeenCalledWith('wave_memory_search', false))
  })

  it('列出扩展并重新加载', async () => {
    const user = userEvent.setup()
    render(<ServicesPage />)
    expect(await screen.findByText('weather.py')).toBeVisible()
    expect(screen.getByText('weather_tool')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /重新加载扩展/ }))
    await waitFor(() => expect(api.reloadExtensions).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(api.getServices).toHaveBeenCalledTimes(2))
  })
})

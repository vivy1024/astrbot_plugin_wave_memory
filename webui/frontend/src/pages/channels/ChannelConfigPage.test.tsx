import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { UnsavedChangesProvider } from '@/app/unsaved-changes'
import { setViewport } from '@/test/setup'
import { ChannelConfigPage } from '@/pages/channels/ChannelConfigPage'
import { channelPatchFingerprint, hasFreshChannelPreflight, serializeChannelPatch } from '@/pages/channels/channel-config-state'

const api = vi.hoisted(() => ({
  apply: vi.fn(),
  get: vi.fn(),
  reset: vi.fn(),
  validate: vi.fn(),
}))

vi.mock('@/api/channels', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/channels')>()
  return {
    ...actual,
    applyChannelConfig: api.apply,
    getChannelConfig: api.get,
    resetChannelConfigDefaults: api.reset,
    validateChannelConfig: api.validate,
  }
})
vi.mock('sonner', () => ({ toast: { error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() } }))

const current = {
  recent_dedup_minutes: 30,
  trace_enabled: false,
  channels: {
    safety: { enabled: true, priority: 0, top_k: 1, max_items: 1, token_budget: 100, timeout_ms: 1000, min_score: 0 },
    memory: { enabled: true, priority: 1, top_k: 5, max_items: 5, token_budget: 500, timeout_ms: 2000, min_score: 0.4 },
  },
}

function renderPage() {
  return render(<MemoryRouter><UnsavedChangesProvider><ChannelConfigPage /></UnsavedChangesProvider></MemoryRouter>)
}

beforeEach(() => {
  api.get.mockResolvedValue({
    current,
    runtime: { mode: 'full' },
    revision: 'rev-1',
    limits: { timeout_ms_max: 5000, min_score_min: 0, min_score_max: 1 },
    descriptors: Object.keys(current.channels).map((id) => ({ id, purpose: `${id} purpose`, dependencies: [], risk: 'low', management_route: null, verification_filters: {}, available: true, numeric_limits: id === 'memory' ? { priority: { min: 0, max: 3 } } : undefined })),
  })
  api.validate.mockResolvedValue({ ok: true, errors: [], diff: [], preflight_token: 'token-1' })
  api.reset.mockResolvedValue({ ok: true, errors: [], diff: [], operation: { status: 'succeeded' }, effective: current })
})

describe('ChannelConfigPage 预检安全', () => {
  it('校验成功后任意编辑立即作废 token、差异与应用能力', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '校验预览' }))
    expect(await screen.findByText('校验通过')).toBeVisible()
    expect(screen.getByRole('button', { name: '应用配置' })).toBeEnabled()

    const input = screen.getByRole('spinbutton', { name: 'memory 优先级' })
    await user.clear(input)
    await user.type(input, '2')

    expect(screen.queryByText('校验通过')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '应用配置' })).toBeDisabled()
  })

  it('descriptor 数值范围与空草稿会阻止预检，且不会静默写成 0', async () => {
    const user = userEvent.setup()
    renderPage()
    const input = await screen.findByRole('spinbutton', { name: 'memory 优先级' })

    await user.clear(input)
    expect(screen.getByText('memory 优先级不能为空')).toBeVisible()
    expect(screen.getByRole('button', { name: '校验预览' })).toBeDisabled()
    expect(api.validate).not.toHaveBeenCalled()

    await user.type(input, '9')
    expect(screen.getByText('memory 优先级不能大于 3')).toBeVisible()
    expect(screen.getByRole('button', { name: '校验预览' })).toBeDisabled()
    expect(api.validate).not.toHaveBeenCalled()
  })

  it('恢复默认取消时不调用服务端写 API', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '恢复默认' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('重置为出厂推荐值')
    await user.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.reset).not.toHaveBeenCalled()
  })

  it('apply 的硬校验拒绝与 token 不一致的 patch fingerprint', () => {
    const patch = serializeChannelPatch(current)
    const validation = { ok: true, preflight_token: 'token-1' }
    const fingerprint = channelPatchFingerprint(patch)
    expect(hasFreshChannelPreflight(validation, fingerprint, patch)).toBe(true)
    expect(hasFreshChannelPreflight(validation, fingerprint, { ...patch, trace_enabled: true })).toBe(false)
  })

  it('只读取通道配置，不加载或展示配置建议审核链路', async () => {
    renderPage()

    expect(await screen.findByRole('spinbutton', { name: 'memory 优先级' })).toBeVisible()
    expect(screen.queryByText('配置建议')).not.toBeInTheDocument()
  })

  it('窄屏将通道参数表降级为可编辑卡片而非裁切参数列', async () => {
    setViewport(390)
    const view = renderPage()
    expect(await screen.findByRole('spinbutton', { name: 'memory 优先级' })).toBeVisible()
    await waitFor(() => expect(view.container.querySelector('[data-responsive-table="cards"]')).toBeInTheDocument())
    expect(view.container.querySelector('[data-responsive-table="cards"] table')).not.toBeInTheDocument()
    expect(view.container.querySelector('[data-responsive-table="cards"]')).toHaveTextContent('memory purpose')
  })
})

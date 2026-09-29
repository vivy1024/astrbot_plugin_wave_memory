import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { UnsavedChangesProvider } from '@/app/unsaved-changes'
import { SettingsPage } from '@/pages/settings/SettingsPage'

const api = vi.hoisted(() => ({
  getSchema: vi.fn(),
  getHot: vi.fn(),
  providers: vi.fn(),
  saveFull: vi.fn(),
  saveHot: vi.fn(),
}))

vi.mock('@/api/config', () => ({
  getConfigSchema: api.getSchema,
  getHotConfig: api.getHot,
  listProviders: api.providers,
  saveFullConfig: api.saveFull,
  saveHotConfig: api.saveHot,
}))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() } }))

const state = {
  default: 5,
  saved: 5,
  effective: 5,
  value: 5,
  source: 'plugin_config',
  effective_source: 'runtime_startup_snapshot',
  apply_mode: 'next_run',
  restart_required: false,
  restart_requirement: 'not_required',
}

beforeEach(() => {
  api.getSchema.mockResolvedValue({
    groups: [
      { key: 'retry_count', kind: 'scalar', type: 'int', description: '重试次数', hint: '测试整数', min: 1, max: 10, ...state },
      { key: 'tag_llm_provider_id', kind: 'scalar', type: 'string', special: 'select_provider', description: '标签 Provider', hint: '选择模型', ...state, default: '', saved: 'provider-a', effective: 'provider-a', value: 'provider-a' },
    ],
    warnings: [],
  })
  api.getHot.mockResolvedValue({
    params: [{ key: 'similarity', type: 'float', min: 0, max: 1, default: 0.5, current: 0.5, saved: 0.5, effective: 0.5, description: '相似度阈值' }],
    config: {},
  })
  api.providers.mockRejectedValue(new Error('Provider 服务不可用'))
})

function renderPage() {
  return render(<MemoryRouter><UnsavedChangesProvider><SettingsPage /></UnsavedChangesProvider></MemoryRouter>)
}

describe('SettingsPage Provider 下拉', () => {
  it('默认值为空的 Provider 项可以选回「留空」：模型分工留空即跟随所属默认', async () => {
    api.providers.mockResolvedValue({ providers: [{ id: 'provider-a', name: 'a', model: 'ma' }, { id: 'provider-b', name: 'b', model: 'mb' }] })
    api.getSchema.mockResolvedValue({
      groups: [{
        key: 'Model_Routing_Settings', kind: 'object', description: '模型分工', hint: '',
        items: [{ key: 'diary', type: 'string', special: 'select_provider', description: '每日日记', hint: '留空跟随默认', ...state, default: '', saved: 'provider-b', effective: 'provider-b', value: 'provider-b', apply_mode: 'service' }],
      }],
      warnings: [],
    })
    api.saveFull.mockResolvedValue({ ok: true, saved: true, message: '配置已保存。', schema: { groups: [], warnings: [] } })
    const user = userEvent.setup()
    renderPage()

    const trigger = await screen.findByRole('combobox', { name: '修改 每日日记' })
    expect(trigger).toHaveTextContent('provider-b')
    await user.click(trigger)
    expect(screen.getByRole('option', { name: 'provider-a (ma)' })).toBeVisible()
    await user.click(screen.getByRole('option', { name: '留空：跟随所属默认' }))
    expect(screen.getByRole('combobox', { name: '修改 每日日记' })).toHaveTextContent('留空：跟随所属默认')
    await user.click(screen.getByRole('button', { name: '保存配置' }))
    expect(api.saveFull).toHaveBeenCalledWith({ Model_Routing_Settings: { diary: '' } })
  })

  it('默认值非空的 Provider 项不提供「留空」', async () => {
    api.providers.mockResolvedValue({ providers: [{ id: 'provider-a', name: 'a', model: 'ma' }] })
    api.getSchema.mockResolvedValue({
      groups: [{ key: 'some_provider', kind: 'scalar', type: 'string', special: 'select_provider', description: '某模型', hint: '', ...state, default: 'provider-a', saved: 'provider-a', effective: 'provider-a', value: 'provider-a' }],
      warnings: [],
    })
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('combobox', { name: '修改 某模型' }))
    expect(screen.queryByRole('option', { name: /留空/ })).not.toBeInTheDocument()
  })
})

describe('SettingsPage 分区降级与数值草稿', () => {
  it('Provider 请求失败仍显示 schema 和 hot 主能力，并明确降级状态', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('spinbutton', { name: '修改 重试次数' })).toHaveValue(5)
    expect(await screen.findByRole('alert')).toHaveTextContent('Provider 服务不可用')
    expect(screen.getByDisplayValue('Provider 选项当前不可用')).toBeDisabled()

    await user.click(screen.getByRole('tab', { name: '实时热参数' }))
    expect(await screen.findByRole('spinbutton', { name: '修改 相似度阈值' })).toHaveValue(0.5)
  })

  it('普通数值空值和越界值显示错误、禁止保存且不会写成 0', async () => {
    const user = userEvent.setup()
    renderPage()
    const input = await screen.findByRole('spinbutton', { name: '修改 重试次数' })
    const save = screen.getByRole('button', { name: '保存配置' })

    await user.clear(input)
    expect(screen.getByText('重试次数不能为空')).toBeVisible()
    expect(input).toHaveValue(null)
    expect(save).toBeDisabled()
    expect(api.saveFull).not.toHaveBeenCalled()

    await user.type(input, '99')
    expect(screen.getByText('重试次数不能大于 10')).toBeVisible()
    expect(save).toBeDisabled()
    expect(api.saveFull).not.toHaveBeenCalled()
  })

  it('放弃修改从本地原始快照恢复，不依赖 Provider 重试', async () => {
    const user = userEvent.setup()
    renderPage()
    const input = await screen.findByRole('spinbutton', { name: '修改 重试次数' })
    await user.clear(input)
    await user.type(input, '7')
    expect(input).toHaveValue(7)

    await user.click(screen.getByRole('button', { name: '放弃修改' }))
    expect(input).toHaveValue(5)
    expect(api.providers).toHaveBeenCalledTimes(1)
  })

  it('hot number 支持暂时清空并在恢复有效值后解除错误', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '实时热参数' }))
    const input = await screen.findByRole('spinbutton', { name: '修改 相似度阈值' })
    const save = screen.getByRole('button', { name: '应用热参数' })

    await user.clear(input)
    expect(screen.getByText('相似度阈值不能为空')).toBeVisible()
    expect(save).toBeDisabled()
    expect(api.saveHot).not.toHaveBeenCalled()

    await user.type(input, '0.8')
    expect(screen.queryByText('相似度阈值不能为空')).not.toBeInTheDocument()
    expect(save).toBeEnabled()
  })
})

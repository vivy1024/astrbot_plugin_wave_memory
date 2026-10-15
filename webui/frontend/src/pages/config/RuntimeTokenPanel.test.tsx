import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTokenPayload } from '@/api/runtimeToken'
import { RuntimeTokenPanel } from '@/pages/config/RuntimeTokenPanel'

const api = vi.hoisted(() => ({ get: vi.fn(), change: vi.fn() }))
vi.mock('@/api/runtimeToken', () => ({ getRuntimeToken: api.get, changeRuntimeToken: api.change }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const USAGE = {
  accepted: 3, last_accepted_at: 1_800_000_000, last_accepted_from: '127.0.0.1', last_accepted_path: '/api/runtime/v1/context/prepare',
  rejected: 0, last_rejected_at: null, last_rejected_from: '', last_rejected_path: '',
}

function payload(over: Partial<RuntimeTokenPayload> = {}): RuntimeTokenPayload {
  return {
    token: 'public-default-token', source: 'default', is_default: true, env_var: 'WAVEMEMORY_RUNTIME_TOKEN',
    env_set: false, can_edit: true, min_length: 16, updated_at: null, usage: USAGE, ...over,
  }
}

describe('RuntimeTokenPanel', () => {
  beforeEach(() => {
    api.get.mockReset()
    api.change.mockReset()
  })

  it('默认令牌醒目警告，令牌默认遮住、可显示，并展示调用情况', async () => {
    api.get.mockResolvedValue(payload())
    const user = userEvent.setup()
    render(<RuntimeTokenPanel />)
    expect(await screen.findByText('正在使用公开的默认令牌')).toBeInTheDocument()
    const field = screen.getByLabelText('当前 Runtime 令牌')
    expect(field).toHaveAttribute('type', 'password')
    await user.click(screen.getByRole('button', { name: '显示令牌' }))
    expect(field).toHaveAttribute('type', 'text')
    expect(field).toHaveValue('public-default-token')
    expect(screen.getByText('3 次')).toBeInTheDocument()
    expect(screen.getByText(/127\.0\.0\.1/)).toBeInTheDocument()
  })

  it('生成新令牌要先确认，确认后显示新值', async () => {
    api.get.mockResolvedValue(payload())
    api.change.mockResolvedValue({
      ...payload({ token: 'fresh-token-abcdefghijklmnop', source: 'database', is_default: false, updated_at: 1_800_000_100 }), ok: true,
    })
    const user = userEvent.setup()
    render(<RuntimeTokenPanel />)
    await user.click(await screen.findByRole('button', { name: /生成新令牌/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/所有调用方都要同步改成新值/)).toBeInTheDocument()
    expect(api.change).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: '确认更换' }))
    expect(api.change).toHaveBeenCalledWith({ action: 'generate' })
    expect(await screen.findByDisplayValue('fresh-token-abcdefghijklmnop')).toBeInTheDocument()
    expect(screen.queryByText('正在使用公开的默认令牌')).not.toBeInTheDocument()
  })

  it('自定义令牌太短时不能保存', async () => {
    api.get.mockResolvedValue(payload())
    const user = userEvent.setup()
    render(<RuntimeTokenPanel />)
    await user.click(await screen.findByRole('button', { name: '自定义…' }))
    await user.type(screen.getByLabelText('自定义令牌'), 'short')
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
    expect(screen.getByText('还差 11 个字符')).toBeInTheDocument()
  })

  it('环境变量设置时只读', async () => {
    api.get.mockResolvedValue(payload({ source: 'env', is_default: false, env_set: true, can_edit: false }))
    render(<RuntimeTokenPanel />)
    expect(await screen.findByText(/由环境变量/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /生成新令牌/ })).not.toBeInTheDocument()
  })
})

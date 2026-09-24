import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { emptyBot } from '@/api/bots'
import { BotsPage } from '@/pages/bots/BotsPage'

const api = vi.hoisted(() => ({
  listBots: vi.fn(),
  getBot: vi.fn(),
  saveBot: vi.fn(),
  setBotEnabled: vi.fn(),
  exportBots: vi.fn(),
  importBots: vi.fn(),
}))

vi.mock('@/api/bots', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api/bots')>()), ...api }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const yushu = { ...emptyBot('yushu'), name: '羽书', qq_id: '2500447291', version: 3, origin: 'config', self_ids: ['2500447291'] }

beforeEach(() => {
  vi.clearAllMocks()
  api.listBots.mockResolvedValue({
    items: [yushu],
    status: { source: 'database', attached: true, revision: 1, enabled: 1, total: 1, legacy_slots: [], migration: {}, invalid_rows: {} },
    binding_hosts: ['astrbot', 'cortico', 'bilibili'],
  })
  api.getBot.mockResolvedValue({ item: yushu, counts: { user_profiles: 5 }, history: [] })
})

describe('BotsPage', () => {
  it('列出 Bot 并显示来源', async () => {
    render(<BotsPage />)
    expect(await screen.findByText('羽书')).toBeVisible()
    expect(screen.getByText('旧配置迁移')).toBeVisible()
    expect(screen.getByText(/来源：数据库/)).toBeVisible()
  })

  it('新建 Bot 以版本 0 保存', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...emptyBot('bot_c'), name: '丙', version: 1 } })
    render(<BotsPage />)
    await screen.findByText('羽书')
    await user.click(screen.getByRole('button', { name: /新建 Bot/ }))
    await user.type(screen.getByLabelText('db_id（稳定主键）'), 'bot_c')
    await user.type(screen.getByLabelText('显示名'), '丙')
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveBot).toHaveBeenCalled())
    const [dbId, item, version] = api.saveBot.mock.calls[0]
    expect(dbId).toBe('bot_c')
    expect(item.name).toBe('丙')
    expect(version).toBe(0)
  })

  it('编辑已有 Bot 时 db_id 不可改，保存带上读到的版本', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...yushu, version: 4 } })
    render(<BotsPage />)
    await user.click(await screen.findByText('羽书'))
    const dbId = await screen.findByLabelText('db_id（稳定主键）')
    expect(dbId).toBeDisabled()
    await user.type(screen.getByLabelText('别名（逗号分隔）'), '器灵')
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveBot).toHaveBeenCalled())
    expect(api.saveBot.mock.calls[0][2]).toBe(3)
    expect(api.saveBot.mock.calls[0][1].aliases).toEqual(['器灵'])
  })
})

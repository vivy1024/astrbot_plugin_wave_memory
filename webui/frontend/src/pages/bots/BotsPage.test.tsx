import { MemoryRouter } from 'react-router-dom'
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
vi.mock('@/api/config', () => ({ listProviders: async () => ({ providers: [{ id: 'nug/meta', name: 'meta', model: 'gemini' }] }) }))
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
    render(<MemoryRouter><BotsPage /></MemoryRouter>)
    expect(await screen.findByText('羽书')).toBeVisible()
    expect(screen.getByText('旧配置迁移')).toBeVisible()
    expect(screen.getByText(/来源：数据库/)).toBeVisible()
  })

  it('新建 Bot 以版本 0 保存', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...emptyBot('bot_c'), name: '丙', version: 1 } })
    render(<MemoryRouter><BotsPage /></MemoryRouter>)
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
    render(<MemoryRouter><BotsPage /></MemoryRouter>)
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

describe('BotsPage 心情画像', () => {
  it('心情标签页编辑敏感度与说话倾向，保存时带上 mood 段', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...yushu, version: 4 } })
    render(<MemoryRouter initialEntries={['/bots?bot=yushu&tab=mood']}><BotsPage /></MemoryRouter>)
    const sensitivity = await screen.findByLabelText('敏感度（0~3）')
    await user.clear(sensitivity)
    await user.type(sensitivity, '1.5')
    await user.type(screen.getByLabelText('各心情下的说话倾向（每行「情绪词=提示」）'), '烦躁=句子变短')
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveBot).toHaveBeenCalled())
    const mood = api.saveBot.mock.calls[0][1].mood
    expect(mood.sensitivity).toBe(1.5)
    expect(mood.speaking_hints).toEqual({ 烦躁: '句子变短' })
    expect(mood.half_life_minutes).toBe(0)
  })
})

describe('BotsPage 模型覆盖', () => {
  it('行为标签页填默认模型与按任务覆盖，清空的任务不保存', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...yushu, version: 4 } })
    render(<MemoryRouter initialEntries={['/bots?bot=yushu&tab=behavior']}><BotsPage /></MemoryRouter>)
    const model = await screen.findByLabelText('默认模型')
    await user.type(model, 'nug/meta')
    await user.type(screen.getByLabelText('模型 · 每日日记'), 'nug/diary')
    const bookkeeping = screen.getByLabelText('模型 · 事后记账')
    await user.type(bookkeeping, 'x')
    await user.clear(bookkeeping)
    expect(screen.getByLabelText('模型 · 关系小传（预留）')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveBot).toHaveBeenCalled())
    const item = api.saveBot.mock.calls[0][1]
    expect(item.model).toBe('nug/meta')
    expect(item.models).toEqual({ diary: 'nug/diary' })
  })
})

describe('BotsPage 角色经历', () => {
  it('角色经历标签页默认关闭，保存时带上 character_experience 段', async () => {
    const user = userEvent.setup()
    api.saveBot.mockResolvedValue({ ok: true, item: { ...yushu, version: 4 } })
    render(<MemoryRouter initialEntries={['/bots?bot=yushu&tab=character']}><BotsPage /></MemoryRouter>)
    const toggle = await screen.findByRole('switch', { name: '启用角色经历' })
    expect(toggle).not.toBeChecked()
    await user.click(toggle)
    const pronoun = screen.getByLabelText('代词')
    expect(pronoun).toHaveValue('ta')
    await user.selectOptions(pronoun, '她')
    const topK = screen.getByLabelText('每次最多想起几条（1~10）')
    await user.clear(topK)
    await user.type(topK, '5')
    await user.type(screen.getByLabelText('接管的旧记忆来源（逗号分隔）'), 'demo_source')
    expect(screen.getByRole('link', { name: '打开角色经历工作台' })).toHaveAttribute('href', '/knowledge/character-experience?bot_id=yushu')
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveBot).toHaveBeenCalled())
    const [, item, version] = api.saveBot.mock.calls[0]
    expect(version).toBe(3)
    expect(item.character_experience).toMatchObject({ enabled: true, current_stage: '', pronoun: '她', top_k: 5, legacy_sources: ['demo_source'] })
  })
})

describe('BotsPage 深链接', () => {
  it('?bot= 直接打开对应 Bot', async () => {
    render(<MemoryRouter initialEntries={['/bots?bot=yushu&tab=behavior']}><BotsPage /></MemoryRouter>)
    await waitFor(() => expect(api.getBot).toHaveBeenCalledWith('yushu', expect.anything()))
  })
})

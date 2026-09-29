import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { GameKnowledgePage } from '@/pages/knowledge/GameKnowledgePage'

const api = vi.hoisted(() => ({
  getGameOverview: vi.fn(),
  createGame: vi.fn(),
  updateGame: vi.fn(),
  getGame: vi.fn(),
  listGameCards: vi.fn(),
  listGameTerms: vi.fn(),
  getGameCard: vi.fn(),
  getGameTerm: vi.fn(),
  saveGameCard: vi.fn(),
  saveGameTerm: vi.fn(),
  setGameItemsStatus: vi.fn(),
  previewGameImport: vi.fn(),
  runGameImport: vi.fn(),
  exportGame: vi.fn(),
  trialGameMatch: vi.fn(),
  importGamePack: vi.fn(),
  scanLegacySource: vi.fn(),
  migrateLegacySource: vi.fn(),
}))
vi.mock('@/api/gameKnowledge', () => api)
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast }))

const counts = (active: number, draft: number) => ({ cards: { active, draft, archived: 0 }, terms: { active: 1, draft: 0, archived: 0 } })

const overview = {
  games: [
    { id: 'minecraft', name: '我的世界', aliases: ['MC'], description: 'Java 版', enabled: true, version: 3, created_at: 1790000000, updated_at: 1790000000, counts: counts(1, 2) },
    { id: 'slay_the_spire', name: '杀戮尖塔', aliases: [], description: '', enabled: true, version: 1, created_at: 1790000000, updated_at: 1790000000, counts: counts(0, 0) },
  ],
  packs: [{ id: 'minecraft_starter', title: '我的世界起始包', description: '生存前期常识', force_status: 'draft', game: { id: 'minecraft', name: '我的世界' }, cards: 38, terms: 31, game_exists: true }],
  legacy_sources: [{ source: 'oni_lore', game_id: 'oni', game_name: '缺氧', game_exists: false, description: '字幕知识点', scanned: false, rows: null, scanned_at: null, migrated: 0 }],
  statuses: { draft: '草稿', active: '启用', archived: '归档' },
  source_types: {},
  import_modes: ['skip', 'update', 'merge'],
  trigger_rule: '写了关键词：关键词命中才触发……',
}

function card(overrides: Record<string, unknown> = {}) {
  return {
    id: 7, game_id: 'minecraft', key: 'mc_creeper', title: '苦力怕', stage: '前期', points: ['听到嘶嘶声马上后退'], pitfalls: ['爆炸会炸坏方块'],
    keywords: ['苦力怕', '爬行者'], conditions: [], conditions_text: [], trigger_mode: 'keywords', source: '起始包', source_url: '', source_type: 'starter_pack',
    status: 'active', version: 2, note: '', meta: {}, created_at: 1790000000, updated_at: 1790000000, ...overrides,
  }
}

const draftCard = card({ id: 8, key: 'mc_night', title: '第一夜怎么过', status: 'draft', version: 1, keywords: [], conditions_text: ['time_of_day == night'], note: '模型起草，待审核' })

function renderPage(entry = '/knowledge/games') {
  return render(<MemoryRouter initialEntries={[entry]}><GameKnowledgePage /></MemoryRouter>)
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getGameOverview.mockResolvedValue(overview)
  api.getGame.mockResolvedValue({ item: overview.games[0], stages: [{ name: '前期', count: 3 }] })
  api.listGameCards.mockImplementation((_game: string, params: { status?: string }) => Promise.resolve(
    params.status === 'draft' ? { items: [draftCard], total: 1, limit: 200, offset: 0 } : { items: [card(), draftCard], total: 2, limit: 30, offset: 0 },
  ))
  api.listGameTerms.mockResolvedValue({ items: [], total: 0, limit: 30, offset: 0 })
  api.getGameCard.mockResolvedValue({ item: card() })
  api.saveGameCard.mockResolvedValue({ ok: true, revision: 3, item: card({ version: 3 }) })
  api.setGameItemsStatus.mockResolvedValue({ ok: true, status: 'active', changed: [8], unchanged: [], missing: [], conflicts: [] })
  api.createGame.mockResolvedValue({ ok: true, revision: 1, item: { ...overview.games[1], id: 'sts', name: '尖塔' } })
  api.importGamePack.mockResolvedValue({ ok: true, cards: { created: 38, updated: 0, unchanged: 0, skipped: 0 }, terms: { created: 31, updated: 0, unchanged: 0, skipped: 0 }, invalid: { cards: 0, terms: 0 }, errors: [], warnings: [] })
  api.previewGameImport.mockResolvedValue({
    format: 'oni_timeline', format_label: '缺氧教程时间线（oni_timeline.json）', game_id: 'minecraft', file_game: {}, force_status: null, default_status_mode: 'auto',
    warnings: ['extras 只有知识点编号'], cards: { total: 2, new: 0, existing: 2, invalid: 0, by_status: { active: 2 } }, terms: { total: 0, new: 0, existing: 0, invalid: 0, by_status: {} },
    errors: [], samples: { cards: [{ key: 'oni_ep01', status: 'active', exists: true, title: 'EP01 虚构开局', stage: '筑基开局', keywords: ['EP01'], conditions: ['cycle 0..30'], points: 3, pitfalls: 1, source: '' }], terms: [] },
  })
  api.runGameImport.mockResolvedValue({ ok: true, format: 'oni_timeline', format_label: '', status_mode: 'auto', on_existing: 'merge', cards: { created: 0, updated: 2, unchanged: 0, skipped: 0 }, terms: { created: 0, updated: 0, unchanged: 0, skipped: 0 }, invalid: { cards: 0, terms: 0 }, errors: [], warnings: [] })
  api.trialGameMatch.mockResolvedValue({
    game_id: 'minecraft', include_drafts: true, state: {}, keywords: [], warnings: [], reason: '', rule: '规则',
    cards: [{ id: 8, key: 'mc_night', title: '第一夜怎么过', stage: '前期', status: 'draft', score: 2.8, reason: '条件全部满足：time_of_day=night' }],
    overflow: [], terms: [], blocked: [], text: '[游戏攻略参考 · 我的世界：……]',
  })
})

describe('GameKnowledgePage', () => {
  it('列出游戏库并显示所选游戏的知识卡与触发条件', async () => {
    renderPage()
    expect(await screen.findByRole('heading', { name: '我的世界' })).toBeVisible()
    const list = await screen.findByRole('list', { name: '知识卡列表' })
    expect(within(list).getByText('关键词：苦力怕、爬行者')).toBeVisible()
    expect(within(list).getByText('条件：time_of_day == night')).toBeVisible()
    expect(within(list).getByText('模型起草，待审核')).toBeVisible()
    expect(api.listGameCards).toHaveBeenCalledWith('minecraft', expect.objectContaining({ limit: 30, offset: 0 }), expect.anything())
  })

  it('知识卡按阶段筛选、按关键字搜索', async () => {
    const user = userEvent.setup()
    renderPage()
    const stage = await screen.findByRole('combobox', { name: '按阶段筛选' })
    await screen.findByRole('option', { name: '前期（3）' })
    await user.selectOptions(stage, '前期')
    await waitFor(() => expect(api.listGameCards).toHaveBeenLastCalledWith('minecraft', expect.objectContaining({ stage: '前期', q: '', offset: 0 }), expect.anything()))
    await user.type(screen.getByRole('textbox', { name: '搜索知识卡' }), '苦力')
    await user.click(screen.getByRole('button', { name: '查询' }))
    await waitFor(() => expect(api.listGameCards).toHaveBeenLastCalledWith('minecraft', expect.objectContaining({ stage: '前期', q: '苦力' }), expect.anything()))
  })

  it('新建游戏库时把别名拆成列表', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /新建游戏库/ }))
    await user.type(screen.getByRole('textbox', { name: '游戏标识' }), 'sts')
    await user.type(screen.getByRole('textbox', { name: '显示名' }), '尖塔')
    await user.type(screen.getByRole('textbox', { name: '游戏别名' }), '杀戮尖塔、STS')
    await user.click(screen.getByRole('button', { name: '创建' }))
    await waitFor(() => expect(api.createGame).toHaveBeenCalledWith({ id: 'sts', name: '尖塔', aliases: ['杀戮尖塔', 'STS'], description: '', enabled: true }))
  })

  it('编辑知识卡带版本保存；版本冲突时提示重新打开', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /苦力怕/ }))
    const keywords = await screen.findByRole('textbox', { name: '触发关键词' })
    expect(keywords).toHaveValue('苦力怕、爬行者')
    await user.clear(keywords)
    await user.type(keywords, '苦力怕，creeper')
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.saveGameCard).toHaveBeenCalledWith('minecraft', expect.objectContaining({ keywords: ['苦力怕', 'creeper'], points: ['听到嘶嘶声马上后退'] }), 7, 2))

    const conflict = Object.assign(new Error('card_version_conflict'), { status: 409, payload: { error: { code: 'card_version_conflict', current_version: 4 } } })
    api.saveGameCard.mockRejectedValueOnce(conflict)
    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('刚被别处改过（当前版本 4），请重新打开后再改'))
  })

  it('草稿审核：勾选后批量启用，带上读到的版本', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: /草稿审核/ }))
    await user.click(await screen.findByRole('checkbox', { name: '选择 第一夜怎么过' }))
    await user.click(screen.getByRole('button', { name: /批量启用/ }))
    await waitFor(() => expect(api.setGameItemsStatus).toHaveBeenCalledWith('minecraft', 'cards', [8], 'active', { 8: 1 }))
    expect(toast.success).toHaveBeenCalledWith('已启用 1 条')
  })

  it('导入先预览，再按选择的方式导入', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '导入导出' }))
    const file = new File([JSON.stringify({ episodes: [{ ep: 1 }] })], 'oni_timeline.json', { type: 'application/json' })
    await user.upload(screen.getByLabelText('选择导入文件'), file)
    expect(await screen.findByText('缺氧教程时间线（oni_timeline.json）')).toBeVisible()
    expect(api.previewGameImport).toHaveBeenCalledWith('minecraft', { episodes: [{ ep: 1 }] })
    expect(screen.getByRole('combobox', { name: '已有条目' })).toHaveValue('merge')
    expect(screen.getByRole('combobox', { name: '导入状态' })).toHaveValue('auto')
    await user.click(screen.getByRole('button', { name: '确认导入' }))
    await waitFor(() => expect(api.runGameImport).toHaveBeenCalledWith('minecraft', { episodes: [{ ep: 1 }] }, { status: 'auto', on_existing: 'merge' }))
  })

  it('起始包要确认后才导入', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /导入起始包/ }))
    expect(api.importGamePack).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '确认导入为草稿' }))
    await waitFor(() => expect(api.importGamePack).toHaveBeenCalledWith('minecraft_starter'))
  })

  it('试匹配把每行状态解析成数字 / 字符串，并显示命中原因', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '试一下' }))
    await user.type(screen.getByRole('textbox', { name: '试匹配消息' }), '天黑了')
    await user.type(screen.getByRole('textbox', { name: '试匹配状态' }), 'time_of_day=night{enter}health=6')
    await user.click(screen.getByRole('checkbox', { name: /把草稿也算上/ }))
    await user.click(screen.getByRole('button', { name: /试匹配/ }))
    await waitFor(() => expect(api.trialGameMatch).toHaveBeenCalledWith('minecraft', { message: '天黑了', keywords: [], state: { time_of_day: 'night', health: 6 }, include_drafts: true }))
    expect(await screen.findByText('条件全部满足：time_of_day=night')).toBeVisible()
  })

  it('观测台跳转带 game 与 card 时直接打开那张卡', async () => {
    renderPage('/knowledge/games?game=minecraft&card=7')
    await waitFor(() => expect(api.getGameCard).toHaveBeenCalledWith('minecraft', 7))
    expect(await screen.findByText('编辑知识卡 · 版本 2')).toBeVisible()
  })
})

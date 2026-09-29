import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CharacterExperienceWorkbenchPage } from '@/pages/knowledge/character-experience/CharacterExperienceWorkbenchPage'

// 全部是虚构的角色、原文与经历
const api = vi.hoisted(() => ({
  getCharacterOverview: vi.fn(),
  listCharacterExperiences: vi.fn(),
  getCharacterExperience: vi.fn(),
  createCharacterExperience: vi.fn(),
  updateCharacterExperience: vi.fn(),
  setCharacterExperienceStatus: vi.fn(),
  setCharacterExperienceStage: vi.fn(),
  saveCharacterSettings: vi.fn(),
  saveTimelineStage: vi.fn(),
  deleteTimelineStage: vi.fn(),
  reorderTimelineStages: vi.fn(),
  confirmTimelineStages: vi.fn(),
  importCharacterJsonl: vi.fn(),
  exportCharacterJsonl: vi.fn(),
  previewExtraction: vi.fn(),
  startExtraction: vi.fn(),
  previewTimelineDraft: vi.fn(),
  startTimelineDraft: vi.fn(),
  previewExpansion: vi.fn(),
  startExpansion: vi.fn(),
  startEmbedding: vi.fn(),
  listCharacterJobs: vi.fn(),
  resumeCharacterJob: vi.fn(),
  cancelCharacterJob: vi.fn(),
  tryCharacterRecall: vi.fn(),
  runLegacyMigration: vi.fn(),
}))
vi.mock('@/api/characterExperience', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api/characterExperience')>()), ...api }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const zero = { draft: 0, active: 0, archived: 0 }
const stageBase = { bot_id: 'oc_demo', description: '', chapter_from: null, chapter_to: null, source_kind: 'manual', source_ref: {}, created_at: 1790000000, updated_at: 1790000000 }
const stages = [
  { ...stageBase, stage_key: 's1', ordinal: 1, title: '山村童年', story_time: '七岁到十二岁', key_events: [{ text: '在溪边捡到一只受伤的白鹭', basis: [1] }], chapter_from: 1, chapter_to: 10, status: 'confirmed', version: 2, counts: { draft: 1, active: 1, archived: 0 }, is_current: true, recallable: true },
  { ...stageBase, stage_key: 's2', ordinal: 2, title: '下山学艺', story_time: '十二岁以后', key_events: [], status: 'confirmed', version: 1, counts: zero, is_current: false, recallable: false },
  { ...stageBase, stage_key: 's3', ordinal: 3, title: '远行', story_time: '', key_events: [], status: 'draft', version: 1, counts: zero, is_current: false, recallable: false },
]
const persona = '青芷生在山村，爹娘都是采药人。\n\n十二岁那年她下山拜师。'
const overview = {
  bot: { db_id: 'oc_demo', name: '青芷', aliases: ['阿芷'], enabled: true, version: 7, persona_text: persona },
  settings: { enabled: false, current_stage: 's1', pronoun: 'ta', top_k: 3, max_chars: 600, min_score: 0.35, legacy_sources: [], lore_categories: [] },
  stages,
  unassigned: zero,
  counts: { total: 2, by_status: { draft: 1, active: 1, archived: 0 }, by_stage: { s1: { draft: 1, active: 1, archived: 0 } }, by_canon: { canon: 1, inferred: 0, invented: 1 }, by_source: { manual: 2 }, active_missing_vectors: 0 },
  recallable_active: 1,
  warnings: [{ code: 'feature_disabled', message: '这个 Bot 还没开启角色经历：启用的经历暂时不会被想起' }],
  model: { available: true, primary: 'demo-model', source_label: '模型分工' },
  embedding_available: true,
  jobs: [] as unknown[],
  scene_stats: {},
  migrations: [],
  sources: [{ path: '/data/虚构山村记.txt', name: '虚构山村记.txt', size: 1048576, modified_at: 1790000000 }],
}
const rowBase = { bot_id: 'oc_demo', seq: 1, plot_stage: '', characters: [], source_kind: 'manual', source_ref: {}, job_id: '', created_by: 'webui', created_at: 1790000000, updated_at: 1790000000 }
const rows = [
  { ...rowBase, id: 1, content: '七岁那年我在溪边捡到一只受伤的白鹭，偷偷养在柴房里。', story_time: '七岁夏天', stage_key: 's1', emotion: '温馨', canon_level: 'invented', status: 'draft', version: 3, has_vector: false },
  { ...rowBase, id: 2, content: '我第一次跟着阿爹进城，看见糖画摊子就挪不动脚。', story_time: '八岁', stage_key: 's1', emotion: '日常', canon_level: 'canon', status: 'active', version: 1, has_vector: true },
]
const extractionPlan = {
  source: { path: '/data/虚构山村记.txt', name: '虚构山村记.txt' },
  params: {},
  chapters_total: 40,
  chapters_in_range: 40,
  chapters_with_character: 12,
  scenes: 14,
  already_processed: 2,
  calls: 12,
  by_stage: { s1: 5, '': 7 },
  unassigned_scenes: 7,
  sample: [{ chapter: 1, title: '第1章 溪边', chars: 820, preview: '青芷蹲在溪边洗药草……' }],
  model: { available: true, primary: 'demo-model', source_label: '模型分工' },
}
const extractionParams = {
  path: '/data/虚构山村记.txt', name: '青芷', aliases: '阿芷', description: '',
  chapter_from: null, chapter_to: null, require_full_name: true, redo: false, concurrency: 2,
}

function renderPage(entry = '/knowledge/character-experience?bot_id=oc_demo') {
  return render(<MemoryRouter initialEntries={[entry]}><CharacterExperienceWorkbenchPage /></MemoryRouter>)
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getCharacterOverview.mockResolvedValue(overview)
  api.listCharacterExperiences.mockResolvedValue({ items: rows, total: 2, limit: 30, offset: 0 })
  api.getCharacterExperience.mockResolvedValue({ item: rows[0], stage: stages[0] })
  api.saveCharacterSettings.mockResolvedValue({ ok: true, version: 8, settings: overview.settings })
  api.setCharacterExperienceStatus.mockResolvedValue({ ok: true, changed: [1, 2] })
  api.confirmTimelineStages.mockResolvedValue({ ok: true, confirmed: ['s3'] })
  api.reorderTimelineStages.mockResolvedValue({ ok: true, items: stages })
  api.previewExtraction.mockResolvedValue(extractionPlan)
  api.startExtraction.mockResolvedValue({ ok: true, calls: 12, job: { job_id: 'run-1', request_id: 'req-1', status: 'queued' } })
  api.previewTimelineDraft.mockResolvedValue({ calls: 1, paragraphs: 2, setting_chars: 30 })
  api.startTimelineDraft.mockResolvedValue({ ok: true, calls: 1, job: { job_id: 'run-2', request_id: 'req-2', status: 'queued' } })
  api.previewExpansion.mockResolvedValue({ stages: ['s1'], unconfirmed_stages: [], events: 1, already_processed: 0, calls: 1, model: overview.model })
  api.startExpansion.mockResolvedValue({ ok: true, calls: 1, job: { job_id: 'run-3', request_id: 'req-3', status: 'queued' } })
  api.importCharacterJsonl.mockResolvedValue({ ok: true, lines: 1, valid: 1, inserted: 1, updated: 0, duplicates: 0, errors: [], error_count: 0, stages_created: ['arc01_入门'] })
  api.resumeCharacterJob.mockResolvedValue({ ok: true, run_id: 'run-9', generation: 1 })
  api.cancelCharacterJob.mockResolvedValue({ status: 'cancel_requested' })
})

describe('CharacterExperienceWorkbenchPage', () => {
  it('没选 Bot 时提示先选 Bot，不请求数据', async () => {
    renderPage('/knowledge/character-experience')
    expect(await screen.findByText('先选一个 Bot')).toBeVisible()
    expect(api.getCharacterOverview).not.toHaveBeenCalled()
  })

  it('显示概况与提醒，开关写进 Bot 资料并带上版本', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText('青芷 的角色经历')).toBeVisible()
    expect(screen.getByText('这个 Bot 还没开启角色经历：启用的经历暂时不会被想起')).toBeVisible()
    expect(screen.getByText('只会想起「山村童年」及之前的经历')).toBeVisible()
    await user.click(screen.getByRole('switch', { name: '启用角色经历' }))
    await waitFor(() => expect(api.saveCharacterSettings).toHaveBeenCalledWith('oc_demo', 7, { enabled: true }))
    await waitFor(() => expect(api.getCharacterOverview).toHaveBeenCalledTimes(2))
  })

  it('当前时间点下拉只能选时间线里的阶段', async () => {
    const user = userEvent.setup()
    renderPage()
    const select = await screen.findByRole('combobox', { name: '当前时间点' })
    await user.selectOptions(select, 's2')
    await waitFor(() => expect(api.saveCharacterSettings).toHaveBeenCalledWith('oc_demo', 7, { current_stage: 's2' }))
  })

  it('概况里改代词，同样写进 Bot 资料并带上版本', async () => {
    const user = userEvent.setup()
    renderPage()
    const select = await screen.findByRole('combobox', { name: '代词' })
    expect(select).toHaveValue('ta')
    expect(within(select).getAllByRole('option').map((option) => option.getAttribute('value'))).toEqual(['ta', '她', '他'])
    await user.selectOptions(select, '他')
    await waitFor(() => expect(api.saveCharacterSettings).toHaveBeenCalledWith('oc_demo', 7, { pronoun: '他' }))
  })

  it('批量启用带上每条读到的版本', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('checkbox', { name: '选择经历 1' }))
    await user.click(screen.getByRole('checkbox', { name: '选择经历 2' }))
    expect(screen.getByText('已选 2 条')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '批量启用' }))
    await waitFor(() => expect(api.setCharacterExperienceStatus).toHaveBeenCalledWith('oc_demo', [{ id: 1, version: 3 }, { id: 2, version: 1 }], 'active'))
  })

  it('编辑只提交改动的字段；版本冲突时提示错误', async () => {
    const user = userEvent.setup()
    api.updateCharacterExperience.mockRejectedValueOnce(Object.assign(new Error('经历已被别人修改，请刷新后再改'), { status: 409 }))
    renderPage()
    await user.click(await screen.findByRole('button', { name: /受伤的白鹭/ }))
    const content = await screen.findByRole('textbox', { name: '经历正文' })
    expect(screen.getByText('出处')).toBeVisible()
    await user.clear(content)
    await user.type(content, '七岁那年我在溪边捡到一只白鹭。')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(api.updateCharacterExperience).toHaveBeenCalledWith('oc_demo', 1, 3, { content: '七岁那年我在溪边捡到一只白鹭。' }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('经历已被别人修改，请刷新后再改'))
  })

  it('时间线：确认全部草稿阶段、调整顺序', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '时间线' }))
    await user.click(await screen.findByRole('button', { name: /确认全部草稿阶段（1）/ }))
    await waitFor(() => expect(api.confirmTimelineStages).toHaveBeenCalledWith('oc_demo', [{ stage_key: 's3', version: 1 }]))
    const up = screen.getByRole('button', { name: '上移 下山学艺' })
    await waitFor(() => expect(up).toBeEnabled())
    await user.click(up)
    await waitFor(() => expect(api.reorderTimelineStages).toHaveBeenCalledWith('oc_demo', [
      { stage_key: 's2', version: 1 }, { stage_key: 's1', version: 2 }, { stage_key: 's3', version: 1 },
    ]))
    const timeline = screen.getByRole('list', { name: '时间线阶段' })
    const [makeCurrent] = within(timeline).getAllByRole('button', { name: '设为当前时间点' })
    await waitFor(() => expect(makeCurrent).toBeEnabled())
    await user.click(makeCurrent)
    await waitFor(() => expect(api.saveCharacterSettings).toHaveBeenCalledWith('oc_demo', 7, { current_stage: 's2' }))
  })

  it('从原著提取：先预览，再确认调用次数才开始', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '从原著提取' }))
    await user.click(screen.getByRole('button', { name: '预览' }))
    await waitFor(() => expect(api.previewExtraction).toHaveBeenCalledWith('oc_demo', extractionParams))
    expect(await screen.findByText('这次预计调用模型 12 次（每个场景一次）')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '开始提取（12 次调用）' }))
    expect(api.startExtraction).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '确认开始' }))
    await waitFor(() => expect(api.startExtraction).toHaveBeenCalledWith('oc_demo', extractionParams, 12))
  })

  it('开始时计划变了（409）：换成新计划，要重新确认', async () => {
    const user = userEvent.setup()
    api.startExtraction.mockRejectedValueOnce(Object.assign(new Error('这次预计调用模型 15 次，请确认后再开始'), {
      status: 409,
      payload: { error: { code: 'confirmation_required' }, plan: { ...extractionPlan, calls: 15 } },
    }))
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '从原著提取' }))
    await user.click(screen.getByRole('button', { name: '预览' }))
    await user.click(await screen.findByRole('button', { name: '开始提取（12 次调用）' }))
    await user.click(screen.getByRole('button', { name: '确认开始' }))
    expect(await screen.findByText('这次预计调用模型 15 次（每个场景一次）')).toBeVisible()
    expect(toast.error).toHaveBeenCalledWith('计划有变化：现在预计调用模型 15 次，请重新确认')
    expect(api.startExtraction).toHaveBeenCalledTimes(1)
  })

  it('改了参数，之前的预览作废', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '从原著提取' }))
    await user.click(screen.getByRole('button', { name: '预览' }))
    expect(await screen.findByText('这次预计调用模型 12 次（每个场景一次）')).toBeVisible()
    await user.type(screen.getByRole('textbox', { name: '起始章' }), '5')
    expect(screen.queryByText('这次预计调用模型 12 次（每个场景一次）')).toBeNull()
  })

  it('按设定编写：起草时间线与展开关键事件都要确认', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '按设定编写' }))
    expect(screen.getByRole('textbox', { name: '设定文本' })).toHaveValue(persona)
    await user.click(screen.getByRole('button', { name: '预览起草' }))
    await waitFor(() => expect(api.previewTimelineDraft).toHaveBeenCalledWith('oc_demo', persona))
    await user.click(await screen.findByRole('button', { name: '起草时间线' }))
    await user.click(screen.getByRole('button', { name: '确认起草' }))
    await waitFor(() => expect(api.startTimelineDraft).toHaveBeenCalledWith('oc_demo', persona, false))

    await user.click(screen.getByRole('checkbox', { name: '展开 山村童年' }))
    const previewExpand = screen.getByRole('button', { name: '预览展开' })
    await waitFor(() => expect(previewExpand).toBeEnabled())
    await user.click(previewExpand)
    await waitFor(() => expect(api.previewExpansion).toHaveBeenCalledWith('oc_demo', ['s1'], false))
    await user.click(await screen.findByRole('button', { name: '开始展开（1 次调用）' }))
    await user.click(screen.getByRole('button', { name: '确认展开' }))
    await waitFor(() => expect(api.startExpansion).toHaveBeenCalledWith('oc_demo', { stage_keys: ['s1'], setting: persona, redo: false }, 1))
  })

  it('任务列表：失败的可以续跑，运行中的可以取消', async () => {
    const user = userEvent.setup()
    const job = { request_id: 'req', payload: {}, progress: { processed: 3, total: 12, done: 3, memories: 4, empty: 0, failed: 1 }, result: null, error_code: null, created_at: 1790000000, updated_at: 1790000000, generation: 0 }
    api.getCharacterOverview.mockResolvedValue({
      ...overview,
      jobs: [
        { ...job, run_id: 'run-8', kind: 'character_experience.extract.v1', status: 'running', error_message: null, resumable: false },
        { ...job, run_id: 'run-9', kind: 'character_experience.extract.v1', status: 'failed', error_message: '连续 5 个场景调用模型失败，已停下', resumable: true },
      ],
    })
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '从原著提取' }))
    const list = screen.getByRole('list', { name: '任务列表' })
    expect(within(list).getAllByText('已处理 3/12 · 新增 4 条草稿 · 失败 1')).toHaveLength(2)
    await user.click(within(list).getByRole('button', { name: '续跑' }))
    await waitFor(() => expect(api.resumeCharacterJob).toHaveBeenCalledWith('run-9'))
    await user.click(within(list).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(api.cancelCharacterJob).toHaveBeenCalledWith('run-8'))
  })

  it('导入 JSONL：按选项提交并显示结果', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('tab', { name: '导入导出' }))
    const line = '{"memory": "那年冬天我第一次下山。", "arc": "arc01_入门", "approx_time": "第1年冬"}'
    await user.click(screen.getByRole('textbox', { name: 'JSONL 内容' }))
    await user.paste(line)
    await user.selectOptions(screen.getByRole('combobox', { name: '重复时' }), 'fill')
    await user.click(screen.getByRole('button', { name: '导入 1 行' }))
    await waitFor(() => expect(api.importCharacterJsonl).toHaveBeenCalledWith('oc_demo', { text: line, status: 'draft', canon_level: 'canon', on_duplicate: 'fill' }))
    expect(await screen.findByText('新建阶段：arc01_入门')).toBeVisible()
  })
})

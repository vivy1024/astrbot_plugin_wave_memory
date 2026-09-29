import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfigCenterPage } from '@/pages/config/ConfigCenterPage'

const api = vi.hoisted(() => ({ schema: vi.fn(), hot: vi.fn(), inventory: vi.fn(), routing: vi.fn() }))

vi.mock('@/api/config', () => ({ getConfigSchema: api.schema, getHotConfig: api.hot }))
vi.mock('@/api/configInventory', () => ({ getConfigInventory: api.inventory }))
vi.mock('@/api/llmRouting', () => ({ getLlmRouting: api.routing }))
// 嵌入的三个页面有各自的测试；这里只验证配置中心把它们挂上并传递定位参数。
vi.mock('@/pages/settings/SettingsPage', () => ({ SettingsPage: () => <div>系统配置页桩</div> }))
vi.mock('@/pages/channels/ChannelConfigPage', () => ({ ChannelConfigPage: () => <div>通道配置页桩</div> }))
vi.mock('@/pages/settings/ConfigInventoryPage', () => ({ ConfigInventoryPage: () => <div>配置来源页桩</div> }))

const state = {
  saved_present: true,
  source: 'plugin_config',
  effective_source: 'runtime_startup_snapshot',
  restart_requirement: 'not_required',
  error: null,
}

function schemaPayload() {
  return {
    groups: [
      {
        key: 'WebUI_Settings', kind: 'object', description: '管理控制台', hint: '',
        items: [
          { key: 'webui_host', type: 'string', description: '监听网卡地址', hint: '', special: '', default: '127.0.0.1', saved: '0.0.0.0', effective: '0.0.0.0', value: '0.0.0.0', apply_mode: 'restart', restart_required: true, ...state },
          { key: 'webui_password', type: 'string', description: '访问登录密码', hint: '', special: '', default: '', saved: '', effective: '', value: '', apply_mode: 'next_run', restart_required: false, ...state },
        ],
      },
      {
        key: 'Query_Settings', kind: 'object', description: '记忆召回', hint: '',
        items: [
          { key: 'inject_top_k', type: 'int', description: '单次最多注入几条记忆', hint: '越多回复越长', special: '', default: 5, saved: 8, effective: 8, value: 8, apply_mode: 'next_run', restart_required: false, ...state },
        ],
      },
    ],
    warnings: [],
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}{location.search}</div>
}

function renderPage(initial = '/config') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route path="*" element={<><ConfigCenterPage /><LocationProbe /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.schema.mockResolvedValue(schemaPayload())
  api.hot.mockResolvedValue({ params: [{ key: 'spike.max_hops', type: 'int', min: 1, max: 8, default: 4, current: 4, saved: 4, effective: 4, description: '最大传播跳数' }], config: {} })
  api.inventory.mockResolvedValue({
    revision: 'inv-1',
    precedence: ['builtin', 'static', 'bot', 'override'],
    counts: {},
    items: [
      { key: 'Query_Settings.inject_top_k', layer: 'static', title: '', scope: 'global', default: 5, saved: 8, effective: 8, source: 'plugin_config', apply_mode: 'next_run', changed: true, warning: null },
      { key: 'channel:jargon.enabled', layer: 'override', title: '注入通道 jargon · enabled', scope: 'global', default: true, saved: null, effective: false, source: 'Channel_Settings', apply_mode: 'hot', changed: true, warning: null },
    ],
    suspects: [],
  })
})

function stats(overrides = {}) {
  return {
    calls: 0, succeeded: 0, failed: 0, provider_errors: 0, last_provider: '', last_success_at: null,
    last_error: '', last_error_provider: '', last_error_at: null, missing_providers: [], ...overrides,
  }
}

function task(id: string, title: string, overrides: Record<string, unknown> = {}) {
  return {
    id, title, family: 'tag', family_title: '标签模型一族', description: `${title}说明`, consumers: [], bot_overridable: false,
    reserved: false, config_key: `Model_Routing_Settings.${id}`, configured: [], invalid: null,
    chain: ['nug/tag'], primary: 'nug/tag', source: 'default', source_label: '所属默认', source_key: 'tag_llm_provider_id',
    layers: [], chain_status: [{ id: 'nug/tag', known: true }], missing: [], default_chain: ['nug/tag'],
    default_key: 'tag_llm_provider_id', default_label: '标签模型 + 回退链', available: true, active: true, active_note: '',
    apply_mode: 'hot', stats: stats(), bots: [], ...overrides,
  }
}

function routingPayload() {
  return {
    available: true,
    tasks: [
      task('jargon', '黑话挖掘', {
        configured: ['typo/missing'], chain: ['typo/missing', 'nug/tag'], primary: 'typo/missing', source: 'task', source_label: '任务配置',
        source_key: 'Model_Routing_Settings.jargon', chain_status: [{ id: 'typo/missing', known: false }, { id: 'nug/tag', known: true }],
        missing: ['typo/missing'],
        stats: stats({ calls: 4, succeeded: 3, failed: 1, provider_errors: 2, last_error: 'provider=nug/tag status=402 kind=unrecoverable: Insufficient Balance' }),
      }),
      task('self_reflect', '纠错自省', { active: false, active_note: '启动时没有可用模型，未创建；指定模型后需重启一次', apply_mode: 'restart' }),
      task('diary', '每日日记', {
        family: 'meta', bot_overridable: true, chain: ['nug/meta'], primary: 'nug/meta', source_key: 'MetaThinking_Settings.default_model',
        chain_status: [{ id: 'nug/meta', known: true }],
        bots: [{
          bot_id: 'bot_a', name: '甲', chain: ['bot/diary', 'nug/meta'], primary: 'bot/diary', source: 'bot', source_label: 'Bot 覆盖',
          source_key: 'bot:bot_a.models.diary', chain_status: [{ id: 'bot/diary', known: true }, { id: 'nug/meta', known: true }],
        }],
      }),
    ],
    families: {
      tag: {
        title: '标签模型一族', keys: ['tag_llm_provider_id', 'llm_fallback_provider_ids'], description: '启动时读取，改了需重启。',
        chain: ['nug/tag'], chain_status: [{ id: 'nug/tag', known: true }], apply_mode: 'restart',
      },
    },
    piggyback: [{ id: 'expression_learning', title: '表达学习', rides_on: 'diary' }],
    embedding: { key: 'embedding_provider_id', provider_id: 'emb/qwen', saved: 'emb/qwen', dimension: 1024, apply_mode: 'restart', note: '向量模型不在模型分工范围。' },
    providers: [{ id: 'nug/tag', model: 'gemini', type: 'openai_chat_completion' }, { id: 'nug/meta', model: 'gemini', type: 'openai_chat_completion' }],
    providers_error: null,
  }
}

function tagModelSchema() {
  return {
    groups: [{
      key: 'tag_llm_provider_id', kind: 'scalar', type: 'string', special: 'select_provider', description: '标签模型', hint: '',
      default: '', saved: 'nug/tag', effective: 'nug/tag', value: 'nug/tag', ...state, apply_mode: 'restart', restart_required: true,
    }],
    warnings: [],
  }
}

describe('ConfigCenterPage', () => {
  it('按功能分组展示当前值与来源，修改跳到内嵌系统配置并带定位参数', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText('单次最多注入几条记忆')).toBeVisible()
    expect(screen.getByText('回复时从记忆里找哪些内容、找多准、跨不跨群。')).toBeVisible()
    expect(screen.getByText('AstrBot 静态配置')).toBeVisible()
    expect(screen.getByText(/注入通道有 1 项与默认值不同/)).toBeVisible()

    await user.click(screen.getByRole('button', { name: '修改 Query_Settings.inject_top_k' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/config?view=settings&key=Query_Settings.inject_top_k&tab=static')
    expect(await screen.findByText('系统配置页桩')).toBeVisible()
  })

  it('高风险标签：0.0.0.0 且无密码标红，密码不显示明文', async () => {
    const user = userEvent.setup()
    renderPage('/config?view=risk')
    expect(await screen.findByText('2 项处于危险状态')).toBeVisible()
    expect(screen.getAllByText(/无需登录即可打开控制台/).length).toBeGreaterThan(0)
    const tab = screen.getByRole('tab', { name: /高风险/ })
    expect(within(tab).getByText('2')).toBeVisible()
    expect(screen.getAllByText('未设置').length).toBeGreaterThan(0)

    await user.click(screen.getByRole('button', { name: '去修改 WebUI_Settings.webui_host' }))
    expect(screen.getByTestId('location')).toHaveTextContent('view=settings&key=WebUI_Settings.webui_host&tab=restart')
  })

  it('顶部搜索支持症状词，结果显示所在分组', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('单次最多注入几条记忆')
    await user.type(screen.getByRole('textbox', { name: /搜索配置键/ }), '回复太长')
    expect(screen.getByText('搜索「回复太长」')).toBeVisible()
    const results = screen.getByText('搜索「回复太长」').closest('[data-slot="card"]') as HTMLElement
    expect(within(results).getByText('单次最多注入几条记忆')).toBeVisible()
    expect(within(results).getByText('记忆召回')).toBeVisible()
    await user.click(within(results).getByRole('button', { name: '清除搜索' }))
    expect(screen.queryByText('搜索「回复太长」')).not.toBeInTheDocument()
  })

  it('通道条目跳到内嵌通道配置', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('单次最多注入几条记忆')
    await user.type(screen.getByRole('textbox', { name: /搜索配置键/ }), 'jargon')
    await user.click(screen.getByRole('button', { name: '修改 channel:jargon.enabled' }))
    expect(screen.getByTestId('location')).toHaveTextContent('view=channels&channel=jargon&field=enabled')
    expect(await screen.findByText('通道配置页桩')).toBeVisible()
  })

  it('模型分工分组展示各任务生效模型、来源、找不到的 provider 与调用统计，修改沿用定位机制', async () => {
    api.schema.mockResolvedValue({
      groups: [
        ...schemaPayload().groups,
        {
          key: 'Model_Routing_Settings', kind: 'object', description: '模型分工', hint: '',
          items: [{ key: 'diary', type: 'string', description: '每日日记', hint: '', special: 'select_provider', default: '', saved: '', effective: '', value: '', ...state, apply_mode: 'service', restart_required: false }],
        },
      ],
      warnings: [],
    })
    api.routing.mockResolvedValue(routingPayload())
    const user = userEvent.setup()
    renderPage()

    const panel = await screen.findByTestId('model-routing-panel')
    expect(within(panel).getByText('当前各任务实际使用的模型')).toBeVisible()
    const jargon = within(panel).getByTestId('llm-task-jargon')
    expect(within(jargon).getByText('任务配置')).toBeVisible()
    expect(within(jargon).getByText('找不到')).toBeVisible()
    expect(within(jargon).getByText(/调用 4 次/)).toBeVisible()
    expect(within(jargon).getByText(/status=402/)).toBeVisible()
    const reflect = within(panel).getByTestId('llm-task-self_reflect')
    expect(within(reflect).getByText('需重启')).toBeVisible()
    expect(within(reflect).getByText(/指定模型后需重启一次/)).toBeVisible()
    expect(within(panel).getByText('emb/qwen')).toBeVisible()
    expect(within(panel).getByText(/表达学习没有单独的模型调用/)).toBeVisible()

    await user.click(within(panel).getByRole('button', { name: '修改 Model_Routing_Settings.jargon' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/config?view=settings&key=Model_Routing_Settings.jargon&tab=static')
  })

  it('模型分工里的 Bot 覆盖跳到 Bot 管理「行为」', async () => {
    api.schema.mockResolvedValue(tagModelSchema())
    api.routing.mockResolvedValue(routingPayload())
    const user = userEvent.setup()
    renderPage()
    const panel = await screen.findByTestId('model-routing-panel')
    expect(within(within(panel).getByTestId('llm-task-diary')).getByText('甲')).toBeVisible()
    await user.click(within(panel).getByRole('button', { name: '修改 bot:bot_a.models.diary' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/bots?bot=bot_a&tab=behavior')
  })

  it('模型分工接口失败只在分组内提示', async () => {
    api.schema.mockResolvedValue(tagModelSchema())
    api.routing.mockRejectedValue(new Error('routing 挂了'))
    renderPage()
    expect(await screen.findByText('模型分工加载失败')).toBeVisible()
    expect(screen.getByText('标签模型')).toBeVisible()
  })

  it('schema 加载失败显示错误与重试；热参数失败只降级提示', async () => {
    api.schema.mockRejectedValueOnce(new Error('schema 挂了'))
    api.hot.mockRejectedValueOnce(new Error('hot 挂了'))
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText('配置 schema 加载失败')).toBeVisible()
    expect(screen.getByText('热参数加载失败')).toBeVisible()
    await user.click(screen.getAllByRole('button', { name: /重试/ })[0])
    expect(await screen.findByText('单次最多注入几条记忆')).toBeVisible()
  })
})

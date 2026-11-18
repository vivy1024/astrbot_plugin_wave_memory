import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfigCenterPage } from '@/pages/config/ConfigCenterPage'
import type { ConfigEntry } from '@/pages/config/config-catalog'
import { ONBOARDING_COLLAPSED_KEY, OnboardingPresetsPanel } from '@/pages/config/OnboardingPresetsPanel'

const api = vi.hoisted(() => ({
  schema: vi.fn(),
  hot: vi.fn(),
  inventory: vi.fn(),
  routing: vi.fn(),
  saveFull: vi.fn(),
}))

vi.mock('@/api/config', () => ({
  getConfigSchema: api.schema,
  getHotConfig: api.hot,
  saveFullConfig: api.saveFull,
}))
vi.mock('@/api/configInventory', () => ({ getConfigInventory: api.inventory }))
vi.mock('@/api/llmRouting', () => ({ getLlmRouting: api.routing }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))
vi.mock('@/pages/settings/SettingsPage', () => ({ SettingsPage: () => <div>系统配置页桩</div> }))
vi.mock('@/pages/channels/ChannelConfigPage', () => ({ ChannelConfigPage: () => <div>通道配置页桩</div> }))
vi.mock('@/pages/settings/ConfigInventoryPage', () => ({ ConfigInventoryPage: () => <div>配置来源页桩</div> }))

function makeEntry(key: string, value: unknown, overrides: Partial<ConfigEntry> = {}): ConfigEntry {
  const dot = key.indexOf('.')
  return {
    key,
    kind: key.startsWith('hot:') ? 'hot' : 'static',
    section: dot > 0 ? key.slice(0, dot) : key,
    title: overrides.title ?? key,
    hint: '',
    type: typeof value === 'boolean' ? 'bool' : typeof value === 'number' ? 'int' : 'string',
    defaultValue: value,
    savedValue: value,
    effectiveValue: value,
    applyMode: 'next_run',
    restartRequired: false,
    layer: 'static',
    source: 'plugin_config',
    changed: false,
    error: null,
    ...overrides,
  }
}

const itemState = {
  saved_present: true,
  source: 'plugin_config',
  effective_source: 'runtime_startup_snapshot',
  restart_requirement: 'not_required' as const,
  error: null,
}

function fullSchemaPayload() {
  return {
    groups: [
      {
        key: 'embedding_provider_id',
        kind: 'scalar' as const,
        type: 'string',
        description: '向量特征模型 (Embedding) 提供商',
        hint: '',
        default: '',
        saved: 'siliconflow/qwen-emb',
        effective: 'siliconflow/qwen-emb',
        value: 'siliconflow/qwen-emb',
        apply_mode: 'restart' as const,
        restart_required: true,
        ...itemState,
      },
      {
        key: 'tag_llm_provider_id',
        kind: 'scalar' as const,
        type: 'string',
        description: '自省标签提取大模型 (Tag LLM)',
        hint: '',
        default: '',
        saved: 'nug/gemini-flash',
        effective: 'nug/gemini-flash',
        value: 'nug/gemini-flash',
        apply_mode: 'restart' as const,
        restart_required: true,
        ...itemState,
      },
      {
        key: 'Query_Settings',
        kind: 'object' as const,
        description: '记忆召回',
        hint: '',
        items: [
          {
            key: 'enable_auto_inject',
            type: 'bool',
            description: '开启自动记忆注入能力',
            hint: '',
            special: '',
            default: true,
            saved: true,
            effective: true,
            value: true,
            apply_mode: 'next_run' as const,
            restart_required: false,
            ...itemState,
          },
          {
            key: 'inject_top_k',
            type: 'int',
            description: '单次最多注入几条记忆',
            hint: '',
            special: '',
            default: 5,
            saved: 5,
            effective: 5,
            value: 5,
            apply_mode: 'next_run' as const,
            restart_required: false,
            ...itemState,
          },
        ],
      },
      {
        key: 'Learning_Settings',
        kind: 'object' as const,
        description: '记账与学习',
        hint: '',
        items: [
          {
            key: 'daily_diary_enabled',
            type: 'bool',
            description: '每日日记',
            hint: '',
            special: '',
            default: true,
            saved: true,
            effective: true,
            value: true,
            apply_mode: 'next_run' as const,
            restart_required: false,
            ...itemState,
          },
        ],
      },
      {
        key: 'WebUI_Settings',
        kind: 'object' as const,
        description: '管理控制台',
        hint: '',
        items: [
          {
            key: 'webui_host',
            type: 'string',
            description: '监听网卡地址',
            hint: '',
            special: '',
            default: '127.0.0.1',
            saved: '127.0.0.1',
            effective: '127.0.0.1',
            value: '127.0.0.1',
            apply_mode: 'restart' as const,
            restart_required: true,
            ...itemState,
          },
        ],
      },
      {
        key: 'Memory_Index_Settings',
        kind: 'object' as const,
        description: '记忆检索索引',
        hint: '',
        items: [
          {
            key: 'hot_max_vectors',
            type: 'int',
            description: '热记忆最大条数',
            hint: '',
            special: '',
            default: 40000,
            saved: 40000,
            effective: 40000,
            value: 40000,
            apply_mode: 'restart' as const,
            restart_required: true,
            ...itemState,
          },
        ],
      },
      {
        key: 'Memory_Budget_Settings',
        kind: 'object' as const,
        description: '内存占用预算',
        hint: '',
        items: [
          {
            key: 'memory_budget_mb',
            type: 'int',
            description: '常驻内存预算（MiB）',
            hint: '',
            special: '',
            default: 2400,
            saved: 2400,
            effective: 2400,
            value: 2400,
            apply_mode: 'restart' as const,
            restart_required: true,
            ...itemState,
          },
        ],
      },
      {
        key: 'Performance_Settings',
        kind: 'object' as const,
        description: '底层性能与批处理调优',
        hint: '',
        items: [
          {
            key: 'embedding_batch_size',
            type: 'int',
            description: '向量化批处理大小',
            hint: '',
            special: '',
            default: 16,
            saved: 16,
            effective: 16,
            value: 16,
            apply_mode: 'next_run' as const,
            restart_required: false,
            ...itemState,
          },
        ],
      },
      {
        key: 'Trace_Settings',
        kind: 'object' as const,
        description: '观测数据留存',
        hint: '',
        items: [
          {
            key: 'max_rows',
            type: 'int',
            description: 'Trace 最大保留记录条数',
            hint: '',
            special: '',
            default: 5000,
            saved: 5000,
            effective: 5000,
            value: 5000,
            apply_mode: 'restart' as const,
            restart_required: true,
            ...itemState,
          },
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

beforeEach(() => {
  // Node 25 自带的 localStorage 在无路径时会遮蔽 jsdom 实现，这里注入内存 Storage
  const values = new Map<string, string>()
  const storage: Storage = {
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => Array.from(values.keys())[index] ?? null,
    get length() {
      return values.size
    },
    removeItem: (key: string) => {
      values.delete(key)
    },
    setItem: (key: string, value: string) => {
      values.set(key, String(value))
    },
  }
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })

  api.schema.mockReset()
  api.hot.mockReset()
  api.inventory.mockReset()
  api.routing.mockReset()
  api.saveFull.mockReset()

  api.schema.mockResolvedValue(fullSchemaPayload())
  api.hot.mockResolvedValue({
    params: [
      { key: 'spike.max_hops', type: 'int', min: 1, max: 8, default: 4, current: 4, saved: 4, effective: 4, description: '最大传播跳数' },
      { key: 'geodesic.alpha', type: 'float', min: 0, max: 1, default: 0.5, current: 0.5, saved: 0.5, effective: 0.5, description: '测地线拉回系数' },
      { key: 'residual.depth', type: 'int', min: 1, max: 4, default: 2, current: 2, saved: 2, effective: 2, description: '残差金字塔阶数' },
    ],
    config: {},
  })
  api.inventory.mockResolvedValue({
    revision: 'inv-1',
    precedence: ['builtin', 'static', 'bot', 'override'],
    counts: {},
    items: [],
    suspects: [],
  })
  api.routing.mockResolvedValue({
    available: true,
    tasks: [],
    families: {},
    piggyback: [],
    embedding: { key: 'embedding_provider_id', provider_id: 'siliconflow/qwen-emb', saved: 'siliconflow/qwen-emb', dimension: 1024, apply_mode: 'restart', note: '' },
    providers: [],
    providers_error: null,
  })
  api.saveFull.mockResolvedValue({ ok: true, saved: true, changed: [], message: '配置已保存。' })
})

describe('OnboardingPresetsPanel', () => {
  it('展示 6185 防覆盖说明与 3 步就绪状态：未配向量模型显示待完成并可一键定位，配齐后显示已就绪', async () => {
    const user = userEvent.setup()
    const onLocate = vi.fn()
    const onApplied = vi.fn()

    const unconfiguredEntries = [
      makeEntry('embedding_provider_id', '', { applyMode: 'restart', restartRequired: true }),
      makeEntry('tag_llm_provider_id', '', { applyMode: 'restart', restartRequired: true }),
    ]

    const { rerender } = render(
      <OnboardingPresetsPanel entries={unconfiguredEntries} onLocate={onLocate} onApplied={onApplied} />,
    )

    // 6185 提示条
    expect(
      screen.getByText(/建议统一在当前 9876 控制台管理系统与 Bot 配置——AstrBot 自带插件表单（6185）仅用于填写初始端口与基础模型，保存旧表单可能覆盖新增开关。/),
    ).toBeVisible()

    // 第 1 步未配齐：显示待完成与一键定位
    const step1 = screen.getByTestId('onboarding-step-models')
    expect(within(step1).getByText('待完成')).toBeVisible()
    expect(within(step1).getByText(/未配置 embedding_provider_id/)).toBeVisible()

    await user.click(within(step1).getByRole('button', { name: '去配置向量模型' }))
    expect(onLocate).toHaveBeenCalledWith(
      expect.objectContaining({
        target: 'settings',
        params: { key: 'embedding_provider_id', tab: 'restart' },
      }),
    )

    // 第 2 步跳 Bot 管理
    const step2 = screen.getByTestId('onboarding-step-bots')
    await user.click(within(step2).getByRole('button', { name: '去管理 Bot 身份' }))
    expect(onLocate).toHaveBeenCalledWith(
      expect.objectContaining({ target: 'bots', pathname: '/bots' }),
    )

    // 配齐向量模型与大模型后实时亮灯为「已就绪」
    const readyEntries = [
      makeEntry('embedding_provider_id', 'siliconflow/Qwen3-Embedding', { applyMode: 'restart', restartRequired: true }),
      makeEntry('MetaThinking_Settings.default_model', 'nug/gemini-flash', { applyMode: 'restart', restartRequired: true }),
    ]
    rerender(<OnboardingPresetsPanel entries={readyEntries} onLocate={onLocate} onApplied={onApplied} />)

    expect(within(screen.getByTestId('onboarding-step-models')).getByText('已就绪')).toBeVisible()
    expect(within(screen.getByTestId('onboarding-step-models')).getByText('siliconflow/Qwen3-Embedding')).toBeVisible()
    expect(within(screen.getByTestId('onboarding-step-models')).getByText('nug/gemini-flash')).toBeVisible()
  })

  it('点击三大场景预设并确认后，真实调用 saveFullConfig 提交对应分组载荷并触发 onApplied 刷新', async () => {
    const user = userEvent.setup()
    const onLocate = vi.fn()
    const onApplied = vi.fn()
    const entries = [
      makeEntry('Query_Settings.enable_auto_inject', false, { title: '开启自动记忆注入能力' }),
      makeEntry('Learning_Settings.daily_diary_enabled', true, { title: '每日日记' }),
    ]

    render(<OnboardingPresetsPanel entries={entries} onLocate={onLocate} onApplied={onApplied} />)

    // 1. 预设 A · 轻量纯记忆检索
    await user.click(screen.getByRole('button', { name: '应用 预设 A · 轻量纯记忆检索' }))
    const dialogA = await screen.findByRole('dialog')
    expect(within(dialogA).getByRole('list', { name: '预设变更预览' })).toHaveTextContent('Query_Settings.enable_auto_inject')
    expect(api.saveFull).not.toHaveBeenCalled()

    await user.click(within(dialogA).getByRole('button', { name: '确认应用' }))
    expect(api.saveFull).toHaveBeenCalledWith({
      Query_Settings: {
        enable_auto_inject: true,
      },
      Learning_Settings: {
        daily_diary_enabled: false,
        belief_reflection_enabled: false,
        relationship_portrait_enabled: false,
      },
    })
    expect(onApplied).toHaveBeenCalledTimes(1)

    // 2. 预设 B · 拟人群聊伴侣
    await user.click(screen.getByRole('button', { name: '应用 预设 B · 拟人群聊伴侣' }))
    const dialogB = await screen.findByRole('dialog')
    await user.click(within(dialogB).getByRole('button', { name: '确认应用' }))
    expect(api.saveFull).toHaveBeenLastCalledWith({
      Query_Settings: {
        enable_auto_inject: true,
      },
      Tag_Settings: {
        tag_extraction_enabled: true,
      },
      Learning_Settings: {
        post_reply_bookkeeping_enabled: true,
        daily_diary_enabled: true,
        expression_auto_approve_enabled: true,
        auto_approve_enabled: true,
      },
    })
    expect(onApplied).toHaveBeenCalledTimes(2)

    // 3. 预设 C · 跨端数字生命全开（含 Bot 管理主动对话与激素身体提示）
    expect(screen.getByTestId('preset-card-digital_life')).toHaveTextContent(/主动对话.*激素身体/)
    await user.click(screen.getByRole('button', { name: '应用 预设 C · 跨端数字生命全开' }))
    const dialogC = await screen.findByRole('dialog')
    expect(within(dialogC).getByText(/前往「Bot 管理」按角色单独开启「主动对话」与「激素身体」/)).toBeVisible()
    await user.click(within(dialogC).getByRole('button', { name: '确认应用' }))
    expect(api.saveFull).toHaveBeenLastCalledWith({
      Query_Settings: {
        enable_auto_inject: true,
      },
      Tag_Settings: {
        tag_extraction_enabled: true,
      },
      Learning_Settings: {
        post_reply_bookkeeping_enabled: true,
        daily_diary_enabled: true,
        expression_auto_approve_enabled: true,
        auto_approve_enabled: true,
        belief_reflection_enabled: true,
        relationship_portrait_enabled: true,
        reply_feedback_enabled: true,
        expression_learning_enabled: true,
      },
    })
    expect(onApplied).toHaveBeenCalledTimes(3)
  })

  it('支持折叠与展开向导面板，并将状态持久化到 localStorage', async () => {
    const user = userEvent.setup()
    const { unmount } = render(
      <OnboardingPresetsPanel entries={[]} onLocate={vi.fn()} onApplied={vi.fn()} />,
    )

    expect(screen.getByText('统一在 9876 控制台管理配置')).toBeVisible()

    // 收起面板
    await user.click(screen.getByRole('button', { name: '收起新手向导与场景预设' }))
    expect(window.localStorage.getItem(ONBOARDING_COLLAPSED_KEY)).toBe('true')
    expect(screen.queryByText('统一在 9876 控制台管理配置')).not.toBeInTheDocument()

    // 重新挂载仍记住收起状态
    unmount()
    render(<OnboardingPresetsPanel entries={[]} onLocate={vi.fn()} onApplied={vi.fn()} />)
    expect(screen.queryByText('统一在 9876 控制台管理配置')).not.toBeInTheDocument()

    // 再次展开并写回 localStorage
    await user.click(screen.getByRole('button', { name: '展开新手向导与场景预设' }))
    expect(window.localStorage.getItem(ONBOARDING_COLLAPSED_KEY)).toBe('false')
    expect(screen.getByText('统一在 9876 控制台管理配置')).toBeVisible()
  })

  it('在 ConfigCenterPage 中切换「新手精简视图」隐藏底层内核参数并保留核心项，也可一键恢复全部专家参数', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/config']}>
        <Routes>
          <Route path="*" element={<><ConfigCenterPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>,
    )

    // 默认显示全部：底层热参数在已展开的「记忆召回」里立即可见
    expect(await screen.findByText('最大传播跳数')).toBeVisible()
    expect(screen.getByText('测地线拉回系数')).toBeVisible()
    expect(screen.getByText('残差金字塔阶数')).toBeVisible()
    expect(screen.getByText('单次最多注入几条记忆')).toBeVisible()

    // 展开默认折叠的「性能与索引」「维护与备份」，确认专家项默认也在
    await user.click(screen.getByRole('button', { name: '展开 性能与索引' }))
    await user.click(screen.getByRole('button', { name: '展开 维护与备份' }))
    expect(screen.getByText('热记忆最大条数')).toBeVisible()
    expect(screen.getByText('常驻内存预算（MiB）')).toBeVisible()
    expect(screen.getByText('向量化批处理大小')).toBeVisible()
    expect(screen.getByText('Trace 最大保留记录条数')).toBeVisible()

    // 开启「新手精简视图」
    await user.click(screen.getByRole('switch', { name: '新手精简视图' }))

    // 底层高阶内核项全部隐藏
    expect(screen.queryByText('最大传播跳数')).not.toBeInTheDocument()
    expect(screen.queryByText('测地线拉回系数')).not.toBeInTheDocument()
    expect(screen.queryByText('残差金字塔阶数')).not.toBeInTheDocument()
    expect(screen.queryByText('热记忆最大条数')).not.toBeInTheDocument()
    expect(screen.queryByText('常驻内存预算（MiB）')).not.toBeInTheDocument()
    expect(screen.queryByText('向量化批处理大小')).not.toBeInTheDocument()
    expect(screen.queryByText('Trace 最大保留记录条数')).not.toBeInTheDocument()

    // 核心模型、记忆召回、学习开关与 WebUI 安全项保留可见
    expect(screen.getByText('向量特征模型 (Embedding) 提供商')).toBeVisible()
    expect(screen.getByText('自省标签提取大模型 (Tag LLM)')).toBeVisible()
    expect(screen.getByText('单次最多注入几条记忆')).toBeVisible()
    expect(screen.getByText('每日日记')).toBeVisible()
    expect(screen.getByText('监听网卡地址')).toBeVisible()

    // 点击「显示全部专家参数」恢复
    await user.click(screen.getByRole('button', { name: '显示全部专家参数' }))
    expect(screen.getByText('最大传播跳数')).toBeVisible()
    expect(screen.getByText('热记忆最大条数')).toBeVisible()
  })
})

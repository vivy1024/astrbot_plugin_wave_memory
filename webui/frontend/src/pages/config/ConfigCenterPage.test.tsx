import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfigCenterPage } from '@/pages/config/ConfigCenterPage'

const api = vi.hoisted(() => ({ schema: vi.fn(), hot: vi.fn(), inventory: vi.fn() }))

vi.mock('@/api/config', () => ({ getConfigSchema: api.schema, getHotConfig: api.hot }))
vi.mock('@/api/configInventory', () => ({ getConfigInventory: api.inventory }))
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

import { act, configure, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GRAPH_CHROME } from '@/lib/graph-palette'
import { kgFixture, NOW, tagFixture } from '@/lib/graph/fixtures'
import { FakeSigma, resetFakes } from '@/test/fake-sigma'
import { setViewport } from '@/test/setup'
import { GraphPage } from './GraphPage'

vi.mock('sigma', async () => (await import('@/test/fake-sigma')).sigmaModule)
vi.mock('graphology-layout-forceatlas2/worker', async () => (await import('@/test/fake-sigma')).workerModule)

const api = vi.hoisted(() => ({
  getKgFull: vi.fn(),
  getKgEntity: vi.fn(),
  getKgEntityTimeline: vi.fn(),
  findKgPath: vi.fn(),
  getTagGraph: vi.fn(),
  findTagGraphPath: vi.fn(),
  updateTagCommand: vi.fn(),
}))

vi.mock('@/api/kg', () => ({
  getKgFull: api.getKgFull,
  getKgEntity: api.getKgEntity,
  getKgEntityTimeline: api.getKgEntityTimeline,
  findKgPath: api.findKgPath,
}))
vi.mock('@/api/tagGraph', () => ({
  getTagGraph: api.getTagGraph,
  findTagGraphPath: api.findTagGraphPath,
  updateTagCommand: api.updateTagCommand,
}))

vi.mock('./showcase/GraphShowcase', () => ({
  GraphShowcase: (props: { hours: number; onOpenIn2D(id: string): void; onHoursChange(hours: number): void }) => (
    <div data-testid="showcase">
      <span data-testid="showcase-hours">{props.hours}</span>
      <button type="button" onClick={() => props.onHoursChange(72)}>三天</button>
      <button type="button" onClick={() => props.onOpenIn2D('tag:7')}>去平面</button>
    </div>
  ),
}))

// 整套 vitest 并行跑时页面首屏（取数 + 建图 + 布局）可能超过默认 1 秒
configure({ asyncUtilTimeout: 5000 })

const SCOPE = 'bot_id=yushu&session_id=%E7%BE%BD%E4%B9%A6%3Agroup%3A42&visibility=group'

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.search}</output>
}

function renderPage(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/graph?${SCOPE}${search ? `&${search}` : ''}`]}>
      <GraphPage />
      <LocationProbe />
    </MemoryRouter>,
  )
}

function currentParams() {
  return new URLSearchParams(screen.getByTestId('location').textContent ?? '')
}

function sigma() {
  return FakeSigma.instances.at(-1)!
}

beforeEach(() => {
  resetFakes()
  setViewport(1440)
  vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
  api.getKgFull.mockResolvedValue({ ...kgFixture(), generated_at: NOW })
  api.getKgEntity.mockResolvedValue({ name: '测试群友甲', person: { name: '测试群友甲', msg_count: 2 }, facts: [], relations: [], memories: [{ id: 700, content: '我用喷油壶喷过油了', sender: '测试群友甲', ts: NOW - 3600 }], read_only: true })
  api.getKgEntityTimeline.mockResolvedValue({ name: '测试群友甲', events: [{ type: 'fact', ts: NOW - 7200, subject: '测试群友甲', predicate: '喜欢', object: '烤洋芋' }], read_only: true })
  api.getTagGraph.mockResolvedValue(tagFixture())
})

afterEach(() => {
  document.documentElement.classList.remove('dark')
})

describe('GraphPage', () => {
  it('默认知识图谱「最强关系」：按当前群取数，顶部一句话说明，图例可按类型过滤', async () => {
    const user = userEvent.setup()
    renderPage('')
    expect(await screen.findByTestId('graph-sentence')).toHaveTextContent('人物/实体之间最强的')
    expect(api.getKgFull).toHaveBeenCalledWith({ bot_id: 'yushu', session_id: '羽书:group:42', visibility: 'group' }, expect.anything())
    expect(screen.getByRole('radio', { name: '知识图谱' })).toHaveAttribute('aria-checked', 'true')
    expect(FakeSigma.instances).toHaveLength(1)

    const legend = screen.getByLabelText('按类型过滤')
    await user.click(within(legend).getByRole('button', { name: /实体/ }))
    expect(currentParams().get('hide_types')).toBe('entity')
    expect(sigma().graph.hasNode('entity:空气炸锅')).toBe(false)
    // 过滤不重建渲染器
    expect(FakeSigma.instances).toHaveLength(1)
  })

  it('切到「新出现」「当前话题」「全部」时，一句话随视图变化', async () => {
    const user = userEvent.setup()
    renderPage('')
    await screen.findByTestId('graph-sentence')
    await user.click(screen.getByRole('tab', { name: '新出现' }))
    expect(currentParams().get('view')).toBe('new')
    expect(screen.getByTestId('graph-sentence')).toHaveTextContent('最近 7 天新出现的 1 条关系')
    expect(screen.getByLabelText('时间窗口')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: '当前话题' }))
    expect(screen.getByTestId('graph-sentence')).toHaveTextContent('最近 7 天最活跃的 3 个人物/实体')
    await user.click(screen.getByRole('tab', { name: '全部' }))
    expect(screen.getByTestId('graph-sentence')).toHaveTextContent('按重要度排出的前 5 个人物/实体')
  })

  it('点击节点打开详情：关系证据、原话与来源记忆链接', async () => {
    renderPage('')
    await screen.findByTestId('graph-sentence')
    act(() => sigma().emit('clickNode', { node: 'entity:测试群友甲' }))
    expect(currentParams().get('node')).toBe('entity:测试群友甲')
    const detail = await screen.findByRole('complementary', { name: '节点详情' })
    expect(within(detail).getByText('证据：说自己用空气炸锅烤土豆')).toBeVisible()
    expect(within(detail).getByText('我用喷油壶喷过油了', { selector: 'blockquote' })).toBeVisible()
    const link = within(detail).getByRole('link', { name: /来源记忆 #656540/ })
    const href = new URLSearchParams(link.getAttribute('href')!.split('?')[1])
    expect(href.get('memory_id')).toBe('656540')
    expect(href.get('session_id')).toBe('羽书:group:42')
    await waitFor(() => expect(api.getKgEntity).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'yushu' }), '测试群友甲', expect.anything()))
    expect(await within(detail).findByText('我用喷油壶喷过油了', { selector: 'p' })).toBeVisible()
    expect(within(detail).getByRole('link', { name: '在事实页管理' }).getAttribute('href')).toContain('search=')
  })

  it('两点找路径（知识图谱走 POST /api/kg/path），路径高亮', async () => {
    const user = userEvent.setup()
    api.findKgPath.mockResolvedValue({ path: ['entity:空气炸锅', 'entity:羽书', 'entity:测试群友甲'], edges: [], nodes: [], read_only: true })
    renderPage('path_from=entity%3A%E7%A9%BA%E6%B0%94%E7%82%B8%E9%94%85&path_to=entity%3A%E6%B5%8B%E8%AF%95%E7%BE%A4%E5%8F%8B%E7%94%B2')
    await screen.findByTestId('graph-sentence')
    await user.click(screen.getByRole('button', { name: '找路径' }))
    expect(api.findKgPath).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'yushu' }), { from: 'entity:空气炸锅', to: 'entity:测试群友甲', max_depth: 6 })
    expect(await screen.findByText('找到 2 步：空气炸锅 → 羽书 → 测试群友甲')).toBeVisible()
    expect(sigma().edge('tagrel:4').color).toBe(GRAPH_CHROME.light.path)
    await user.click(screen.getByRole('button', { name: '清除' }))
    expect(currentParams().get('path_from')).toBeNull()
  })

  it('标签路径：服务端没找到时在已取回的边上找，并标注来源', async () => {
    const user = userEvent.setup()
    api.findTagGraphPath.mockResolvedValue({ found: false, path: [], nodes: [], edges: [], layers: [], scope: {}, read_only: true })
    renderPage('layer=tags&path_from=tag%3A1&path_to=tag%3A2')
    await screen.findByTestId('graph-sentence')
    await user.click(screen.getByRole('button', { name: '找路径' }))
    expect(api.findTagGraphPath).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ source_ref: 'oref.tag-1', target_ref: 'oref.tag-2', max_depth: 8 }))
    expect(await screen.findByText(/找到 2 步：政治人物信息 → 羽书 → 空气炸锅/)).toBeVisible()
    expect(screen.getByText(/服务端完整图未找到/)).toBeVisible()
  })

  it('搜索节点并聚焦；不在当前视图的节点会被钉进视图', async () => {
    const user = userEvent.setup()
    renderPage('view=new&window=24')
    await screen.findByTestId('graph-sentence')
    await user.type(screen.getByLabelText('搜索节点'), '空气')
    const option = await screen.findByRole('option', { name: /空气炸锅/ })
    expect(option).toHaveTextContent('不在当前视图')
    await user.click(within(option).getByRole('button'))
    expect(currentParams().get('node')).toBe('entity:空气炸锅')
    await waitFor(() => expect(sigma().graph.hasNode('entity:空气炸锅')).toBe(true))
  })

  it('标签层：按视图换后端排序（links / recent / created），旧后端缺字段时如实说明', async () => {
    const user = userEvent.setup()
    renderPage('layer=tags')
    await screen.findByTestId('graph-sentence')
    expect(api.getTagGraph).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ rankBy: 'links', maxNodes: 300, layers: ['cooccurrence', 'relations'] }))
    await user.click(screen.getByRole('tab', { name: '当前话题' }))
    await waitFor(() => expect(api.getTagGraph).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ rankBy: 'recent', recentWindowHours: 168 })))
    expect(await screen.findByText(/最近 7 天最活跃的 3 个标签/)).toBeVisible()

    api.getTagGraph.mockResolvedValue(tagFixture(false))
    await user.click(screen.getByRole('tab', { name: '新出现' }))
    await waitFor(() => expect(api.getTagGraph).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ rankBy: 'created' })))
    expect(await screen.findByText(/需要升级 WaveMemory 后端/)).toBeVisible()
  })

  it('标签详情保留编辑入口；旧 /tags/graph 的 ref 深链会换成节点 id', async () => {
    renderPage('layer=tags&ref=oref.tag-3')
    const detail = await screen.findByRole('complementary', { name: '节点详情' })
    expect(currentParams().get('node')).toBe('tag:3')
    expect(currentParams().get('ref')).toBeNull()
    expect(within(detail).getByRole('heading', { name: '烤洋芋' })).toBeVisible()
    expect(within(detail).getByRole('button', { name: /编辑/ })).toBeVisible()
  })

  it('切换数据层清空节点、路径与类型过滤', async () => {
    const user = userEvent.setup()
    renderPage('node=entity%3A%E7%BE%BD%E4%B9%A6&hide_types=topic&path_from=entity%3A%E7%BE%BD%E4%B9%A6')
    await screen.findByTestId('graph-sentence')
    await user.click(screen.getByRole('radio', { name: '标签' }))
    const params = currentParams()
    expect(params.get('layer')).toBe('tags')
    expect(params.get('node')).toBeNull()
    expect(params.get('hide_types')).toBeNull()
    expect(params.get('path_from')).toBeNull()
    expect(params.get('bot_id')).toBe('yushu')
  })

  it('窄屏把详情放进 Sheet', async () => {
    setViewport(390)
    renderPage('node=entity%3A%E7%BE%BD%E4%B9%A6')
    await screen.findByTestId('graph-sentence')
    const sheet = await screen.findByRole('dialog')
    expect(sheet).toHaveAttribute('data-graph-sheet')
    expect(within(sheet).getByRole('heading', { name: '羽书' })).toBeVisible()
    expect(screen.queryByRole('complementary', { name: '节点详情' })).not.toBeInTheDocument()
  })

  it('颜色跟随 html.dark 实时切换', async () => {
    renderPage('')
    await screen.findByTestId('graph-sentence')
    act(() => document.documentElement.classList.add('dark'))
    await waitFor(() => expect(sigma().setSetting).toHaveBeenCalledWith('labelColor', { color: GRAPH_CHROME.dark.label }))
  })

  it('没有群作用域时不取数', () => {
    render(<MemoryRouter initialEntries={['/graph']}><GraphPage /></MemoryRouter>)
    expect(screen.getByText('请先在顶栏选择 Bot 和群。')).toBeVisible()
    expect(api.getKgFull).not.toHaveBeenCalled()
  })

  it('3D 展示：切换写入 URL、不再取平面图数据；从节点卡片回到平面图的标签层并选中节点', async () => {
    const user = userEvent.setup()
    renderPage('layer=kg&node=person%3A1')
    await waitFor(() => expect(api.getKgFull).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('radio', { name: '3D 展示' }))
    expect(await screen.findByTestId('showcase')).toBeInTheDocument()
    expect(currentParams().get('mode')).toBe('3d')
    expect(screen.queryByRole('radiogroup', { name: '数据层' })).toBeNull()
    expect(screen.getByTestId('showcase-hours').textContent).toBe('24')
    await user.click(screen.getByRole('button', { name: '三天' }))
    expect(currentParams().get('replay_hours')).toBe('72')
    expect(api.getKgFull).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '去平面' }))
    const params = currentParams()
    expect(params.get('mode')).toBeNull()
    expect(params.get('replay_hours')).toBeNull()
    expect(params.get('layer')).toBe('tags')
    expect(params.get('node')).toBe('tag:7')
    await waitFor(() => expect(api.getTagGraph).toHaveBeenCalled())
  })
})

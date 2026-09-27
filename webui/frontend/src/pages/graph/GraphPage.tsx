import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  CrosshairIcon,
  LoaderCircleIcon,
  MinusIcon,
  NetworkIcon,
  PlusIcon,
  RefreshCwIcon,
  RouteIcon,
  SearchIcon,
  ShuffleIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  XIcon,
} from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { findKgPath, getKgFull, type KgGraphEdge, type KgGraphNode } from '@/api/kg'
import { findTagGraphPath, getTagGraph, type TagGraphEdge, type TagGraphNode, type TagGraphRankBy } from '@/api/tagGraph'
import { useGlobalScope } from '@/app/global-scope'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useIsMobile } from '@/hooks/use-mobile'
import { buildLegendItems } from '@/lib/graph-palette'
import { kgPayloadToGraph } from '@/lib/graph/kg-adapter'
import type { LayoutStatus } from '@/lib/graph/layout-controller'
import { findLocalPath } from '@/lib/graph/path'
import { mergeTagPath, tagPayloadToGraph } from '@/lib/graph/tag-adapter'
import type { GraphBundle, GraphLayerKind, GraphViewKind } from '@/lib/graph/types'
import { buildView, formatWindow, GRAPH_VIEWS, pairKey, WINDOW_OPTIONS_HOURS } from '@/lib/graph/views'
import { humanizeApiError } from '@/lib/reason-label'
import { cn } from '@/lib/utils'
import { GraphCanvas, type GraphCanvasHandle } from './GraphCanvas'
import { KgNodeDetail } from './KgNodeDetail'
import { TagNodeDetail } from './TagNodeDetail'
import { LAYER_DEPENDENT_KEYS, MAX_NODE_OPTIONS, MIN_WEIGHT_OPTIONS, parseGraphUrl } from './graph-url'
import { useGraphTheme, useWideLayout } from './use-graph-theme'

type KgBundle = GraphBundle<KgGraphNode, KgGraphEdge>
type TagBundle = GraphBundle<TagGraphNode, TagGraphEdge>
type AnyBundle = KgBundle | TagBundle

interface PathState {
  from: string
  to: string
  loading: boolean
  found: boolean
  nodes: string[]
  /** 完整图里的边 key（强制加入视图）。 */
  edges: string[]
  message?: string
}

const LAYERS: ReadonlyArray<{ id: GraphLayerKind; label: string; hint: string }> = [
  { id: 'kg', label: '知识图谱', hint: '人物、实体与事实' },
  { id: 'tags', label: '标签', hint: '标签共现与显式关系' },
]

const STOP_REASON: Record<string, string> = {
  converged: '布局已稳定',
  'max-iterations': '已到迭代上限',
  timeout: '已到时间上限',
  stopped: '布局已停止',
  empty: '无需布局',
}

function rankByFor(view: GraphViewKind): TagGraphRankBy {
  return view === 'new' ? 'created' : view === 'current' ? 'recent' : 'links'
}

/** 同一对节点之间（任意方向）权重最高的边 key。 */
function strongestEdgeBetween(bundle: AnyBundle, a: string, b: string): string | null {
  const graph = bundle.graph
  if (!graph.hasNode(a) || !graph.hasNode(b)) return null
  let best: string | null = null
  let weight = -Infinity
  for (const key of [...graph.edges(a, b), ...graph.edges(b, a)]) {
    const value = graph.getEdgeAttribute(key, 'weight')
    if (value > weight) { weight = value; best = key }
  }
  return best
}

export function GraphPage() {
  const [params, setParams] = useSearchParams()
  const isMobile = useIsMobile()
  const wide = useWideLayout()
  const theme = useGraphTheme()
  const { botId, sessionId, status: scopeStatus } = useGlobalScope()
  const url = parseGraphUrl(params, isMobile)
  const scope = useMemo(() => (botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' as const } : null), [botId, sessionId])
  const canvasRef = useRef<GraphCanvasHandle>(null)
  const cacheRef = useRef(new Map<string, AnyBundle>())
  const pendingFocus = useRef<string | null>(null)
  const [bundle, setBundle] = useState<AnyBundle | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>()
  const [reload, setReload] = useState(0)
  const [pinned, setPinned] = useState<string[]>([])
  const [path, setPath] = useState<PathState | null>(null)
  const [layout, setLayout] = useState<LayoutStatus | null>(null)
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [filtersOpen, setFiltersOpen] = useState(false)

  const setQueryParams = useCallback((changes: Record<string, string | null>, replace = true) => {
    setParams((current) => {
      const next = new URLSearchParams(current)
      for (const [key, value] of Object.entries(changes)) {
        if (value === null || value === '') next.delete(key)
        else next.set(key, value)
      }
      return next
    }, { replace })
  }, [setParams])

  // ---- 数据：按数据层取数，同一参数组合缓存在内存里，切回来不重新请求 ----
  const rankBy = url.layer === 'tags' ? rankByFor(url.view) : null
  const fetchKey = scope
    ? url.layer === 'kg'
      ? `kg|${scope.bot_id}|${scope.session_id}`
      : `tags|${scope.bot_id}|${scope.session_id}|${url.maxNodes}|${rankBy}|${rankBy === 'links' ? '' : url.windowHours}`
    : ''

  useEffect(() => {
    if (!scope || !fetchKey) { setBundle(null); setError(undefined); setLoading(false); return }
    const cached = reload === 0 ? cacheRef.current.get(fetchKey) : undefined
    if (cached) { setBundle(cached); setError(undefined); setLoading(false); return }
    const controller = new AbortController()
    // 不在新数据到达前继续展示上一个数据层 / 排序的图，避免一句话与图对不上
    setBundle(null)
    setLoading(true)
    setError(undefined)
    const request: Promise<AnyBundle> = url.layer === 'kg'
      ? getKgFull(scope, { signal: controller.signal }).then((payload) => kgPayloadToGraph(payload, theme))
      : getTagGraph(scope, {
        layers: ['cooccurrence', 'relations'],
        maxNodes: url.maxNodes,
        rankBy: rankBy ?? 'links',
        recentWindowHours: url.windowHours,
        signal: controller.signal,
      }).then((payload) => tagPayloadToGraph(payload, theme))
    request.then((next) => {
      if (controller.signal.aborted) return
      cacheRef.current.set(fetchKey, next)
      setBundle(next)
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || isRequestCancelled(reason)) return
      setBundle(null)
      setError(reason)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
    // theme 只影响 adapter 给的初始颜色，reducer 会按当前主题重新上色，不必因此重新取数
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey, reload])

  // 换数据源时丢掉旧的聚焦与路径
  useEffect(() => { setPinned([]); setPath(null) }, [bundle?.layer, fetchKey])

  // 旧 /tags/graph 深链：ref / source_ref / target_ref 是标签 ObjectRef，换成节点 id
  useEffect(() => {
    if (!bundle || bundle.layer !== 'tags' || !(url.legacyRef || url.legacySourceRef || url.legacyTargetRef)) return
    const byRef = (ref: string | null) => {
      if (!ref) return null
      for (const [id, raw] of (bundle as TagBundle).rawNodes) if (raw.ref === ref) return id
      return null
    }
    setQueryParams({
      ref: null, source_ref: null, target_ref: null,
      node: byRef(url.legacyRef) ?? url.node,
      path_from: byRef(url.legacySourceRef) ?? url.pathFrom,
      path_to: byRef(url.legacyTargetRef) ?? url.pathTo,
    })
  }, [bundle, setQueryParams, url.legacyRef, url.legacySourceRef, url.legacyTargetRef, url.node, url.pathFrom, url.pathTo])

  // ---- 视图 ----
  const hiddenKey = url.hiddenTypes.join(',')
  const allowedTypes = useMemo(() => {
    if (!bundle || !url.hiddenTypes.length) return null
    const hidden = new Set(url.hiddenTypes)
    const allowed = new Set<string>()
    bundle.graph.forEachNode((_id, attrs) => { if (!hidden.has(attrs.type)) allowed.add(attrs.type) })
    return allowed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundle, hiddenKey])
  const pathForView = useMemo(() => (path?.found ? { nodes: path.nodes, edges: path.edges } : null), [path])
  const pinnedKey = pinned.join('|')
  const view = useMemo(() => {
    if (!bundle) return null
    return buildView(bundle, {
      view: url.view,
      now: bundle.generatedAt ?? Date.now() / 1000,
      windowHours: url.windowHours,
      maxNodes: url.maxNodes,
      minWeight: url.minWeight,
      types: allowedTypes,
      pinned,
      path: pathForView,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundle, url.view, url.windowHours, url.maxNodes, url.minWeight, allowedTypes, pinnedKey, pathForView])

  const legend = useMemo(() => {
    if (!bundle) return []
    const types: string[] = []
    bundle.graph.forEachNode((_id, attrs) => types.push(attrs.type))
    return buildLegendItems(types, theme, bundle.legendOrder)
  }, [bundle, theme])

  const pathNodes = useMemo(() => new Set(path?.found ? path.nodes : []), [path])
  const pathPairs = useMemo(() => {
    const pairs = new Set<string>()
    if (path?.found) for (let index = 1; index < path.nodes.length; index += 1) pairs.add(pairKey(path.nodes[index - 1], path.nodes[index]))
    return pairs
  }, [path])

  // 深链 / 搜索选中的节点不在当前视图里：钉进视图（带最强邻居）
  const selected = url.node && bundle?.graph.hasNode(url.node) ? url.node : null
  useEffect(() => {
    if (!view || !selected || view.graph.hasNode(selected) || pinned.includes(selected)) return
    setPinned([selected])
  }, [pinned, selected, view])

  const selectNode = useCallback((id: string | null) => {
    setQueryParams({ node: id })
  }, [setQueryParams])

  const focusNode = useCallback((id: string) => {
    selectNode(id)
    if (view?.graph.hasNode(id) && !layout?.running) {
      canvasRef.current?.focusNode(id)
    } else {
      pendingFocus.current = id
      if (!view?.graph.hasNode(id)) setPinned([id])
    }
  }, [layout?.running, selectNode, view])

  const onLayoutStatus = useCallback((status: LayoutStatus) => {
    setLayout(status)
    if (!status.running && pendingFocus.current) {
      const id = pendingFocus.current
      pendingFocus.current = null
      requestAnimationFrame(() => canvasRef.current?.focusNode(id))
    }
  }, [])

  // ---- 搜索 ----
  const matches = useMemo(() => {
    const text = query.trim().toLowerCase()
    if (!bundle || !text) return []
    const found: Array<{ id: string; label: string; type: string; importance: number; starts: boolean }> = []
    bundle.graph.forEachNode((id, attrs) => {
      const label = attrs.label.toLowerCase()
      const index = label.indexOf(text)
      if (index >= 0) found.push({ id, label: attrs.label, type: attrs.type, importance: attrs.importance, starts: index === 0 })
    })
    found.sort((a, b) => Number(b.starts) - Number(a.starts) || b.importance - a.importance || (a.id < b.id ? -1 : 1))
    return found.slice(0, 8)
  }, [bundle, query])

  // ---- 路径 ----
  const runPath = useCallback(async () => {
    if (!scope || !bundle || !url.pathFrom || !url.pathTo) return
    const from = url.pathFrom
    const to = url.pathTo
    setPath({ from, to, loading: true, found: false, nodes: [], edges: [] })
    try {
      if (bundle.layer === 'kg') {
        const result = await findKgPath(scope, { from, to, max_depth: 6 })
        const nodes = result.path ?? []
        if (nodes.length < 2) { setPath({ from, to, loading: false, found: false, nodes: [], edges: [], message: '6 步之内没有连通路径。' }); return }
        const edges = nodes.slice(1).map((id, index) => strongestEdgeBetween(bundle, nodes[index], id)).filter((key): key is string => Boolean(key))
        setPath({ from, to, loading: false, found: true, nodes, edges })
      } else {
        const tagBundle = bundle as TagBundle
        const source = tagBundle.rawNodes.get(from)?.ref
        const target = tagBundle.rawNodes.get(to)?.ref
        if (!source || !target) { setPath({ from, to, loading: false, found: false, nodes: [], edges: [], message: '缺少服务端签发的标签引用，无法找路径。' }); return }
        const result = await findTagGraphPath(scope, { source_ref: source, target_ref: target, layers: ['cooccurrence', 'relations'], max_depth: 8 })
        if (!result.found) {
          // 服务端在自己的完整投影上找路；该投影与本页取回的截取不完全一致，没找到时再在已取回的边上找一次，并如实标注来源
          const local = findLocalPath(tagBundle.graph, from, to, { directed: true, maxDepth: 8 })
          if (local && local.nodes.length > 1) {
            setPath({ from, to, loading: false, found: true, nodes: local.nodes, edges: local.edges, message: '服务端完整图未找到，这条路径来自本页已取回的标签关系。' })
          } else {
            setPath({ from, to, loading: false, found: false, nodes: [], edges: [], message: '8 步之内没有有向路径（标签路径只沿边的方向走）。' })
          }
          return
        }
        const merged = mergeTagPath(tagBundle, result, theme)
        if (merged !== tagBundle) setBundle(merged)
        setPath({ from, to, loading: false, found: true, nodes: result.path, edges: result.edges.map((edge) => edge.id) })
      }
    } catch (reason) {
      setPath({ from, to, loading: false, found: false, nodes: [], edges: [], message: humanizeApiError(reason, '路径查询失败') })
    }
  }, [bundle, scope, theme, url.pathFrom, url.pathTo])

  const clearPath = () => { setPath(null); setQueryParams({ path_from: null, path_to: null }) }

  // ---- 交互 ----
  const changeLayer = (layer: GraphLayerKind) => {
    if (layer === url.layer) return
    const changes: Record<string, string | null> = { layer }
    for (const key of LAYER_DEPENDENT_KEYS) changes[key] = null
    setQueryParams(changes, false)
  }
  const toggleType = (type: string) => {
    const hidden = new Set(url.hiddenTypes)
    if (hidden.has(type)) hidden.delete(type)
    else hidden.add(type)
    setQueryParams({ hide_types: [...hidden].join(',') || null })
  }
  const labelOf = (id: string | null) => (id && bundle?.graph.hasNode(id) ? bundle.graph.getNodeAttribute(id, 'label') : id ?? '')

  const selectedDetail = (() => {
    if (!selected || !bundle || !scope) return null
    if (bundle.layer === 'kg') {
      const raw = (bundle as KgBundle).rawNodes.get(selected)
      if (!raw) return null
      return (
        <KgNodeDetail
          scope={scope}
          nodeId={selected}
          node={raw}
          graph={bundle.graph}
          rawEdges={(bundle as KgBundle).rawEdges}
          theme={theme}
          pathFrom={url.pathFrom}
          pathTo={url.pathTo}
          onSetSource={(id) => setQueryParams({ path_from: id })}
          onSetTarget={(id) => setQueryParams({ path_to: id })}
          onSelect={focusNode}
        />
      )
    }
    const raw = (bundle as TagBundle).rawNodes.get(selected)
    if (!raw) return null
    return (
      <TagNodeDetail
        scope={scope}
        node={raw}
        graph={bundle.graph}
        theme={theme}
        recentWindowHours={bundle.recentWindowHours}
        pathFrom={url.pathFrom}
        pathTo={url.pathTo}
        onSetSource={(id) => setQueryParams({ path_from: id })}
        onSetTarget={(id) => setQueryParams({ path_to: id })}
        onSelect={focusNode}
        onMutated={() => { cacheRef.current.clear(); setReload((value) => value + 1) }}
      />
    )
  })()

  const summary = view?.summary
  const showWindow = url.view === 'new' || url.view === 'current'

  return (
    <div
      className="flex flex-col gap-4"
      data-page="graph"
      data-layer={url.layer}
      data-view={url.view}
      data-state={bundle && !loading && bundle.layer === url.layer ? 'ready' : 'loading'}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <header className="max-w-3xl">
          <div className="flex items-center gap-2">
            <NetworkIcon className="size-5 text-primary" aria-hidden="true" />
            <h1 className="text-xl font-bold tracking-tight">关系图谱</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">当前群里谁和什么有关、最近在聊什么、新出现了什么。点节点看证据，选两个点找路径。</p>
        </header>
        <div className="flex flex-wrap items-center gap-2">
          <div role="radiogroup" aria-label="数据层" className="inline-flex rounded-lg border bg-muted p-[3px]">
            {LAYERS.map((layer) => (
              <button
                key={layer.id}
                type="button"
                role="radio"
                aria-checked={url.layer === layer.id}
                title={layer.hint}
                onClick={() => changeLayer(layer.id)}
                className={cn(
                  'rounded-md px-3 py-1 text-sm font-medium transition-colors',
                  url.layer === layer.id ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {layer.label}
              </button>
            ))}
          </div>
          <Button type="button" size="sm" variant="outline" disabled={!scope || loading} onClick={() => { cacheRef.current.delete(fetchKey); setReload((value) => value + 1) }}>
            <RefreshCwIcon data-icon="inline-start" aria-hidden="true" className={cn(loading && 'animate-spin')} />刷新
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-3 rounded-xl border bg-card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Tabs value={url.view} onValueChange={(value) => setQueryParams({ view: value === 'strongest' ? null : value })}>
            <TabsList aria-label="视图">
              {GRAPH_VIEWS.map((item) => <TabsTrigger key={item.id} value={item.id}>{item.label}</TabsTrigger>)}
            </TabsList>
          </Tabs>
          {showWindow ? (
            <Select value={String(url.windowHours)} onValueChange={(value) => setQueryParams({ window: value === '168' ? null : value })}>
              <SelectTrigger size="sm" aria-label="时间窗口" className="w-28"><SelectValue /></SelectTrigger>
              <SelectContent>
                {WINDOW_OPTIONS_HOURS.map((hours) => <SelectItem key={hours} value={String(hours)}>最近 {formatWindow(hours)}</SelectItem>)}
              </SelectContent>
            </Select>
          ) : null}
          <div className="relative ml-auto w-full sm:w-64">
            <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input
              value={query}
              placeholder={url.layer === 'kg' ? '搜索人物 / 实体…' : '搜索标签…'}
              aria-label="搜索节点"
              className="h-8 pl-8"
              onChange={(event) => { setQuery(event.target.value); setSearchOpen(true) }}
              onFocus={() => setSearchOpen(true)}
              onBlur={() => setTimeout(() => setSearchOpen(false), 150)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && matches[0]) { focusNode(matches[0].id); setSearchOpen(false) }
                if (event.key === 'Escape') setSearchOpen(false)
              }}
            />
            {searchOpen && query.trim() ? (
              <ul role="listbox" aria-label="搜索结果" className="absolute right-0 left-0 z-20 mt-1 max-h-72 overflow-auto rounded-md border bg-popover p-1 shadow-md">
                {matches.length ? matches.map((item) => (
                  <li key={item.id} role="option" aria-selected={selected === item.id}>
                    <button
                      type="button"
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent"
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => { focusNode(item.id); setSearchOpen(false) }}
                    >
                      <span className="size-2.5 shrink-0 rounded-full" style={{ background: legend.find((entry) => entry.type === item.type)?.color }} aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate">{item.label}</span>
                      {view && !view.graph.hasNode(item.id) ? <span className="shrink-0 text-xs text-muted-foreground">不在当前视图</span> : null}
                    </button>
                  </li>
                )) : <li className="px-2 py-1.5 text-sm text-muted-foreground">没有匹配的节点</li>}
              </ul>
            ) : null}
          </div>
        </div>

        {isMobile ? (
          <Button type="button" size="sm" variant="ghost" className="self-start" aria-expanded={filtersOpen} onClick={() => setFiltersOpen((open) => !open)}>
            <SlidersHorizontalIcon data-icon="inline-start" aria-hidden="true" />{filtersOpen ? '收起筛选与图例' : '筛选与图例'}
          </Button>
        ) : null}
        <div className={cn('flex flex-wrap items-center gap-x-4 gap-y-2 text-sm', isMobile && !filtersOpen && 'hidden')} data-slot="graph-filters">
          <label className="flex items-center gap-2 text-muted-foreground">最多节点
            <Select value={String(url.maxNodes)} onValueChange={(value) => setQueryParams({ max_nodes: value })}>
              <SelectTrigger size="sm" aria-label="最多节点" className="w-20"><SelectValue /></SelectTrigger>
              <SelectContent>{MAX_NODE_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <label className="flex items-center gap-2 text-muted-foreground">最小权重
            <Select value={String(url.minWeight)} onValueChange={(value) => setQueryParams({ min_weight: value === '0' ? null : value })}>
              <SelectTrigger size="sm" aria-label="最小权重" className="w-20"><SelectValue /></SelectTrigger>
              <SelectContent>{MIN_WEIGHT_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value.toFixed(1)}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <label className="flex items-center gap-2 text-muted-foreground">
            <SparklesIcon className="size-4" aria-hidden="true" />光晕
            <Switch checked={url.glow} onCheckedChange={(checked) => setQueryParams({ glow: checked ? '1' : null })} aria-label="光晕" />
          </label>
          {legend.length ? (
            <div className="flex flex-wrap items-center gap-1.5" aria-label="按类型过滤">
              {legend.map((item) => {
                const hidden = url.hiddenTypes.includes(item.type)
                return (
                  <button
                    key={item.type}
                    type="button"
                    aria-pressed={!hidden}
                    title={hidden ? `显示「${item.label}」` : `隐藏「${item.label}」`}
                    onClick={() => toggleType(item.type)}
                    className={cn('inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs transition-opacity', hidden ? 'opacity-45 line-through' : 'hover:bg-accent')}
                  >
                    <span className="size-2.5 rounded-full" style={{ background: item.color }} aria-hidden="true" />
                    {item.label}<span className="tabular-nums text-muted-foreground">{item.count}</span>
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
      </div>

      {url.pathFrom || url.pathTo || path ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card px-3 py-2 text-sm" data-slot="graph-path">
          <RouteIcon className="size-4 text-muted-foreground" aria-hidden="true" />
          <span className="text-muted-foreground">起点</span><Badge variant={url.pathFrom ? 'secondary' : 'outline'}>{url.pathFrom ? labelOf(url.pathFrom) : '未选'}</Badge>
          <span className="text-muted-foreground">→ 终点</span><Badge variant={url.pathTo ? 'secondary' : 'outline'}>{url.pathTo ? labelOf(url.pathTo) : '未选'}</Badge>
          <Button type="button" size="sm" disabled={!url.pathFrom || !url.pathTo || path?.loading} onClick={() => { void runPath() }}>
            {path?.loading ? <LoaderCircleIcon data-icon="inline-start" className="animate-spin" aria-hidden="true" /> : null}找路径
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={clearPath}><XIcon data-icon="inline-start" aria-hidden="true" />清除</Button>
          {path && !path.loading ? (
            path.found
              ? <span className="text-sm">找到 {path.nodes.length - 1} 步：{path.nodes.map((id) => labelOf(id)).join(' → ')}{path.message ? <span className="ml-1 text-xs text-muted-foreground">（{path.message}）</span> : null}</span>
              : <span className="text-sm text-muted-foreground">{path.message}</span>
          ) : null}
          <span className="w-full text-xs text-muted-foreground">{url.layer === 'kg' ? '知识图谱路径不分方向，最多 6 步。' : '标签路径沿有向边查找（共现 + 显式关系），最多 8 步。'}</span>
        </div>
      ) : null}

      {!scope ? (
        scopeStatus === 'resolving'
          ? <Skeleton className="h-[60svh] w-full" />
          : <Card><CardContent className="p-6 text-sm text-muted-foreground">请先在顶栏选择 Bot 和群。</CardContent></Card>
      ) : error && !bundle ? (
        <Alert variant="destructive">
          <AlertTitle>关系图谱读取失败</AlertTitle>
          <AlertDescription>
            {humanizeApiError(error, '请检查当前群是否可用。')}
            <Button type="button" size="sm" variant="outline" className="ml-2" onClick={() => setReload((value) => value + 1)}>重试</Button>
          </AlertDescription>
        </Alert>
      ) : !bundle || !view ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_22rem]" role="status" aria-label="正在读取图谱">
          <Skeleton className="h-[60svh] w-full" />
          {wide ? <Skeleton className="h-[40svh] w-full" /> : null}
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-1" data-slot="graph-summary">
            <p className="text-sm font-medium" data-testid="graph-sentence">{summary?.sentence}</p>
            <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{view.graph.order} 个节点 · {view.graph.size} 条边</span>
              {bundle.layer === 'tags' ? <span>（本次取回 {bundle.graph.order} 个 / 本群共 {bundle.total} 个标签）</span> : <span>（知识图谱共 {bundle.graph.order} 个节点、{bundle.graph.size} 条边）</span>}
              {summary?.note ? <span className={cn(isMobile && 'hidden')}>· {summary.note}</span> : null}
            </p>
          </div>

          <div className={cn('grid items-start gap-4', wide && selectedDetail ? 'grid-cols-[minmax(0,1fr)_22rem]' : 'grid-cols-1')}>
            <div className={cn('relative overflow-hidden rounded-xl border', isMobile ? 'h-[62svh] min-h-[360px]' : 'h-[68svh] min-h-[460px]')}>
              {summary?.unavailable || view.graph.order === 0 ? (
                <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground" data-slot="graph-empty">
                  <div className="max-w-md">
                    <p className="font-medium text-foreground">{summary?.unavailable ? '这个视图暂时不可用' : '这里暂时没有可画的内容'}</p>
                    <p className="mt-2">{summary?.unavailable ?? summary?.sentence}</p>
                  </div>
                </div>
              ) : (
                <GraphCanvas
                  ref={canvasRef}
                  graph={view.graph}
                  theme={theme}
                  glow={url.glow}
                  selectedNode={selected}
                  pathNodes={pathNodes}
                  pathPairs={pathPairs}
                  onSelectNode={selectNode}
                  onLayoutStatus={onLayoutStatus}
                />
              )}
              {view.graph.order ? (
                <>
                  <div className="absolute top-2 right-2 flex flex-col gap-1">
                    <Button type="button" size="icon-sm" variant="outline" aria-label="放大" onClick={() => canvasRef.current?.zoomIn()}><PlusIcon aria-hidden="true" /></Button>
                    <Button type="button" size="icon-sm" variant="outline" aria-label="缩小" onClick={() => canvasRef.current?.zoomOut()}><MinusIcon aria-hidden="true" /></Button>
                    <Button type="button" size="icon-sm" variant="outline" aria-label="复位视角" onClick={() => canvasRef.current?.resetCamera()}><CrosshairIcon aria-hidden="true" /></Button>
                    <Button type="button" size="icon-sm" variant="outline" aria-label="重新布局" onClick={() => canvasRef.current?.relayout()}><ShuffleIcon aria-hidden="true" /></Button>
                  </div>
                  <div className="pointer-events-none absolute bottom-2 left-2 rounded-md border bg-background/90 px-2 py-1 text-xs text-muted-foreground" data-testid="layout-status" aria-live="polite">
                    {layout?.running
                      ? `布局中… ${layout.iterations} 步`
                      : layout?.reason
                        ? `${STOP_REASON[layout.reason] ?? '布局已停止'}（${layout.iterations} 步，${(layout.elapsedMs / 1000).toFixed(1)} 秒）`
                        : '准备布局…'}
                  </div>
                </>
              ) : null}
            </div>

            {wide && selectedDetail ? (
              <aside className="rounded-xl border bg-card" aria-label="节点详情">
                <div className="flex items-center justify-between border-b px-4 py-2">
                  <p className="text-sm font-medium">节点详情</p>
                  <Button type="button" size="icon-sm" variant="ghost" aria-label="关闭详情" onClick={() => selectNode(null)}><XIcon aria-hidden="true" /></Button>
                </div>
                <ScrollArea className="h-[calc(68svh-2.75rem)] min-h-[416px]">
                  <div className="p-4">{selectedDetail}</div>
                </ScrollArea>
              </aside>
            ) : null}
          </div>

          {!wide ? (
            <Sheet open={Boolean(selectedDetail)} onOpenChange={(open) => { if (!open) selectNode(null) }}>
              <SheetContent side={isMobile ? 'bottom' : 'right'} className={cn(isMobile ? 'max-h-[82svh]' : 'w-[min(92vw,28rem)] sm:max-w-md')} data-graph-sheet>
                <SheetHeader className="border-b pr-12">
                  <SheetTitle>节点详情</SheetTitle>
                  <SheetDescription>{selected ? labelOf(selected) : ''}</SheetDescription>
                </SheetHeader>
                <div className="min-h-0 flex-1 overflow-y-auto p-4">{selectedDetail}</div>
              </SheetContent>
            </Sheet>
          ) : null}
        </>
      )}
    </div>
  )
}

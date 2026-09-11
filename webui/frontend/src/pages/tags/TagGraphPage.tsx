import { useEffect, useMemo, useRef, useState } from 'react'
import { humanizeApiError } from '@/lib/reason-label'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowLeftIcon, BrainCircuitIcon, RefreshCwIcon, ShieldCheckIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { findTagGraphPath, getTagGraph, getTagGraphDetail, type TagGraphLayer, type TagGraphNode, type TagGraphPathPayload, type TagGraphPayload, type TagGraphScope } from '@/api/tagGraph'
import { TagGraphCanvas } from '@/components/tag-graph/TagGraphCanvas'
import { TagGraphControls } from '@/components/tag-graph/TagGraphControls'
import { TagGraphDetail } from '@/components/tag-graph/TagGraphDetail'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useIsMobile } from '@/hooks/use-mobile'
import { useCanonicalScopeDefault } from '@/hooks/use-pagination-search-params'

const DEFAULT_LAYERS: TagGraphLayer[] = ['cooccurrence', 'relations']

const LAYER_LABELS_ZH: Record<TagGraphLayer, string> = {
  cooccurrence: '有向共现',
  relations: '显式关系',
}

function parseLayers(value: string | null): TagGraphLayer[] {
  if (value === null) return DEFAULT_LAYERS
  if (value === 'none') return []
  return value.split(',').filter((item): item is TagGraphLayer => item === 'cooccurrence' || item === 'relations')
}

export function TagGraphPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const isMobile = useIsMobile()
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  const layerQuery = searchParams.get('layers')
  const layers = useMemo(() => parseLayers(layerQuery), [layerQuery])
  const includePulse = searchParams.get('pulse') === '1'
  const selectedRef = searchParams.get('ref')
  const sourceRef = searchParams.get('source_ref')
  const targetRef = searchParams.get('target_ref')
  const [graph, setGraph] = useState<TagGraphPayload | null>(null)
  const [detailNode, setDetailNode] = useState<TagGraphNode | null>(null)
  const [path, setPath] = useState<TagGraphPathPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [pathLoading, setPathLoading] = useState(false)
  const [error, setError] = useState<unknown>()
  const [reload, setReload] = useState(0)
  const graphRequest = useRef<AbortController | null>(null)
  const detailRequest = useRef<AbortController | null>(null)
  const pathRequest = useRef<AbortController | null>(null)
  const scope = useMemo<TagGraphScope | null>(() => botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId])

  const setQuery = (changes: Record<string, string | null>) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      Object.entries(changes).forEach(([key, value]) => { if (value) next.set(key, value); else next.delete(key) })
      return next
    })
  }

  useCanonicalScopeDefault({
    botId,
    sessionId,
    setFilters: (filters) => setQuery(filters as Record<string, string | null>),
  })

  useEffect(() => {
    graphRequest.current?.abort()
    if (!scope) { setGraph(null); setError(undefined); setLoading(false); return }
    if (!layers.length) { setGraph(null); setError(undefined); setLoading(false); return }
    const controller = new AbortController()
    graphRequest.current = controller
    setLoading(true)
    setError(undefined)
    const timeoutId = setTimeout(() => {
      controller.abort(new Error('API 请求超时（30 秒上限），请检查网络或刷新重试'))
    }, 30_000)

    getTagGraph(scope, { layers, includePulse, maxNodes: isMobile ? 200 : 1000, signal: controller.signal })
      .then((payload) => { if (!controller.signal.aborted && graphRequest.current === controller) setGraph(payload) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && graphRequest.current === controller && !isRequestCancelled(reason)) { setGraph(null); setError(reason) } })
      .finally(() => {
        clearTimeout(timeoutId)
        if (!controller.signal.aborted && graphRequest.current === controller) setLoading(false)
      })
    return () => {
      clearTimeout(timeoutId)
      controller.abort()
    }
  }, [includePulse, isMobile, layers, reload, scope])

  const graphNode = graph?.nodes.find((node) => node.ref === selectedRef) ?? null
  const selectedNode = graphNode ?? detailNode
  useEffect(() => {
    detailRequest.current?.abort()
    setDetailNode(null)
    if (!scope || !selectedRef || graphNode) return
    const controller = new AbortController()
    detailRequest.current = controller
    getTagGraphDetail(scope, selectedRef, layers, controller.signal)
      .then((payload) => { if (!controller.signal.aborted && detailRequest.current === controller) setDetailNode(payload.item) })
      .catch(() => { if (!controller.signal.aborted && detailRequest.current === controller) setDetailNode(null) })
    return () => controller.abort()
  }, [graphNode, layers, scope, selectedRef])

  const selectNode = (node: TagGraphNode) => setQuery({ ref: node.ref })
  const changeLayers = (nextLayers: TagGraphLayer[]) => {
    setPath(null)
    setQuery({ layers: nextLayers.length ? nextLayers.join(',') : 'none' })
  }
  const runPath = () => {
    pathRequest.current?.abort()
    if (!scope || !sourceRef || !targetRef) return
    const controller = new AbortController()
    pathRequest.current = controller
    setPathLoading(true)
    findTagGraphPath(scope, { source_ref: sourceRef, target_ref: targetRef, layers, max_depth: 8 }, controller.signal)
      .then((payload) => { if (!controller.signal.aborted && pathRequest.current === controller) setPath(payload) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && pathRequest.current === controller && !isRequestCancelled(reason)) setError(reason) })
      .finally(() => { if (!controller.signal.aborted && pathRequest.current === controller) setPathLoading(false) })
  }
  const clearPath = () => { pathRequest.current?.abort(); setPath(null); setQuery({ source_ref: null, target_ref: null }) }
  const pathEdgeIds = useMemo(() => new Set(path?.edges.map((edge) => edge.id) ?? []), [path])

  return <div className="flex flex-col gap-6" data-page="tag-graph">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <header className="max-w-3xl">
        <div className="flex items-center gap-2">
          <BrainCircuitIcon className="size-5 text-primary" aria-hidden="true" />
          <h1 className="text-xl font-bold tracking-tight">标签关系图</h1>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">直观展示当前群的标签共现网络与关联路径。</p>
      </header>
      <div className="flex gap-2">
        <Button asChild size="sm" variant="outline">
          <Link to="/tags"><ArrowLeftIcon aria-hidden="true" />返回标签总览</Link>
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={!scope || loading} onClick={() => setReload((value) => value + 1)}>
          <RefreshCwIcon aria-hidden="true" />刷新
        </Button>
      </div>
    </div>
    <Alert>
      <ShieldCheckIcon aria-hidden="true" />
      <AlertTitle>当前群 · 关系探索</AlertTitle>
      <AlertDescription>点击节点可高亮一跳邻居并查看关联记忆；路径查询只使用已选图层。</AlertDescription>
    </Alert>
    <div className="shrink-0"><TagGraphControls botId={botId} sessionId={sessionId} layers={layers} includePulse={includePulse} loading={loading} onScopeChange={({ botId: nextBot, sessionId: nextSession }) => setQuery({ bot_id: nextBot ?? botId, session_id: nextSession ?? sessionId, visibility: 'group', ref: null, source_ref: null, target_ref: null })} onLayersChange={changeLayers} onPulseChange={(enabled) => setQuery({ pulse: enabled ? '1' : null })} /></div>

    {!scope ? (
      <Card><CardContent className="p-6 text-sm text-muted-foreground">请先选择 Bot 和群聊。</CardContent></Card>
    ) : !layers.length ? (
      <Card><CardContent className="flex flex-col gap-2 p-6 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">未开启任何图层</span>
        <span>请在上方开启「有向共现」或「显式关系」以查看图谱节点。</span>
      </CardContent></Card>
    ) : error && !graph ? (
      <Alert variant="destructive">
        <AlertTitle>标签关系图读取失败</AlertTitle>
        <AlertDescription>
          {humanizeApiError(error, '请检查当前群是否可用。')}
          <Button type="button" size="sm" variant="outline" className="ml-2" onClick={() => setReload((value) => value + 1)}>重试</Button>
        </AlertDescription>
      </Alert>
    ) : loading && !graph ? (
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <Skeleton className="h-[32rem] w-full" />
        <Skeleton className="h-[26rem] w-full" />
      </div>
    ) : graph ? (
      <>
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge variant="secondary">节点 {graph.nodes.length}{graph.tag_total && graph.tag_total > graph.nodes.length ? ` / 本群共 ${graph.tag_total}` : ''}</Badge>
          <Badge variant="secondary">边 {graph.edges.length}</Badge>
          {graph.layers.map((layer) => <Badge key={layer} variant="outline">{LAYER_LABELS_ZH[layer] ?? layer} · {graph.layer_counts[layer]?.edges ?? 0}</Badge>)}
          {graph.pulse.enabled ? <Badge variant="outline">脉冲 · {graph.pulse.half_life_hours}h</Badge> : null}
        </div>
        {graph.nodes.length ? (
          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_23rem]">
            <TagGraphCanvas nodes={graph.nodes} edges={graph.edges} selectedRef={selectedRef} pathEdgeIds={pathEdgeIds} legend={graph.legend} onSelect={selectNode} />
            <TagGraphDetail scope={scope} node={selectedNode} sourceRef={sourceRef} targetRef={targetRef} path={path} pathLoading={pathLoading} onSetSource={(node) => setQuery({ source_ref: node.ref })} onSetTarget={(node) => setQuery({ target_ref: node.ref })} onRunPath={runPath} onClearPath={clearPath} onMutated={() => setReload((value) => value + 1)} />
          </div>
        ) : (
          <Card><CardContent className="p-6 text-sm text-muted-foreground">当前群暂无标签连线关系（共 {graph.tag_total ?? 0} 个独立标签）。</CardContent></Card>
        )}
      </>
    ) : null}
  </div>
}

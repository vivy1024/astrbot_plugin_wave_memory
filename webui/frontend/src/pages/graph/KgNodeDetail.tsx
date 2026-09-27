import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ClockIcon, ExternalLinkIcon, GitBranchIcon, QuoteIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { getKgEntity, getKgEntityTimeline, type KgEntityPayload, type KgGraphEdge, type KgGraphNode, type KgTimelinePayload } from '@/api/kg'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import type { GraphTheme } from '@/lib/graph-palette'
import type { RelationGraph } from '@/lib/graph/types'
import { humanizeApiError } from '@/lib/reason-label'
import { MemoryLink, PathEndpointButtons, Stat, StrongestNeighbors, TypeBadge } from './detail-shared'
import { formatTime, type GraphScope } from './graph-format'

export interface KgNodeDetailProps {
  scope: GraphScope
  nodeId: string
  node: KgGraphNode
  graph: RelationGraph
  rawEdges: Map<string, KgGraphEdge>
  theme: GraphTheme
  pathFrom: string | null
  pathTo: string | null
  onSetSource: (id: string) => void
  onSetTarget: (id: string) => void
  onSelect: (id: string) => void
}

const STATUS_LABELS: Record<string, string> = { pending: '待审', active: '生效', reviewed: '已审', approved: '已通过', rejected: '已拒绝' }

/** 知识图谱节点详情：本节点的关系与证据（来自 /api/kg/full 的边）、实体记忆（/api/kg/entity）与时间线。 */
export function KgNodeDetail({ scope, nodeId, node, graph, rawEdges, theme, pathFrom, pathTo, onSetSource, onSetTarget, onSelect }: KgNodeDetailProps) {
  const [entity, setEntity] = useState<KgEntityPayload | null>(null)
  const [timeline, setTimeline] = useState<KgTimelinePayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>('')
  const [showAll, setShowAll] = useState(false)
  const name = node.name

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setEntity(null)
    setTimeline(null)
    setShowAll(false)
    Promise.all([
      getKgEntity(scope, name, { limit: 15, signal: controller.signal }),
      getKgEntityTimeline(scope, name, { limit: 25, signal: controller.signal }),
    ]).then(([entityPayload, timelinePayload]) => {
      if (controller.signal.aborted) return
      setEntity(entityPayload)
      setTimeline(timelinePayload)
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || isRequestCancelled(reason)) return
      setError(humanizeApiError(reason, '实体详情读取失败'))
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [name, scope])

  const relations = useMemo(() => {
    if (!graph.hasNode(nodeId)) return []
    const items = graph.edges(nodeId).map((key) => rawEdges.get(key)).filter((edge): edge is KgGraphEdge => Boolean(edge))
    items.sort((a, b) => (Number(b.ts ?? 0) - Number(a.ts ?? 0)) || (a.id < b.id ? -1 : 1))
    return items
  }, [graph, nodeId, rawEdges])
  const visibleRelations = showAll ? relations : relations.slice(0, 8)
  const attrs = graph.hasNode(nodeId) ? graph.getNodeAttributes(nodeId) : null
  const nameOf = (id: string) => (graph.hasNode(id) ? graph.getNodeAttribute(id, 'label') : id.replace(/^entity:/, ''))
  const factsHref = `/facts?${new URLSearchParams({ bot_id: scope.bot_id, session_id: scope.session_id, visibility: scope.visibility, search: name }).toString()}`

  return (
    <div className="flex flex-col gap-4" data-graph-detail="kg">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold break-all">{name}</h2>
        <TypeBadge type={node.type} theme={theme} />
        <Button asChild size="xs" variant="outline" className="ml-auto">
          <Link to={factsHref}>在事实页管理<ExternalLinkIcon data-icon="inline-end" aria-hidden="true" /></Link>
        </Button>
      </div>

      <dl className="grid grid-cols-2 gap-2">
        <Stat label="关系数" value={attrs?.degree ?? node.degree} />
        <Stat label="本人发言" value={entity?.person ? `${entity.person.msg_count} 条` : '—'} />
        <Stat label="最早关系" value={formatTime(attrs?.createdAt)} />
        <Stat label="最近更新" value={formatTime(attrs?.lastSeenAt)} />
      </dl>

      <PathEndpointButtons nodeId={nodeId} pathFrom={pathFrom} pathTo={pathTo} onSetSource={onSetSource} onSetTarget={onSetTarget} />

      <section>
        <p className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><GitBranchIcon className="size-3.5" aria-hidden="true" />关系与证据（{relations.length}）</p>
        <ul className="mt-2 grid gap-2">
          {visibleRelations.map((edge) => {
            const outgoing = edge.s === nodeId
            const other = outgoing ? edge.t : edge.s
            const evidence = edge.provenance?.evidence
            const quote = edge.provenance?.source_quote
            return (
              <li key={edge.id} className="rounded-md border p-2 text-sm" data-relation={edge.id}>
                <div className="flex flex-wrap items-center gap-1">
                  {outgoing ? <span className="font-medium">{name}</span> : <button type="button" className="font-medium underline-offset-2 hover:underline" onClick={() => onSelect(other)}>{nameOf(other)}</button>}
                  <span className="text-muted-foreground">—{edge.l || '关联'}→</span>
                  {outgoing ? <button type="button" className="font-medium underline-offset-2 hover:underline" onClick={() => onSelect(other)}>{nameOf(other)}</button> : <span className="font-medium">{name}</span>}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="outline">{edge.kind === 'fact' ? '事实' : '标签关系'}</Badge>
                  {edge.status ? <span>{STATUS_LABELS[edge.status] ?? edge.status}</span> : null}
                  <span>置信度 {Number(edge.confidence ?? edge.weight ?? 0).toFixed(2)}</span>
                  <span>{formatTime(edge.ts)}</span>
                </div>
                {evidence ? <p className="mt-2 text-xs text-muted-foreground">证据：{evidence}</p> : null}
                {quote ? (
                  <blockquote className="mt-1 flex gap-1 border-l-2 pl-2 text-xs italic text-muted-foreground">
                    <QuoteIcon className="mt-0.5 size-3 shrink-0" aria-hidden="true" />{quote}
                  </blockquote>
                ) : null}
                {edge.source_memory_id ? <div className="mt-2"><MemoryLink scope={scope} memoryId={edge.source_memory_id}>来源记忆 #{edge.source_memory_id}</MemoryLink></div> : null}
              </li>
            )
          })}
        </ul>
        {relations.length > 8 ? (
          <Button type="button" size="sm" variant="ghost" className="mt-1" onClick={() => setShowAll((value) => !value)}>
            {showAll ? '收起' : `展开全部 ${relations.length} 条`}
          </Button>
        ) : null}
      </section>

      <section>
        <p className="text-xs font-medium text-muted-foreground">最强关联</p>
        <div className="mt-1"><StrongestNeighbors graph={graph} nodeId={nodeId} theme={theme} onSelect={onSelect} /></div>
      </section>

      {loading ? (
        <div className="grid gap-2" role="status" aria-label="正在读取实体详情"><Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" /></div>
      ) : error ? (
        <p className="text-sm text-destructive">{error}</p>
      ) : (
        <>
          <section>
            <p className="text-xs font-medium text-muted-foreground">本人发言（{entity?.memories.length ?? 0}）</p>
            {entity?.memories.length ? (
              <div className="mt-2 grid gap-2">
                {entity.memories.slice(0, 6).map((memory) => (
                  <article key={memory.id} className="rounded-md border p-2">
                    <p className="line-clamp-3 text-sm">{memory.content}</p>
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>{formatTime(memory.ts)}</span>
                      <MemoryLink scope={scope} memoryId={memory.id} />
                    </div>
                  </article>
                ))}
              </div>
            ) : <p className="mt-1 text-xs text-muted-foreground">没有以「{name}」为发送者的记忆（非人物实体通常没有）。</p>}
          </section>
          <section>
            <p className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><ClockIcon className="size-3.5" aria-hidden="true" />时间线</p>
            {timeline?.events.length ? (
              <ol className="mt-2 grid gap-1 border-l pl-3">
                {timeline.events.slice(0, 12).map((event, index) => (
                  <li key={`${event.type}-${index}`} className="text-xs">
                    <span className="text-muted-foreground">{formatTime(event.ts)}</span>{' '}
                    {event.type === 'fact'
                      ? <span>{event.subject} —{event.predicate}→ {event.object}</span>
                      : <span className="text-muted-foreground">发言：{event.content.slice(0, 60)}</span>}
                  </li>
                ))}
              </ol>
            ) : <p className="mt-1 text-xs text-muted-foreground">暂无时间线事件。</p>}
          </section>
        </>
      )}
    </div>
  )
}

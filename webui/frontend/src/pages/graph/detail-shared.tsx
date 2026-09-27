import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLinkIcon, FlagIcon, MapPinIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { nodeColor, nodeTypeLabel, type GraphTheme } from '@/lib/graph-palette'
import type { RelationGraph } from '@/lib/graph/types'
import { memoryHref, type GraphScope } from './graph-format'


export function MemoryLink({ scope, memoryId, children = '查看记忆' }: { scope: GraphScope; memoryId: number | string; children?: ReactNode }) {
  return (
    <Button asChild size="xs" variant="outline">
      <Link to={memoryHref(scope, memoryId)} data-memory-link={memoryId}>
        {children}
        <ExternalLinkIcon data-icon="inline-end" aria-hidden="true" />
      </Link>
    </Button>
  )
}

export function TypeBadge({ type, theme }: { type: string; theme: GraphTheme }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs text-foreground">
      <span className="size-2.5 rounded-full" style={{ background: nodeColor(type, theme) }} aria-hidden="true" />
      {nodeTypeLabel(type)}
    </span>
  )
}

export function Stat({ label, value, icon }: { label: string; value: ReactNode; icon?: ReactNode }) {
  return (
    <div className="rounded-md border p-2">
      <dt className="flex items-center gap-1 text-xs text-muted-foreground">{icon}{label}</dt>
      <dd className="mt-1 text-sm font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

export function PathEndpointButtons({ nodeId, pathFrom, pathTo, onSetSource, onSetTarget }: {
  nodeId: string
  pathFrom: string | null
  pathTo: string | null
  onSetSource: (id: string) => void
  onSetTarget: (id: string) => void
}) {
  return (
    <div className="flex flex-wrap gap-2">
      <Button type="button" size="sm" variant={pathFrom === nodeId ? 'secondary' : 'outline'} onClick={() => onSetSource(nodeId)}>
        <MapPinIcon data-icon="inline-start" aria-hidden="true" />设为起点
      </Button>
      <Button type="button" size="sm" variant={pathTo === nodeId ? 'secondary' : 'outline'} onClick={() => onSetTarget(nodeId)}>
        <FlagIcon data-icon="inline-start" aria-hidden="true" />设为终点
      </Button>
    </div>
  )
}

/** 完整数据里与该节点连得最强的邻居（不受当前视图过滤影响），点击即切换选中。 */
export function StrongestNeighbors({ graph, nodeId, theme, onSelect, limit = 8 }: {
  graph: RelationGraph
  nodeId: string
  theme: GraphTheme
  onSelect: (id: string) => void
  limit?: number
}) {
  if (!graph.hasNode(nodeId)) return null
  const best = new Map<string, { weight: number; label: string; outgoing: boolean }>()
  graph.forEachEdge(nodeId, (_key, attrs, source, target) => {
    const other = source === nodeId ? target : source
    const current = best.get(other)
    if (!current || attrs.weight > current.weight) best.set(other, { weight: attrs.weight, label: attrs.label, outgoing: source === nodeId })
  })
  const items = [...best.entries()].sort((a, b) => (b[1].weight - a[1].weight) || (a[0] < b[0] ? -1 : 1)).slice(0, limit)
  if (!items.length) return <p className="text-xs text-muted-foreground">完整数据里没有与它相连的关系。</p>
  return (
    <ul className="grid gap-1">
      {items.map(([id, info]) => {
        const attrs = graph.getNodeAttributes(id)
        return (
          <li key={id}>
            <button type="button" className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-accent" onClick={() => onSelect(id)}>
              <span className="size-2.5 shrink-0 rounded-full" style={{ background: nodeColor(attrs.type, theme) }} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{attrs.label}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{info.outgoing ? '→' : '←'} {info.label || '关联'} · {info.weight.toFixed(2)}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

import { useEffect, useMemo, useState } from 'react'

import type { TagGraphEdge, TagGraphNode } from '@/api/tagGraph'
import { Badge } from '@/components/ui/badge'
import { useIsMobile } from '@/hooks/use-mobile'
import { cn } from '@/lib/utils'

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(Boolean(query?.matches))
    query?.addEventListener?.('change', update)
    update()
    return () => query?.removeEventListener?.('change', update)
  }, [])
  return reduced
}

function colorFor(value: string): string {
  const palette = ['#38bdf8', '#a78bfa', '#34d399', '#f59e0b', '#fb7185', '#22d3ee']
  const hash = Array.from(value).reduce((sum, char) => sum + char.charCodeAt(0), 0)
  return palette[hash % palette.length]
}

export interface TagGraphCanvasProps {
  nodes: TagGraphNode[]
  edges: TagGraphEdge[]
  selectedRef?: string | null
  pathEdgeIds?: Set<string>
  onSelect: (node: TagGraphNode) => void
}

export function TagGraphCanvas({ nodes, edges, selectedRef, pathEdgeIds = new Set(), onSelect }: TagGraphCanvasProps) {
  const isMobile = useIsMobile()
  const reducedMotion = usePrefersReducedMotion()
  const layout = useMemo(() => {
    const width = 920
    const height = 520
    const centerX = width / 2
    const centerY = height / 2
    const ring = Math.min(width, height) * 0.39
    const sorted = [...nodes].sort((left, right) => (right.in_degree + right.out_degree) - (left.in_degree + left.out_degree) || left.name.localeCompare(right.name))
    const positions = new Map<string, { x: number; y: number }>()
    sorted.forEach((node, index) => {
      if (sorted.length === 1) positions.set(node.id, { x: centerX, y: centerY })
      else {
        const angle = -Math.PI / 2 + (Math.PI * 2 * index) / sorted.length
        const lane = 0.62 + (index % 3) * 0.19
        positions.set(node.id, { x: centerX + Math.cos(angle) * ring * lane, y: centerY + Math.sin(angle) * ring * lane })
      }
    })
    return { width, height, positions }
  }, [nodes])

  if (isMobile) {
    return <div className="grid gap-2" data-tag-graph-mode="list" aria-label="标签关系图移动端列表">
      {[...nodes].sort((a, b) => (b.in_degree + b.out_degree) - (a.in_degree + a.out_degree)).map((node) => <button key={node.id} type="button" className={cn('rounded-lg border bg-card p-3 text-left', selectedRef === node.ref && 'border-primary ring-1 ring-primary/30')} onClick={() => onSelect(node)}>
        <span className="flex items-center justify-between gap-3"><span className="min-w-0 truncate font-medium">{node.name}</span><Badge variant="outline">{node.type}</Badge></span>
        <span className="mt-2 grid grid-cols-3 gap-2 text-xs text-muted-foreground"><span>记忆 {node.memory_count}</span><span>入度 {node.in_degree}</span><span>出度 {node.out_degree}</span></span>
      </button>)}
      <p className="text-xs text-muted-foreground">移动端已降级为可访问列表；路径和图层筛选仍使用同一正式 API。</p>
    </div>
  }

  return <div className="overflow-hidden rounded-xl border bg-slate-950" data-tag-graph-mode="svg">
    <svg viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label={`标签关系图，共 ${nodes.length} 个节点、${edges.length} 条有向边`} className="h-auto min-h-[28rem] w-full">
      <defs>
        <marker id="tag-graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker>
      </defs>
      <g aria-hidden="true">
        {edges.map((edge) => {
          const source = layout.positions.get(edge.source)
          const target = layout.positions.get(edge.target)
          if (!source || !target) return null
          const highlighted = pathEdgeIds.has(edge.id)
          const opacity = Math.max(0.18, Math.min(0.82, edge.confidence || edge.weight))
          return <g key={edge.id} className={edge.layer === 'relations' ? 'text-fuchsia-400' : 'text-sky-400'}>
            <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke="currentColor" strokeOpacity={highlighted ? 1 : opacity} strokeWidth={highlighted ? 4 : Math.max(1, edge.weight * 3)} markerEnd="url(#tag-graph-arrow)" strokeDasharray={edge.layer === 'relations' ? '7 5' : undefined} />
            {!reducedMotion && (edge.pulse_energy ?? 0) > 0 ? <circle r={2 + Math.min(4, (edge.pulse_energy ?? 0) * 6)} fill="currentColor"><animate attributeName="cx" values={`${source.x};${target.x}`} dur={`${Math.max(1.2, 3.4 - (edge.pulse_energy ?? 0) * 2)}s`} repeatCount="indefinite" /><animate attributeName="cy" values={`${source.y};${target.y}`} dur={`${Math.max(1.2, 3.4 - (edge.pulse_energy ?? 0) * 2)}s`} repeatCount="indefinite" /></circle> : null}
          </g>
        })}
      </g>
      <g>
        {nodes.map((node) => {
          const position = layout.positions.get(node.id)
          if (!position) return null
          const degree = node.in_degree + node.out_degree
          const radius = Math.max(15, Math.min(30, 14 + Math.sqrt(degree + node.memory_count) * 2.3))
          const selected = selectedRef === node.ref
          return <g key={node.id} role="button" tabIndex={0} aria-label={`选择标签 ${node.name}`} transform={`translate(${position.x} ${position.y})`} className="cursor-pointer outline-none" onClick={() => onSelect(node)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node) } }}>
            <circle r={radius + (selected ? 7 : 3)} fill={colorFor(node.type)} fillOpacity={selected ? 0.26 : 0.12} stroke={colorFor(node.type)} strokeWidth={selected ? 3 : 1.5} />
            <circle r={Math.max(6, radius * 0.42)} fill={colorFor(node.type)} fillOpacity={0.88} />
            <text y={radius + 16} textAnchor="middle" fill="white" fontSize="12" fontWeight={selected ? 700 : 500}>{node.name.length > 16 ? `${node.name.slice(0, 15)}…` : node.name}</text>
            <title>{`${node.name} · ${node.type} · 入度 ${node.in_degree} · 出度 ${node.out_degree} · 记忆 ${node.memory_count}`}</title>
          </g>
        })}
      </g>
    </svg>
    <div className="flex flex-wrap gap-3 border-t border-white/10 px-3 py-2 text-xs text-slate-300"><span><i className="mr-1 inline-block h-0.5 w-5 bg-sky-400" />序位共现</span><span><i className="mr-1 inline-block w-5 border-t border-dashed border-fuchsia-400" />显式关系</span><span>{reducedMotion ? '已遵循减少动态效果偏好' : '脉冲仅在 API 返回真实能量时显示'}</span></div>
  </div>
}

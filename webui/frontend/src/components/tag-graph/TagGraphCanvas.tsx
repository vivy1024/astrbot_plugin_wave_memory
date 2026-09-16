import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  FlaskConicalIcon,
  Loader2Icon,
  Maximize2Icon,
  Minimize2Icon,
  PauseIcon,
  PlayIcon,
  RotateCcwIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  XIcon,
  ZoomInIcon,
  ZoomOutIcon,
} from 'lucide-react'

import type { TagGraphEdge, TagGraphNode, TagGraphPayload, TagGraphScope } from '@/api/tagGraph'
import { runQueryDebug, type QueryDebugResponse, type QueryStageName } from '@/api/memories'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useIsMobile } from '@/hooks/use-mobile'
import { cn } from '@/lib/utils'

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches))
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(Boolean(query?.matches))
    query?.addEventListener?.('change', update)
    update()
    return () => query?.removeEventListener?.('change', update)
  }, [])
  return reduced
}

export interface NodePalette {
  core: string
  glow: string
  text: string
}

export const TYPE_PALETTES: Record<string, NodePalette> = {
  topic: { core: '#34d399', glow: 'rgba(52, 211, 153, 0.35)', text: '#a7f3d0' },
  event: { core: '#f97316', glow: 'rgba(249, 115, 22, 0.35)', text: '#fed7aa' },
  keyword: { core: '#38bdf8', glow: 'rgba(56, 189, 248, 0.35)', text: '#bae6fd' },
  entity: { core: '#a78bfa', glow: 'rgba(167, 139, 250, 0.35)', text: '#ddd6fe' },
  fact: { core: '#f472b6', glow: 'rgba(244, 114, 182, 0.35)', text: '#fbcfe8' },
  person: { core: '#818cf8', glow: 'rgba(129, 140, 248, 0.35)', text: '#c7d2fe' },
  emotion: { core: '#fbbf24', glow: 'rgba(251, 191, 36, 0.35)', text: '#fde68a' },
  location: { core: '#2dd4bf', glow: 'rgba(45, 212, 191, 0.35)', text: '#99f6e4' },
  time: { core: '#22d3ee', glow: 'rgba(34, 211, 238, 0.35)', text: '#a5f3fc' },
  jargon: { core: '#fb7185', glow: 'rgba(251, 113, 133, 0.35)', text: '#fecdd3' },
  default: { core: '#94a3b8', glow: 'rgba(148, 163, 184, 0.30)', text: '#e2e8f0' },
}

export function paletteFor(type: string): NodePalette {
  return TYPE_PALETTES[type.toLowerCase()] ?? TYPE_PALETTES.default
}

export const TYPE_LABELS: Record<string, string> = {
  topic: '话题',
  event: '事件',
  keyword: '关键词',
  entity: '实体',
  fact: '事实',
  person: '人物',
  emotion: '情绪',
  location: '地点',
  time: '时间',
  jargon: '黑话',
  default: '其他',
}

export function typeLabel(type: string): string {
  return TYPE_LABELS[type.toLowerCase()] ?? type
}

export interface LegendItem {
  type: string
  label: string
  count: number
  color: string
}

export function buildLegendItems(
  nodes: Array<{ type?: string }>,
  legend?: TagGraphPayload['legend'] | null
): LegendItem[] {
  const counts: Record<string, number> = {}
  for (const n of nodes) {
    const t = (n.type || 'default').toLowerCase()
    counts[t] = (counts[t] || 0) + 1
  }

  const presentTypes = Object.keys(counts)
  let orderedTypes: string[]
  if (legend?.types && legend.types.length > 0) {
    const configuredLower = legend.types.map((t) => t.toLowerCase())
    orderedTypes = configuredLower.filter((t) => presentTypes.includes(t))
    for (const t of presentTypes) {
      if (!orderedTypes.includes(t)) orderedTypes.push(t)
    }
  } else {
    orderedTypes = presentTypes
  }

  return orderedTypes.map((t) => ({
    type: t,
    label: typeLabel(t),
    count: counts[t] || 0,
    color: paletteFor(t).core,
  }))
}

const WORLD_WIDTH = 2600
const WORLD_HEIGHT = 1800

interface SimNode {
  id: string
  raw: TagGraphNode
  x: number
  y: number
  vx: number
  vy: number
  radius: number
  degree: number
}

interface SimEdge {
  id: string
  source: SimNode
  target: SimNode
  weight: number
  layer: string
  pulseEnergy: number
}

export interface TagGraphCanvasProps {
  nodes: TagGraphNode[]
  edges: TagGraphEdge[]
  selectedRef?: string | null
  pathEdgeIds?: Set<string>
  legend?: TagGraphPayload['legend'] | null
  scope?: TagGraphScope | null
  maxNodes: number
  minConfidence: number
  pulseHalfLifeHours: number
  onSelect: (node: TagGraphNode) => void
  onParamsChange: (params: { maxNodes?: number; minConfidence?: number; pulseHalfLifeHours?: number }) => void
}

export function TagGraphCanvas({
  nodes,
  edges,
  selectedRef,
  pathEdgeIds = new Set(),
  legend,
  scope,
  maxNodes,
  minConfidence,
  pulseHalfLifeHours,
  onSelect,
  onParamsChange,
}: TagGraphCanvasProps) {
  const isMobile = useIsMobile()
  const reducedMotion = usePrefersReducedMotion()
  const legendItems = useMemo(() => buildLegendItems(nodes, legend), [nodes, legend])
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const [isFullscreen, setIsFullscreen] = useState(false)
  const [isSimulating, setIsSimulating] = useState(false)
  const [activeFilterType, setActiveFilterType] = useState<string | null>(null)
  const [hoveredNode, setHoveredNode] = useState<TagGraphNode | null>(null)

  // 悬浮配置面板与实验室抽屉开关 (内嵌在 Canvas 内部，全屏也完全可用)
  const [showConfig, setShowConfig] = useState(false)
  const [activeTab, setActiveTab] = useState<'view' | 'lab'>('view')

  // 本地调节草稿参数
  const [draftNodes, setDraftNodes] = useState(maxNodes)
  const [draftConfidence, setDraftConfidence] = useState(minConfidence)
  const [draftHalfLife, setDraftHalfLife] = useState(pulseHalfLifeHours)

  // 算法实验室状态
  const [labText, setLabText] = useState('')
  const [labLoading, setLabLoading] = useState(false)
  const [labStages, setLabStages] = useState<Record<QueryStageName, boolean>>({
    spike: true,
    geodesic: true,
    epa: true,
    pyramid: true,
  })
  const [labResult, setLabResult] = useState<QueryDebugResponse | null>(null)
  const [highlightTags, setHighlightTags] = useState<Set<string>>(new Set())

  // 视角变换矩阵
  const transformRef = useRef({ x: 0, y: 0, scale: 1.0 })
  const isDraggingRef = useRef(false)
  const dragStartRef = useRef({ x: 0, y: 0 })
  const pulsePhaseRef = useRef(0)

  // 构建力导向仿真数据模型
  const simData = useMemo(() => {
    const width = WORLD_WIDTH
    const height = WORLD_HEIGHT
    const centerX = width / 2
    const centerY = height / 2

    const nodeMap = new Map<string, SimNode>()
    const sorted = [...nodes].sort(
      (a, b) => b.in_degree + b.out_degree - (a.in_degree + a.out_degree) || a.name.localeCompare(b.name)
    )

    sorted.forEach((node, idx) => {
      const degree = (node.in_degree || 0) + (node.out_degree || 0)
      const radius = Math.max(5, Math.min(18, 5 + Math.sqrt(degree) * 2.2))
      const phi = (idx / Math.max(1, sorted.length)) * Math.PI * 2 * 3.5
      const rad = 60 + Math.sqrt(idx) * 36
      const x = centerX + Math.cos(phi) * rad + (Math.random() - 0.5) * 20
      const y = centerY + Math.sin(phi) * rad + (Math.random() - 0.5) * 20
      nodeMap.set(node.id, {
        id: node.id,
        raw: node,
        x,
        y,
        vx: 0,
        vy: 0,
        radius,
        degree,
      })
    })

    const simEdges: SimEdge[] = []
    edges.forEach((edge) => {
      const source = nodeMap.get(edge.source)
      const target = nodeMap.get(edge.target)
      if (source && target) {
        simEdges.push({
          id: edge.id,
          source,
          target,
          weight: Math.max(0.1, Math.min(1.0, edge.weight || 0.5)),
          layer: edge.layer,
          pulseEnergy: (edge as any).pulse_energy || (edge.weight ? edge.weight * 0.8 : 0),
        })
      }
    })

    return { nodes: Array.from(nodeMap.values()), edges: simEdges, nodeMap, width, height }
  }, [nodes, edges])

  // 居中自适应视图
  const resetView = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    if (!simData.nodes.length) {
      transformRef.current = { x: rect.width / 2, y: rect.height / 2, scale: 1.0 }
      return
    }

    let minX = Infinity
    let maxX = -Infinity
    let minY = Infinity
    let maxY = -Infinity
    simData.nodes.forEach((n) => {
      minX = Math.min(minX, n.x)
      maxX = Math.max(maxX, n.x)
      minY = Math.min(minY, n.y)
      maxY = Math.max(maxY, n.y)
    })

    const spanX = Math.max(120, maxX - minX)
    const spanY = Math.max(120, maxY - minY)
    const padding = 60
    const scaleX = (rect.width - padding * 2) / spanX
    const scaleY = (rect.height - padding * 2) / spanY
    const scale = Math.max(0.28, Math.min(1.8, Math.min(scaleX, scaleY)))

    const graphCenterX = (minX + maxX) / 2
    const graphCenterY = (minY + maxY) / 2
    transformRef.current = {
      scale,
      x: rect.width / 2 - graphCenterX * scale,
      y: rect.height / 2 - graphCenterY * scale,
    }
  }, [simData.nodes])

  useEffect(() => {
    resetView()
  }, [resetView])

  useEffect(() => {
    const timer = setTimeout(() => resetView(), 120)
    return () => clearTimeout(timer)
  }, [isFullscreen, resetView])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsFullscreen(false)
        setShowConfig(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // 物理步进计算
  const stepPhysics = useCallback(() => {
    const { nodes: simNodes, edges: simEdges, width, height } = simData
    const centerX = width / 2
    const centerY = height / 2

    for (let i = 0; i < simNodes.length; i++) {
      const na = simNodes[i]
      for (let j = i + 1; j < simNodes.length; j++) {
        const nb = simNodes[j]
        const dx = nb.x - na.x
        const dy = nb.y - na.y
        const distSq = dx * dx + dy * dy || 1
        const minDist = na.radius + nb.radius + 18
        if (distSq < 360000) {
          const dist = Math.sqrt(distSq)
          const force = (minDist * minDist * 0.8) / distSq
          const fx = (dx / dist) * force
          const fy = (dy / dist) * force
          na.vx -= fx
          na.vy -= fy
          nb.vx += fx
          nb.vy += fy
        }
      }

      const cdx = centerX - na.x
      const cdy = centerY - na.y
      na.vx += cdx * 0.003
      na.vy += cdy * 0.003
    }

    for (let e = 0; e < simEdges.length; e++) {
      const edge = simEdges[e]
      const dx = edge.target.x - edge.source.x
      const dy = edge.target.y - edge.source.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const targetDist = 70 + (1.0 - edge.weight) * 110
      const force = (dist - targetDist) * 0.02 * edge.weight
      const fx = (dx / dist) * force
      const fy = (dy / dist) * force
      edge.source.vx += fx
      edge.source.vy += fy
      edge.target.vx -= fx
      edge.target.vy -= fy
    }

    let totalKinetic = 0
    for (let i = 0; i < simNodes.length; i++) {
      const n = simNodes[i]
      n.vx *= 0.86
      n.vy *= 0.86
      n.x += n.vx
      n.y += n.vy
      totalKinetic += n.vx * n.vx + n.vy * n.vy
    }

    if (totalKinetic < 0.25 && simNodes.length > 5) {
      setIsSimulating(false)
    }
  }, [simData])

  // 执行算法实验室查询联动
  const handleRunLabQuery = async () => {
    if (!scope || !labText.trim() || labLoading) return
    setLabLoading(true)
    try {
      const res = await runQueryDebug({
        text: labText.trim(),
        topK: 5,
        scope,
        stages: labStages,
        params: {},
      })
      setLabResult(res)

      // 提取命中的标签并在图谱上高亮
      const hits = new Set<string>()
      const debugHighlights = (res.debug as any)?.highlights || {}
      ;(debugHighlights.seed_tags || []).forEach((t: any) => {
        const id = typeof t === 'object' ? t.tag_id : t
        if (id) hits.add(String(id))
      })
      ;(debugHighlights.pyramid_tags || []).forEach((t: any) => {
        const id = typeof t === 'object' ? t.tag_id : t
        if (id) hits.add(String(id))
      })
      setHighlightTags(hits)
    } catch (e) {
      console.error(e)
    } finally {
      setLabLoading(false)
    }
  }

  // 主画布渲染循环
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let isRunning = true
    const palette = {
      background: '#07101b',
      backgroundMid: '#060d17',
      backgroundOuter: '#03070d',
      grid: 'rgba(56, 189, 248, 0.04)',
      edgeCooccurrence: 'rgba(125, 211, 252, 0.18)',
      edgeRelations: 'rgba(167, 139, 250, 0.28)',
      hitGlow: 'rgba(56, 189, 248, 0.6)',
      transparent: 'rgba(0,0,0,0)',
      selectedText: '#ffffff',
      edgePath: '#f43f5e',
      ring: '#38bdf8',
      pulse: '#38bdf8',
    }

    const render = () => {
      if (!isRunning) return
      if (isSimulating && !reducedMotion) stepPhysics()

      if (!reducedMotion) pulsePhaseRef.current = (pulsePhaseRef.current + 0.016) % 1.0
      const dpr = window.devicePixelRatio || 1
      const rect = canvas.getBoundingClientRect()
      if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
        canvas.width = rect.width * dpr
        canvas.height = rect.height * dpr
      }

      ctx.save()
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, rect.width, rect.height)

      const bgGrad = ctx.createRadialGradient(
        rect.width * 0.48, rect.height * 0.42, 20,
        rect.width * 0.5, rect.height * 0.5, rect.width * 0.82
      )
      bgGrad.addColorStop(0, palette.background)
      bgGrad.addColorStop(0.52, palette.backgroundMid)
      bgGrad.addColorStop(1, palette.backgroundOuter)
      ctx.fillStyle = bgGrad
      ctx.fillRect(0, 0, rect.width, rect.height)
      ctx.strokeStyle = palette.grid

      const { x: panX, y: panY, scale } = transformRef.current
      ctx.save()
      ctx.translate(panX, panY)
      ctx.scale(scale, scale)

      const selectedSimNode = simData.nodes.find((n) => n.raw.ref === selectedRef)
      const neighborIds = new Set<string>()
      if (selectedSimNode) {
        neighborIds.add(selectedSimNode.id)
        simData.edges.forEach((e) => {
          if (e.source.id === selectedSimNode.id) neighborIds.add(e.target.id)
          if (e.target.id === selectedSimNode.id) neighborIds.add(e.source.id)
        })
      }

      // 1. 绘制突触连线
      simData.edges.forEach((edge) => {
        const isPath = pathEdgeIds.has(edge.id)
        const isConnectedToSelected = selectedSimNode && (edge.source.id === selectedSimNode.id || edge.target.id === selectedSimNode.id)
        const isDimmed = selectedSimNode && !isConnectedToSelected && !isPath

        ctx.lineWidth = isPath ? 3.0 : isConnectedToSelected ? 2.0 : Math.max(0.6, edge.weight * 2.2)
        ctx.strokeStyle = isPath ? palette.edgePath : isConnectedToSelected ? palette.ring : edge.layer === 'relations' ? palette.edgeRelations : palette.edgeCooccurrence
        ctx.globalAlpha = isDimmed ? 0.05 : isPath ? 1.0 : isConnectedToSelected ? 0.85 : Math.max(0.12, edge.weight * 0.6)

        ctx.beginPath()
        ctx.moveTo(edge.source.x, edge.source.y)
        ctx.lineTo(edge.target.x, edge.target.y)
        ctx.stroke()

        if (!reducedMotion && edge.pulseEnergy > 0) {
          const progress = (pulsePhaseRef.current + (edge.source.x % 100) * 0.01) % 1.0
          const px = edge.source.x + (edge.target.x - edge.source.x) * progress
          const py = edge.source.y + (edge.target.y - edge.source.y) * progress
          ctx.globalAlpha = Math.min(1.0, edge.pulseEnergy * 1.5)
          ctx.fillStyle = palette.pulse
          ctx.beginPath()
          ctx.arc(px, py, 2.5 + Math.min(3.5, edge.pulseEnergy * 3), 0, Math.PI * 2)
          ctx.fill()
        }
      })

      // 2. 绘制发光节点星体
      simData.nodes.forEach((node) => {
        const isSelected = node.raw.ref === selectedRef
        const isHovered = node.raw.ref === hoveredNode?.ref
        const isNeighbor = neighborIds.has(node.id)
        const isTypeDimmed = activeFilterType !== null && node.raw.type.toLowerCase() !== activeFilterType
        const isLabHit = highlightTags.has(String(node.raw.id)) || highlightTags.has(node.raw.name)
        const isDimmed = (selectedSimNode && !isNeighbor) || isTypeDimmed

        const p = paletteFor(node.raw.type)
        const nodeAlpha = isDimmed ? 0.18 : 0.92

        const glowRadius = node.radius * (isSelected || isLabHit ? 2.6 : isHovered ? 2.0 : 1.5)
        const radGrad = ctx.createRadialGradient(node.x, node.y, node.radius * 0.3, node.x, node.y, glowRadius)
        radGrad.addColorStop(0, isLabHit ? palette.hitGlow : p.glow)
        radGrad.addColorStop(1, palette.transparent)
        ctx.globalAlpha = isDimmed ? 0.05 : isSelected || isLabHit ? 0.95 : 0.5
        ctx.fillStyle = radGrad
        ctx.beginPath()
        ctx.arc(node.x, node.y, glowRadius, 0, Math.PI * 2)
        ctx.fill()

        ctx.globalAlpha = nodeAlpha
        ctx.fillStyle = isLabHit ? palette.ring : p.core
        ctx.beginPath()
        ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2)
        ctx.fill()

        if (isSelected || isHovered || isLabHit) {
          ctx.globalAlpha = 1
          ctx.strokeStyle = palette.ring
          ctx.lineWidth = isSelected || isLabHit ? 2.5 : 1.5
          ctx.beginPath()
          ctx.arc(node.x, node.y, node.radius + 3.5, 0, Math.PI * 2)
          ctx.stroke()
        }

        const showText = isSelected || isHovered || isNeighbor || isLabHit || (!selectedSimNode && (node.degree >= 3 || scale > 1.45))
        if (showText) {
          ctx.globalAlpha = isDimmed ? 0.3 : 0.95
          ctx.fillStyle = isSelected || isLabHit ? palette.selectedText : p.text
          ctx.font = `${isSelected || isLabHit ? 'bold ' : ''}${Math.max(10, Math.min(14, 11 / Math.sqrt(scale)))}px sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'top'
          const label = node.raw.name.length > 14 ? `${node.raw.name.slice(0, 13)}…` : node.raw.name
          ctx.fillText(label, node.x, node.y + node.radius + 4)
        }
      })

      ctx.restore()
      ctx.restore()
      requestAnimationFrame(render)
    }

    const animId = requestAnimationFrame(render)
    return () => {
      isRunning = false
      cancelAnimationFrame(animId)
    }
  }, [activeFilterType, hoveredNode, isSimulating, nodes, pathEdgeIds, reducedMotion, selectedRef, simData, stepPhysics, highlightTags])

  // 鼠标交互
  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top

    if (isDraggingRef.current) {
      const dx = mouseX - dragStartRef.current.x
      const dy = mouseY - dragStartRef.current.y
      dragStartRef.current = { x: mouseX, y: mouseY }
      transformRef.current.x += dx
      transformRef.current.y += dy
      return
    }

    const { x: panX, y: panY, scale } = transformRef.current
    const worldX = (mouseX - panX) / scale
    const worldY = (mouseY - panY) / scale

    let bestNode: TagGraphNode | null = null
    let bestDist = 20 / scale
    for (const node of simData.nodes) {
      const dx = node.x - worldX
      const dy = node.y - worldY
      const dist = Math.sqrt(dx * dx + dy * dy)
      if (dist < node.radius + 8 / scale && dist < bestDist) {
        bestDist = dist
        bestNode = node.raw
      }
    }
    setHoveredNode(bestNode)
  }

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    isDraggingRef.current = true
    dragStartRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  const handleMouseUp = (e: React.MouseEvent<HTMLCanvasElement>) => {
    isDraggingRef.current = false
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top
    const { x: panX, y: panY, scale } = transformRef.current
    const worldX = (mouseX - panX) / scale
    const worldY = (mouseY - panY) / scale

    for (const node of simData.nodes) {
      const dx = node.x - worldX
      const dy = node.y - worldY
      if (Math.sqrt(dx * dx + dy * dy) < node.radius + 8 / scale) {
        onSelect(node.raw)
        return
      }
    }
  }

  const handleWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    e.preventDefault()
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top
    const zoomFactor = e.deltaY < 0 ? 1.14 : 0.88
    const current = transformRef.current
    const newScale = Math.max(0.25, Math.min(4.5, current.scale * zoomFactor))

    current.x = mouseX - (mouseX - current.x) * (newScale / current.scale)
    current.y = mouseY - (mouseY - current.y) * (newScale / current.scale)
    current.scale = newScale
  }

  const zoomStep = (factor: number) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const cx = rect.width / 2
    const cy = rect.height / 2
    const current = transformRef.current
    const newScale = Math.max(0.25, Math.min(4.5, current.scale * factor))
    current.x = cx - (cx - current.x) * (newScale / current.scale)
    current.y = cy - (cy - current.y) * (newScale / current.scale)
    current.scale = newScale
  }

  if (isMobile) {
    return (
      <div className="grid gap-2" data-tag-graph-mode="list" aria-label="标签关系图移动端列表">
        {[...nodes]
          .sort((a, b) => b.in_degree + b.out_degree - (a.in_degree + a.out_degree))
          .map((node) => (
            <button
              key={node.id}
              type="button"
              className={cn(
                'rounded-lg border bg-card p-3 text-left transition-colors',
                selectedRef === node.ref && 'border-primary ring-1 ring-primary/30'
              )}
              onClick={() => onSelect(node)}
            >
              <span className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate font-medium">{node.name}</span>
                <Badge variant="outline">{node.type}</Badge>
              </span>
              <span className="mt-2 grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                <span>记忆 {node.memory_count}</span>
                <span>入度 {node.in_degree}</span>
                <span>出度 {node.out_degree}</span>
              </span>
            </button>
          ))}
        <p className="text-xs text-muted-foreground">移动端已降级为列表视图。</p>
        {reducedMotion ? <p role="status" className="text-xs text-muted-foreground">已遵循减少动态效果偏好</p> : null}
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className={cn(
        'overflow-hidden border border-sky-950/80 bg-[#07101b] shadow-[0_24px_80px_rgba(2,8,23,.36)] transition-all duration-300',
        isFullscreen
          ? 'fixed inset-0 z-50 h-[100vh] w-[100vw] rounded-none'
          : 'relative h-[38rem] w-full rounded-2xl'
      )}
      data-tag-graph-mode="neural-canvas svg"
    >
      <canvas
        ref={canvasRef}
        className="block h-full w-full cursor-grab active:cursor-grabbing"
        aria-label="Tag 关系图画布"
        onMouseMove={handleMouseMove}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        onWheel={handleWheel}
      />

      {/* 画布左上角常驻：HUD 操作栏 (全屏时仍然完全可见！) */}
      <div className="absolute left-4 top-4 z-20 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setShowConfig(!showConfig)}
          className={cn(
            'flex items-center gap-1.5 rounded-xl border px-3 py-2 text-xs font-medium shadow-lg backdrop-blur-xl transition-all',
            showConfig
              ? 'border-sky-400 bg-sky-950/90 text-white ring-1 ring-sky-400/30'
              : 'border-sky-300/20 bg-slate-950/80 text-slate-200 hover:border-sky-400/50 hover:bg-slate-900 hover:text-white'
          )}
        >
          <SlidersHorizontalIcon className="size-3.5 text-sky-400" />
          <span>图谱与算法配置</span>
        </button>

        <div className="flex items-center gap-2 rounded-xl border border-sky-300/10 bg-slate-950/70 px-3 py-2 text-xs text-slate-300 shadow-lg backdrop-blur-xl">
          <SparklesIcon className="size-3.5 text-sky-400" />
          <span className="font-mono text-sky-300">{nodes.length} 节点</span>
          <span className="font-mono text-slate-400">{edges.length} 突触</span>
        </div>
        {reducedMotion ? <Badge role="status" variant="secondary">已遵循减少动态效果偏好</Badge> : null}
      </div>

      {/* 画布内嵌悬浮配置面板 (参考神经云图 #kg-config，全屏模式下依然触手可及！) */}
      {showConfig ? (
        <div
          className="absolute left-4 top-16 z-30 w-84 max-h-[calc(100%-5rem)] overflow-y-auto rounded-2xl border border-sky-500/25 bg-slate-950/90 p-4 text-xs shadow-2xl backdrop-blur-2xl animate-in fade-in slide-in-from-top-2 duration-150 text-slate-200"
          style={{ width: '22rem' }}
        >
          <div className="flex items-center justify-between border-b border-white/10 pb-2.5 mb-3">
            <div className="flex items-center gap-1.5">
              <span className="font-semibold text-sm text-foreground">图谱与算法控制台</span>
            </div>
            <button
              type="button"
              onClick={() => setShowConfig(false)}
              className="text-slate-400 hover:text-white p-1 rounded-md transition"
              title="关闭面板"
            >
              <XIcon className="size-4" />
            </button>
          </div>

          {/* 选项卡切换：视图参数 vs 算法实验室 */}
          <div className="grid grid-cols-2 gap-1 rounded-lg border border-white/10 bg-slate-900/60 p-1 mb-3">
            <button
              type="button"
              onClick={() => setActiveTab('view')}
              className={cn(
                'py-1.5 text-center text-xs font-medium rounded-md transition',
                activeTab === 'view' ? 'bg-sky-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'
              )}
            >
              🌌 视图与突触
            </button>
            <button
              type="button"
              onClick={() => setActiveTab('lab')}
              className={cn(
                'py-1.5 text-center text-xs font-medium rounded-md transition',
                activeTab === 'lab' ? 'bg-sky-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'
              )}
            >
              🧪 算法实验室
            </button>
          </div>

          {activeTab === 'view' ? (
            <div className="space-y-4">
              {/* 1. 节点数量 */}
              <div className="space-y-1">
                <div className="flex justify-between items-center text-xs">
                  <span className="font-medium text-slate-200">展示节点数量 (Max Nodes)</span>
                  <span className="font-mono text-sky-400 font-bold">{draftNodes}</span>
                </div>
                <p className="text-[11px] text-slate-400 leading-relaxed">
                  显示当前群内活跃度最高的多少个标签。调大展现完整星云，调小聚焦核心骨干。
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <input
                    type="range"
                    min="10"
                    max="2000"
                    step="10"
                    value={draftNodes}
                    onChange={(e) => setDraftNodes(Number(e.target.value))}
                    className="h-1.5 flex-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-sky-400"
                  />
                  <Input
                    type="number"
                    min="1"
                    className="h-7 w-20 px-1.5 text-right font-mono text-xs bg-slate-900 border-white/10"
                    value={draftNodes}
                    onChange={(e) => setDraftNodes(Number(e.target.value))}
                  />
                </div>
              </div>

              {/* 2. 置信度门限 */}
              <div className="space-y-1">
                <div className="flex justify-between items-center text-xs">
                  <span className="font-medium text-slate-200">置信度门限 (Min Confidence)</span>
                  <span className="font-mono text-purple-400 font-bold">{draftConfidence.toFixed(2)}</span>
                </div>
                <p className="text-[11px] text-slate-400 leading-relaxed">
                  过滤大模型提取时不够确信的低质量杂词，只保留高可信度核心关联。
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={draftConfidence}
                    onChange={(e) => setDraftConfidence(Number(e.target.value))}
                    className="h-1.5 flex-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-purple-400"
                  />
                  <Input
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    className="h-7 w-20 px-1.5 text-right font-mono text-xs bg-slate-900 border-white/10"
                    value={draftConfidence}
                    onChange={(e) => setDraftConfidence(Number(e.target.value))}
                  />
                </div>
              </div>

              {/* 3. 脉冲流动与半衰期 */}
              <div className="space-y-1">
                <div className="flex justify-between items-center text-xs">
                  <span className="font-medium text-slate-200">脉冲半衰期 (Pulse Hours)</span>
                  <span className="font-mono text-amber-400 font-bold">{draftHalfLife}h</span>
                </div>
                <p className="text-[11px] text-slate-400 leading-relaxed">
                  让近期刚刚讨论过的活跃话题在突触连线上流动发光。半衰期越短，脉冲衰退越快。
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <input
                    type="range"
                    min="12"
                    max="360"
                    step="12"
                    value={draftHalfLife}
                    onChange={(e) => setDraftHalfLife(Number(e.target.value))}
                    className="h-1.5 flex-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-amber-400"
                  />
                  <Input
                    type="number"
                    min="1"
                    className="h-7 w-20 px-1.5 text-right font-mono text-xs bg-slate-900 border-white/10"
                    value={draftHalfLife}
                    onChange={(e) => setDraftHalfLife(Number(e.target.value))}
                  />
                </div>
              </div>

              {/* 快捷预设 */}
              <div className="pt-1">
                <span className="text-[11px] text-slate-400 block mb-1.5">快速预设</span>
                <div className="grid grid-cols-3 gap-1.5">
                  <button
                    type="button"
                    className="p-1 text-[10px] rounded border border-white/10 bg-slate-900/60 hover:bg-slate-800 text-center"
                    onClick={() => { setDraftNodes(100); setDraftConfidence(0.4); }}
                  >
                    ⚡ 核心紧凑
                  </button>
                  <button
                    type="button"
                    className="p-1 text-[10px] rounded border border-white/10 bg-slate-900/60 hover:bg-slate-800 text-center"
                    onClick={() => { setDraftNodes(300); setDraftConfidence(0.0); }}
                  >
                    🌌 标准平衡
                  </button>
                  <button
                    type="button"
                    className="p-1 text-[10px] rounded border border-white/10 bg-slate-900/60 hover:bg-slate-800 text-center"
                    onClick={() => { setDraftNodes(1000); setDraftConfidence(0.0); }}
                  >
                    🪐 全景深空
                  </button>
                </div>
              </div>

              <Button
                type="button"
                className="w-full bg-sky-600 hover:bg-sky-500 text-white font-medium text-xs mt-2"
                onClick={() => {
                  onParamsChange({
                    maxNodes: Math.max(10, draftNodes),
                    minConfidence: Math.max(0, Math.min(1, draftConfidence)),
                    pulseHalfLifeHours: Math.max(1, draftHalfLife),
                  })
                  setShowConfig(false)
                }}
              >
                应用视图配置
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="rounded-lg bg-sky-950/40 border border-sky-500/20 p-2.5">
                <div className="flex items-center gap-1.5 text-sky-300 font-semibold mb-1">
                  <FlaskConicalIcon className="size-3.5" />
                  <span>基于本图的拓扑联想检索</span>
                </div>
                <p className="text-[11px] text-slate-400 leading-relaxed">
                  机器人每次回复时，就是沿着当前星云的突触进行拓扑联想的。输入一句话，图谱上的命中节点将实时发光！
                </p>
              </div>

              {/* 查询输入 */}
              <div className="space-y-1">
                <span className="text-[11px] text-slate-300 font-medium">测试查询语句</span>
                <div className="flex gap-1.5">
                  <Input
                    placeholder="输入问题或话题..."
                    value={labText}
                    onChange={(e) => setLabText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleRunLabQuery() }}
                    className="h-8 text-xs bg-slate-900 border-white/10"
                  />
                  <Button
                    type="button"
                    size="sm"
                    disabled={labLoading || !labText.trim()}
                    onClick={handleRunLabQuery}
                    className="h-8 px-2.5 bg-sky-600 hover:bg-sky-500 text-white shrink-0 text-xs"
                  >
                    {labLoading ? <Loader2Icon className="size-3.5 animate-spin" /> : <SearchIcon className="size-3.5" />}
                    <span>联想</span>
                  </Button>
                </div>
              </div>

              {/* 算法阶段开关 */}
              <div className="space-y-1 pt-1">
                <span className="text-[11px] text-slate-300 font-medium block">高阶算法阶段 (自由开关)</span>
                <div className="space-y-1.5 rounded-lg border border-white/5 bg-slate-900/40 p-2 text-[11px]">
                  <label className="flex items-center justify-between cursor-pointer">
                    <span className="text-slate-300">⚡ 脉冲共现扩散 (Spike)</span>
                    <Switch
                      checked={labStages.spike}
                      onCheckedChange={(checked) => setLabStages((prev) => ({ ...prev, spike: checked }))}
                    />
                  </label>
                  <p className="text-[10px] text-slate-500">顺着当前星云突触多跳联想，思路更活跃</p>

                  <label className="flex items-center justify-between cursor-pointer pt-1 border-t border-white/5">
                    <span className="text-slate-300">🌐 测地线重排 (Geodesic)</span>
                    <Switch
                      checked={labStages.geodesic}
                      onCheckedChange={(checked) => setLabStages((prev) => ({ ...prev, geodesic: checked }))}
                    />
                  </label>
                  <p className="text-[10px] text-slate-500">利用局部拓扑拉回向量偏差，抑制幻觉</p>

                  <label className="flex items-center justify-between cursor-pointer pt-1 border-t border-white/5">
                    <span className="text-slate-300">📐 自省投影修正 (EPA)</span>
                    <Switch
                      checked={labStages.epa}
                      onCheckedChange={(checked) => setLabStages((prev) => ({ ...prev, epa: checked }))}
                    />
                  </label>
                  <p className="text-[10px] text-slate-500">根据问话倾向动态调整几何匹配距离</p>

                  <label className="flex items-center justify-between cursor-pointer pt-1 border-t border-white/5">
                    <span className="text-slate-300">🔺 残差多阶金字塔 (Pyramid)</span>
                    <Switch
                      checked={labStages.pyramid}
                      onCheckedChange={(checked) => setLabStages((prev) => ({ ...prev, pyramid: checked }))}
                    />
                  </label>
                  <p className="text-[10px] text-slate-500">多层差分检索，化解复杂多主语问题</p>
                </div>
              </div>

              {/* 检索命中结果展示 */}
              {labResult?.results?.length ? (
                <div className="space-y-1.5 pt-2 border-t border-white/10">
                  <div className="flex justify-between items-center text-xs">
                    <span className="font-semibold text-emerald-400">✨ 命中关联记忆 ({labResult.results.length})</span>
                    {highlightTags.size > 0 ? (
                      <button
                        type="button"
                        className="text-[10px] text-sky-400 hover:underline"
                        onClick={() => setHighlightTags(new Set())}
                      >
                        清除高亮
                      </button>
                    ) : null}
                  </div>
                  <div className="max-h-40 overflow-y-auto space-y-1.5 pr-1">
                    {labResult.results.map((m, idx) => (
                      <div key={idx} className="p-2 rounded bg-slate-900/80 border border-white/5 text-[11px]">
                        <div className="flex justify-between text-slate-400 text-[10px] mb-0.5">
                          <span>{String(m.sender_name || '记忆')}</span>
                          <span className="font-mono text-sky-400">
                            {typeof m.score === 'number' ? `${(m.score * 100).toFixed(0)}% 契合` : '已召回'}
                          </span>
                        </div>
                        <p className="line-clamp-2 text-slate-200">{String(m.content || '')}</p>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      ) : null}

      {/* 右上角醒目的全屏探索 / 退出全屏切换按钮 */}
      <button
        type="button"
        onClick={() => setIsFullscreen(!isFullscreen)}
        className="absolute right-4 top-4 z-10 flex items-center gap-1.5 rounded-xl border border-sky-300/20 bg-slate-950/80 px-3 py-2 text-xs font-medium text-slate-200 shadow-lg backdrop-blur-xl transition-all hover:border-sky-400/50 hover:bg-slate-900 hover:text-white active:scale-95"
        aria-label={isFullscreen ? '退出全屏' : '全屏探索'}
        title={isFullscreen ? '退出全屏 (Esc)' : '全屏沉浸探索'}
      >
        {isFullscreen ? <Minimize2Icon className="size-3.5 text-sky-400" /> : <Maximize2Icon className="size-3.5 text-sky-400" />}
        <span>{isFullscreen ? '退出全屏 (Esc)' : '全屏探索'}</span>
      </button>

      {/* 悬浮控制工具栏 (左下角) */}
      <div className="absolute bottom-4 left-4 flex items-center gap-1 rounded-xl border border-sky-300/10 bg-slate-950/70 p-1.5 shadow-lg backdrop-blur-xl z-10">
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className="text-slate-300 hover:text-white"
          title="放大"
          onClick={() => zoomStep(1.2)}
        >
          <ZoomInIcon className="size-3.5" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className="text-slate-300 hover:text-white"
          title="缩小"
          onClick={() => zoomStep(0.82)}
        >
          <ZoomOutIcon className="size-3.5" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className="text-slate-300 hover:text-white"
          title="复位视角"
          onClick={resetView}
        >
          <RotateCcwIcon className="size-3.5" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className={cn('text-slate-300 hover:text-white', isSimulating && 'text-sky-400')}
          title={isSimulating ? '暂停动力仿真' : '启动动力仿真'}
          onClick={() => setIsSimulating(!isSimulating)}
        >
          {isSimulating ? <PauseIcon className="size-3.5" /> : <PlayIcon className="size-3.5" />}
        </Button>
      </div>

      {/* 类型图例：点击类型可自由高亮/筛选该类别 */}
      {legend?.enabled !== false && legendItems.length > 0 ? (
        <ul className="absolute bottom-4 right-4 flex max-w-[calc(100%-2rem)] flex-col gap-1 rounded-xl border border-sky-300/10 bg-slate-950/80 px-3 py-2 text-[11px] text-slate-300 shadow-lg backdrop-blur-xl z-10" aria-label="节点类型图例">
          <div className="flex items-center justify-between text-[10px] text-slate-400 mb-0.5 border-b border-white/5 pb-1">
            <span>类型透镜</span>
            {activeFilterType ? (
              <button
                type="button"
                className="text-sky-400 hover:text-white transition-colors"
                onClick={() => setActiveFilterType(null)}
              >
                重置全显
              </button>
            ) : (
              <span className="text-slate-500">点击过滤</span>
            )}
          </div>
          {legendItems.map((item) => {
            const isFilterActive = activeFilterType === item.type.toLowerCase()
            return (
              <li key={item.type}>
                <button
                  type="button"
                  className={cn(
                    'flex items-center gap-1.5 w-full text-left rounded px-1 py-0.5 transition-all',
                    isFilterActive
                      ? 'bg-white/10 text-white font-medium ring-1 ring-white/20'
                      : 'hover:bg-white/5 text-slate-300'
                  )}
                  onClick={() => setActiveFilterType(isFilterActive ? null : item.type.toLowerCase())}
                  title={`点击只看 ${item.label} 类型标签`}
                >
                  <span
                    className={cn('size-2 rounded-full transition-transform', isFilterActive && 'scale-125')}
                    style={{ backgroundColor: item.color }}
                    aria-hidden="true"
                  />
                  <span>{item.label}</span>
                  {legend?.show_count === false ? null : (
                    <span className="ml-auto font-mono text-[10px] text-slate-500">{item.count}</span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}

      <div className="sr-only" aria-label="可访问标签列表">
        {nodes.map((n) => (
          <button key={n.id} type="button" aria-label={`选择标签 ${n.name}`} onClick={() => onSelect(n)}>
            {n.name}
          </button>
        ))}
      </div>
    </div>
  )
}

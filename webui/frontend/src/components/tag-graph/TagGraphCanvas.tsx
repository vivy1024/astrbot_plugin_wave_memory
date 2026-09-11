import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2Icon, Minimize2Icon, PlayIcon, PauseIcon, RotateCcwIcon, ZoomInIcon, ZoomOutIcon, SparklesIcon } from 'lucide-react'

import type { TagGraphEdge, TagGraphNode, TagGraphPayload } from '@/api/tagGraph'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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

/**
 * 图谱配色：深空观测台风格，按节点类型区分色相。
 * 颜色集中在此处，避免散落在绘制逻辑里；future 可改为从配置读取。
 */
export interface NodePalette {
  core: string
  glow: string
  text: string
}

export const TYPE_PALETTES: Record<string, NodePalette> = {
  keyword: { core: '#38bdf8', glow: 'rgba(56, 189, 248, 0.35)', text: '#bae6fd' },
  entity: { core: '#a78bfa', glow: 'rgba(167, 139, 250, 0.35)', text: '#ddd6fe' },
  topic: { core: '#34d399', glow: 'rgba(52, 211, 153, 0.35)', text: '#a7f3d0' },
  emotion: { core: '#fbbf24', glow: 'rgba(251, 191, 36, 0.35)', text: '#fde68a' },
  fact: { core: '#f472b6', glow: 'rgba(244, 114, 182, 0.35)', text: '#fbcfe8' },
  jargon: { core: '#fb7185', glow: 'rgba(251, 113, 133, 0.35)', text: '#fecdd3' },
  default: { core: '#94a3b8', glow: 'rgba(148, 163, 184, 0.30)', text: '#e2e8f0' },
}

export function paletteFor(type: string): NodePalette {
  return TYPE_PALETTES[type.toLowerCase()] ?? TYPE_PALETTES.default
}

/** 图例里用的中文类型名，与 TYPE_PALETTES 的键一一对应。 */
export const TYPE_LABELS: Record<string, string> = {
  keyword: '关键词',
  entity: '实体',
  topic: '话题',
  emotion: '情绪',
  fact: '事实',
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

/**
 * 按配置整理图例项：顺序取自服务端配置，只保留图谱里实际出现的类型。
 * 配置为空 => 按节点中出现顺序展示全部类型（旧行为兜底）。
 */
export function buildLegendItems(
  nodes: Array<{ type?: string }>,
  legend?: { types?: string[] } | null,
): LegendItem[] {
  const counts = new Map<string, number>()
  for (const node of nodes) {
    const key = (node.type || 'default').toLowerCase()
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  const configured = (legend?.types ?? []).map((item) => item.toLowerCase())
  const order = configured.length ? configured : [...counts.keys()]
  const seen = new Set<string>()
  const items: LegendItem[] = []
  for (const type of order) {
    const count = counts.get(type)
    // 配置里列了图里没有的类型就不展示，避免出现 0 个的图例项误导。
    if (seen.has(type) || !count) continue
    seen.add(type)
    items.push({ type, label: typeLabel(type), count, color: paletteFor(type).core })
  }
  return items
}

/** 深空背景、网格与连线配色：固定观测台色板，不随外部主题变化。 */
interface GraphPalette {
  background: string
  backgroundMid: string
  backgroundOuter: string
  grid: string
  edgeCooccurrence: string
  edgeRelations: string
  pulse: string
  ring: string
}

function readPalette(): GraphPalette {
  return {
    background: '#10233a',
    backgroundMid: '#091421',
    backgroundOuter: '#050a12',
    grid: 'rgba(125, 211, 252, 0.035)',
    edgeCooccurrence: '#67e8f9',
    edgeRelations: '#c4b5fd',
    pulse: '#ffffff',
    ring: '#ffffff',
  }
}


// 世界坐标尺寸：布局与视口复位共用同一常量，避免缩放漂移。
const WORLD_WIDTH = 1200
const WORLD_HEIGHT = 800
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

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
  onSelect: (node: TagGraphNode) => void
}

export function TagGraphCanvas({
  nodes,
  edges,
  selectedRef,
  pathEdgeIds = new Set(),
  legend,
  onSelect,
}: TagGraphCanvasProps) {
  const isMobile = useIsMobile()
  const reducedMotion = usePrefersReducedMotion()
  const legendItems = useMemo(() => buildLegendItems(nodes, legend), [nodes, legend])
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const [isFullscreen, setIsFullscreen] = useState(false)
  const [isSimulating, setIsSimulating] = useState(false)
  const [hoveredNode, setHoveredNode] = useState<TagGraphNode | null>(null)
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null)

  // 视角变换矩阵状态
  const transformRef = useRef({ x: 0, y: 0, scale: 1.0 })
  const isDraggingRef = useRef(false)
  const dragStartRef = useRef({ x: 0, y: 0 })
  const animFrameRef = useRef<number | null>(null)
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
    const total = Math.max(1, sorted.length)
    // 覆盖可视范围的最大半径：按世界高度留边，横向用椭圆铺满更宽的画布。
    const maxRadius = Math.min(width, height) * 0.46

    sorted.forEach((node, index) => {
      const degree = node.in_degree + node.out_degree
      const radius = Math.max(9, Math.min(24, 8 + Math.sqrt(degree + node.memory_count) * 2))
      // 确定性中心螺旋（phyllotaxis）：度数越高越靠中心，节点均匀铺满画面，刷新后位置稳定不跳动。
      const rr = maxRadius * Math.sqrt((index + 0.5) / total)
      const theta = index * GOLDEN_ANGLE
      const x = centerX + Math.cos(theta) * rr * (width / Math.min(width, height)) * 0.68
      const y = centerY + Math.sin(theta) * rr

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
      if (source && target && source !== target) {
        simEdges.push({
          id: edge.id,
          source,
          target,
          weight: Math.max(0.1, Math.min(1.0, edge.confidence || edge.weight || 0.5)),
          layer: edge.layer,
          pulseEnergy: edge.pulse_energy ?? 0,
        })
      }
    })

    return { nodes: Array.from(nodeMap.values()), edges: simEdges, nodeMap, width, height }
  }, [nodes, edges])

  // 视口复位居中
  const resetView = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    // 画布尚未布局（在隐藏 Tab 内 rect≈0）时跳过，交给 ResizeObserver 在真正可见后重算。
    if (rect.width < 2 || rect.height < 2) return
    const scale = Math.max(0.4, Math.min(1.8, Math.min(rect.width / WORLD_WIDTH, rect.height / WORLD_HEIGHT) * 0.92))
    transformRef.current = {
      x: (rect.width - WORLD_WIDTH * scale) / 2,
      y: (rect.height - WORLD_HEIGHT * scale) / 2,
      scale,
    }
  }, [])

  useEffect(() => {
    if (isMobile) return
    const canvas = canvasRef.current
    resetView()
    if (!canvas || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => resetView())
    observer.observe(canvas)
    // 监听窗口尺寸变化与全屏动画过渡完成（350ms后），确保全屏与退出演示完全对齐视口
    const t1 = setTimeout(() => resetView(), 100)
    const t2 = setTimeout(() => resetView(), 350)
    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
      observer.disconnect()
    }
  }, [isMobile, isFullscreen, resetView, simData])

  // 支持 Escape 键一键退出全屏
  useEffect(() => {
    if (!isFullscreen) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsFullscreen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isFullscreen])

  // 物理步进计算 (Force-Directed Simulation Step)
  const stepPhysics = useCallback(() => {
    const { nodes: simNodes, edges: simEdges, width, height } = simData
    const centerX = width / 2
    const centerY = height / 2

    // 1. 节点排斥力 (N^2 节点距离斥力)
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

      // 2. 居中引力 (Centering gravity)
      const cdx = centerX - na.x
      const cdy = centerY - na.y
      na.vx += cdx * 0.003
      na.vy += cdy * 0.003
    }

    // 3. 弹簧连线引力 (Spring attraction)
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

    // 4. 更新位置与阻尼阻力 (Damping)
    let totalKinetic = 0
    for (let i = 0; i < simNodes.length; i++) {
      const node = simNodes[i]
      node.vx *= 0.86
      node.vy *= 0.86
      node.x += Math.max(-12, Math.min(12, node.vx))
      node.y += Math.max(-12, Math.min(12, node.vy))
      totalKinetic += Math.abs(node.vx) + Math.abs(node.vy)
    }

    // 动能衰减至阈值时自动休眠
    if (totalKinetic < 0.25 && simNodes.length > 5) {
      setIsSimulating(false)
    }
  }, [simData])

  // 主画布渲染循环 (60 FPS Render Loop)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let isRunning = true
    // 主题 token 每次进入渲染循环时解析一次；样式表变更由 effect 依赖重建。
    const palette = readPalette()

    const render = () => {
      if (!isRunning) return

      // 物理布局仅在用户显式点击播放时运行；默认保持静止便于阅读。
      if (isSimulating && !reducedMotion) {
        stepPhysics()
      }

      // 脉冲波相位推进
      pulsePhaseRef.current = (pulsePhaseRef.current + 0.016) % 1.0

      const dpr = window.devicePixelRatio || 1
      const rect = canvas.getBoundingClientRect()
      if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
        canvas.width = rect.width * dpr
        canvas.height = rect.height * dpr
      }

      ctx.save()
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, rect.width, rect.height)

      // 深空观测台背景：低对比度星尘与细网格只提供空间感，不抢数据层注意力。
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
      ctx.lineWidth = 1
      for (let x = 0; x < rect.width; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, rect.height); ctx.stroke() }
      for (let y = 0; y < rect.height; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(rect.width, y); ctx.stroke() }

      // 应用视口矩阵 (Pan & Zoom)
      const { x: panX, y: panY, scale } = transformRef.current
      ctx.save()
      ctx.translate(panX, panY)
      ctx.scale(scale, scale)

      const activeRef = selectedRef ?? hoveredNode?.ref ?? null
      const selectedSimNode = activeRef ? simData.nodes.find((n) => n.raw.ref === activeRef) : null

      // 计算当前活跃节点的一跳邻居集合 (Neighbor Highlighting)
      const neighborIds = new Set<string>()
      if (selectedSimNode) {
        neighborIds.add(selectedSimNode.id)
        simData.edges.forEach((edge) => {
          if (edge.source.id === selectedSimNode.id) neighborIds.add(edge.target.id)
          if (edge.target.id === selectedSimNode.id) neighborIds.add(edge.source.id)
        })
      }

      // 1. 绘制连线光纤 (Edges)
      simData.edges.forEach((edge) => {
        const isConnectedToActive = selectedSimNode
          ? edge.source.id === selectedSimNode.id || edge.target.id === selectedSimNode.id
          : false
        const isPathEdge = pathEdgeIds.has(edge.id)

        let strokeColor = edge.layer === 'relations' ? palette.edgeRelations : palette.edgeCooccurrence
        let lineWidth = isPathEdge ? 3.2 : Math.max(0.7, edge.weight * 1.8)
        let alpha = isPathEdge ? 0.95 : Math.max(0.08, Math.min(0.42, edge.weight * 0.52))

        if (selectedSimNode) {
          if (!isConnectedToActive && !isPathEdge) {
            alpha = 0.04 // 无关连线极暗化
          } else {
            alpha = Math.max(alpha, 0.85)
            lineWidth = Math.max(lineWidth, 2.2)
          }
        }

        ctx.strokeStyle = strokeColor
        ctx.globalAlpha = alpha
        ctx.lineWidth = lineWidth
        ctx.setLineDash(edge.layer === 'relations' ? [6, 4] : [])

        ctx.beginPath()
        ctx.moveTo(edge.source.x, edge.source.y)
        ctx.lineTo(edge.target.x, edge.target.y)
        ctx.stroke()
        ctx.setLineDash([])

        // 绘制脉冲光球动画 (Spike Energy Motion)
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

      // 2. 绘制发光节点星体 (Nodes)
      simData.nodes.forEach((node) => {
        const isSelected = node.raw.ref === selectedRef
        const isHovered = node.raw.ref === hoveredNode?.ref
        const isNeighbor = neighborIds.has(node.id)
        const isDimmed = selectedSimNode && !isNeighbor

        const p = paletteFor(node.raw.type)
        const nodeAlpha = isDimmed ? 0.18 : 0.92

        // 外圈光晕 (Radial Glow)
        const glowRadius = node.radius * (isSelected ? 2.4 : isHovered ? 2.0 : 1.5)
        const radGrad = ctx.createRadialGradient(node.x, node.y, node.radius * 0.3, node.x, node.y, glowRadius)
        radGrad.addColorStop(0, p.glow)
        radGrad.addColorStop(1, 'rgba(0,0,0,0)')
        ctx.globalAlpha = isDimmed ? 0.05 : isSelected ? 0.9 : 0.5
        ctx.fillStyle = radGrad
        ctx.beginPath()
        ctx.arc(node.x, node.y, glowRadius, 0, Math.PI * 2)
        ctx.fill()

        // 实体核心 (Star Core)
        ctx.globalAlpha = nodeAlpha
        ctx.fillStyle = p.core
        ctx.beginPath()
        ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2)
        ctx.fill()

        // 选中高亮光环 (Selection Ring)
        if (isSelected || isHovered) {
          ctx.globalAlpha = 1
          ctx.strokeStyle = palette.ring
          ctx.lineWidth = isSelected ? 2.5 : 1.5
          ctx.beginPath()
          ctx.arc(node.x, node.y, node.radius + 3.5, 0, Math.PI * 2)
          ctx.stroke()
        }

        // 标签文字 (Star Name Text)
        const showText = isSelected || isHovered || isNeighbor || (!selectedSimNode && (node.degree >= 3 || scale > 1.45))
        if (showText) {
          ctx.globalAlpha = isDimmed ? 0.3 : 0.95
          ctx.fillStyle = isSelected ? '#ffffff' : p.text
          ctx.font = `${isSelected ? 'bold ' : ''}${Math.max(10, Math.min(14, 11 / Math.sqrt(scale)))}px sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'top'
          const label = node.raw.name.length > 14 ? `${node.raw.name.slice(0, 13)}…` : node.raw.name
          ctx.fillText(label, node.x, node.y + node.radius + 4)
        }
      })

      ctx.restore()
      ctx.restore()

      animFrameRef.current = requestAnimationFrame(render)
    }

    animFrameRef.current = requestAnimationFrame(render)

    return () => {
      isRunning = false
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current)
      }
    }
  }, [isSimulating, reducedMotion, stepPhysics, simData, selectedRef, hoveredNode, pathEdgeIds])

  // 坐标反投影：从屏幕坐标转换到模拟器世界坐标
  const screenToWorld = useCallback((screenX: number, screenY: number) => {
    const canvas = canvasRef.current
    if (!canvas) return { x: 0, y: 0 }
    const rect = canvas.getBoundingClientRect()
    const localX = screenX - rect.left
    const localY = screenY - rect.top
    const { x: panX, y: panY, scale } = transformRef.current
    return {
      x: (localX - panX) / scale,
      y: (localY - panY) / scale,
    }
  }, [])

  // 鼠标悬停拾取检测 (Hit Testing)
  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (isDraggingRef.current) {
      const dx = e.clientX - dragStartRef.current.x
      const dy = e.clientY - dragStartRef.current.y
      transformRef.current.x += dx
      transformRef.current.y += dy
      dragStartRef.current = { x: e.clientX, y: e.clientY }
      return
    }

    const world = screenToWorld(e.clientX, e.clientY)
    const { scale } = transformRef.current
    // 带有容差的最近节点拾取
    let bestNode: TagGraphNode | null = null
    let minDist = 24 / scale

    for (const node of simData.nodes) {
      const dx = node.x - world.x
      const dy = node.y - world.y
      const dist = Math.sqrt(dx * dx + dy * dy)
      if (dist < node.radius + minDist) {
        bestNode = node.raw
        minDist = dist
      }
    }

    setHoveredNode(bestNode)
    setMousePos(bestNode ? { x: e.clientX, y: e.clientY } : null)
  }

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (e.button === 0 || e.button === 1) {
      isDraggingRef.current = true
      dragStartRef.current = { x: e.clientX, y: e.clientY }
    }
  }

  const handleMouseUp = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const wasDragging = isDraggingRef.current
    isDraggingRef.current = false

    // 如果只是点击（非长距离拖拽），触发节点选中
    const distMoved = Math.hypot(e.clientX - dragStartRef.current.x, e.clientY - dragStartRef.current.y)
    if (wasDragging && distMoved < 6) {
      const world = screenToWorld(e.clientX, e.clientY)
      for (const node of simData.nodes) {
        const dx = node.x - world.x
        const dy = node.y - world.y
        if (Math.hypot(dx, dy) <= node.radius + 12) {
          onSelect(node.raw)
          return
        }
      }
    }
  }

  // 鼠标滚轮缩放 (Zoom In/Out)
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

    // 以当前鼠标焦点为中心平滑缩放
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
        <p className="text-xs text-muted-foreground">移动端已降级为响应式卡片列表；所有筛选与详情依然生效。</p>
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className={cn(
        // `relative` 与 `fixed` 不能同时写在 class 里：Tailwind 中 `.relative`
        // 排在 `.fixed` 之后，会覆盖掉全屏定位。全屏时改用 fixed 分支。
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

      {/* 悬浮控制工具栏 */}
      <div className="absolute bottom-4 left-4 flex items-center gap-1 rounded-xl border border-sky-300/10 bg-slate-950/70 p-1.5 shadow-lg backdrop-blur-xl">
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
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className="text-slate-300 hover:text-white"
          title={isFullscreen ? '退出全屏' : '全屏探索'}
          onClick={() => setIsFullscreen(!isFullscreen)}
        >
          {isFullscreen ? <Minimize2Icon className="size-3.5" /> : <Maximize2Icon className="size-3.5" />}
        </Button>
      </div>

      {/* 状态徽章与图例 */}
      <div className="absolute left-4 top-4 flex max-w-[calc(100%-10rem)] flex-wrap items-center gap-2 rounded-xl border border-sky-300/10 bg-slate-950/70 px-3 py-2 text-xs text-slate-300 shadow-lg backdrop-blur-xl">
        <SparklesIcon className="size-3.5 text-sky-400" />
        <span>Tag 神经星云</span>
        <span className="text-slate-500">·</span>
        <span className="font-mono text-sky-300">{nodes.length} 节点</span>
        <span className="font-mono text-slate-400">{edges.length} 突触</span>
        <span className="text-slate-500">·</span>
        <span className="text-[11px] text-slate-400">滚轮缩放 / 拖拽平移 / 点击聚焦</span>
        {reducedMotion ? <span className="ml-2 text-amber-300">已遵循减少动态效果偏好</span> : null}
      </div>

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

      {/* 类型图例：顺序与显隐由配置决定，只展示图中实际出现的类型 */}
      {legend?.enabled !== false && legendItems.length > 0 ? (
        <ul className="absolute bottom-4 right-4 flex max-w-[calc(100%-2rem)] flex-col gap-1 rounded-xl border border-sky-300/10 bg-slate-950/70 px-3 py-2 text-[11px] text-slate-300 shadow-lg backdrop-blur-xl" aria-label="节点类型图例">
          {legendItems.map((item) => (
            <li key={item.type} className="flex items-center gap-1.5">
              <span className="size-2 rounded-full" style={{ backgroundColor: item.color }} aria-hidden="true" />
              <span>{item.label}</span>
              {legend?.show_count === false ? null : <span className="font-mono text-slate-500">{item.count}</span>}
            </li>
          ))}
        </ul>
      ) : null}

      {/* 供屏幕阅读器与测试环境的无障碍标签列表 */}
      <div className="sr-only" aria-label="可访问标签列表">
        {nodes.map((n) => (
          <button key={n.id} type="button" aria-label={`选择标签 ${n.name}`} onClick={() => onSelect(n)}>
            {n.name}
          </button>
        ))}
      </div>

      {/* 鼠标悬停标签 Tooltip */}
      {hoveredNode && mousePos && containerRef.current && (
        <div
          className="pointer-events-none fixed z-50 flex flex-col gap-1 rounded-md border border-sky-500/30 bg-slate-950/90 px-2.5 py-1.5 text-xs text-white shadow-xl backdrop-blur-md"
          style={{
            left: mousePos.x + 14,
            top: mousePos.y - 12,
          }}
        >
          <div className="flex items-center gap-1.5 font-medium">
            <span className="size-2 rounded-full" style={{ backgroundColor: paletteFor(hoveredNode.type).core }} />
            <span>{hoveredNode.name}</span>
            <Badge variant="outline" className="text-[9px] px-1 py-0">{hoveredNode.type}</Badge>
          </div>
          <div className="grid grid-cols-3 gap-2 font-mono text-[10px] text-slate-400">
            <span>记忆 {hoveredNode.memory_count}</span>
            <span>入度 {hoveredNode.in_degree}</span>
            <span>出度 {hoveredNode.out_degree}</span>
          </div>
        </div>
      )}
    </div>
  )
}

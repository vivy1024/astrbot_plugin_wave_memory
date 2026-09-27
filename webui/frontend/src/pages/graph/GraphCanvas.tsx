import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import Sigma from 'sigma'
import FA2LayoutSupervisor from 'graphology-layout-forceatlas2/worker'
import type { Settings } from 'sigma/settings'
import type { NodeDisplayData } from 'sigma/types'

import { GRAPH_CHROME, type GraphTheme } from '@/lib/graph-palette'
import { LayoutController, type LayoutStatus, type LayoutSupervisor } from '@/lib/graph/layout-controller'
import type { GraphEdgeAttributes, GraphNodeAttributes, RelationGraph } from '@/lib/graph/types'
import { cn } from '@/lib/utils'
import { neighborsOf, reduceEdge, reduceNode, type EdgeData, type NodeData, type ReducerState } from './graph-reducers'

export interface GraphCanvasHandle {
  /** 把相机移到节点上（节点必须在当前视图图里）。 */
  focusNode: (id: string) => void
  zoomIn: () => void
  zoomOut: () => void
  resetCamera: () => void
  /** 从当前坐标重新跑一次 ForceAtlas2。 */
  relayout: () => void
  stopLayout: () => void
}

export interface GraphCanvasProps {
  graph: RelationGraph
  theme: GraphTheme
  glow: boolean
  selectedNode: string | null
  /** 路径上的节点 id。 */
  pathNodes: ReadonlySet<string>
  /** 路径上的节点对（pairKey，与方向无关）。 */
  pathPairs: ReadonlySet<string>
  onSelectNode: (id: string | null) => void
  onLayoutStatus?: (status: LayoutStatus) => void
  className?: string
}

type GraphSettings = Settings<GraphNodeAttributes, GraphEdgeAttributes>

const LABEL_FONT = '"Geist Variable", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif'
const LABEL_SIZE = 12

function drawLabel(themeRef: { current: ReducerState }) {
  return (context: CanvasRenderingContext2D, data: Partial<NodeDisplayData> & { x: number; y: number; size: number }, settings: GraphSettings) => {
    if (!data.label) return
    const chrome = GRAPH_CHROME[themeRef.current.theme]
    const size = settings.labelSize
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`
    const x = data.x + data.size + 3
    const y = data.y + size / 3
    context.lineJoin = 'round'
    context.lineWidth = 3.5
    context.strokeStyle = chrome.labelHalo
    context.strokeText(data.label, x, y)
    context.fillStyle = chrome.label
    context.fillText(data.label, x, y)
  }
}

function drawHover(themeRef: { current: ReducerState }) {
  const label = drawLabel(themeRef)
  return (context: CanvasRenderingContext2D, data: Partial<NodeDisplayData> & { x: number; y: number; size: number }, settings: GraphSettings) => {
    const chrome = GRAPH_CHROME[themeRef.current.theme]
    const size = settings.labelSize
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`
    const padding = 4
    const textWidth = data.label ? context.measureText(data.label).width : 0
    const height = size + padding * 2
    const radius = Math.max(data.size, size / 2) + padding
    context.save()
    context.fillStyle = chrome.hoverBox
    context.strokeStyle = chrome.hoverBorder
    context.lineWidth = 1
    context.shadowColor = 'rgba(0,0,0,0.18)'
    context.shadowBlur = 8
    context.beginPath()
    if (data.label) {
      const left = data.x - radius
      const width = radius * 2 + textWidth + padding + 3
      context.roundRect(left, data.y - height / 2, width, height, height / 2)
    } else {
      context.arc(data.x, data.y, radius, 0, Math.PI * 2)
    }
    context.fill()
    context.shadowBlur = 0
    context.stroke()
    context.restore()
    label(context, data, settings)
  }
}

/**
 * sigma v3 WebGL 画布。渲染器只在挂载时创建一次：换视图 = setGraph，
 * hover / 选中 / 路径 / 主题 / 光晕 = 改 reducer 状态后 refresh({ skipIndexation: true })。
 * sigma 本身没有常驻动画循环，只有交互或布局写坐标时才请求重绘。
 */
export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(function GraphCanvas(
  { graph, theme, glow, selectedNode, pathNodes, pathPairs, onSelectNode, onLayoutStatus, className },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma<GraphNodeAttributes, GraphEdgeAttributes> | null>(null)
  const layoutRef = useRef<LayoutController | null>(null)
  const graphRef = useRef(graph)
  const onSelectRef = useRef(onSelectNode)
  const onLayoutRef = useRef(onLayoutStatus)
  onSelectRef.current = onSelectNode
  onLayoutRef.current = onLayoutStatus
  const stateRef = useRef<ReducerState>({
    theme,
    glow,
    hovered: null,
    hoveredNeighbors: new Set(),
    selected: selectedNode,
    selectedNeighbors: neighborsOf(graph, selectedNode),
    pathNodes,
    pathPairs,
  })

  const state = stateRef
  const refresh = () => sigmaRef.current?.refresh({ skipIndexation: true })

  const startLayout = () => {
    layoutRef.current?.stop('stopped')
    const target = graphRef.current
    const controller = new LayoutController(
      target,
      (layoutGraph, settings) => new FA2LayoutSupervisor(layoutGraph, { settings, getEdgeWeight: 'weight' }) as LayoutSupervisor,
      (status) => {
        if (layoutRef.current === controller || !status.running) onLayoutRef.current?.(status)
      },
    )
    layoutRef.current = controller
    controller.start()
  }

  // 渲染器：只创建一次
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const sigma = new Sigma<GraphNodeAttributes, GraphEdgeAttributes>(graphRef.current, container, {
      allowInvalidContainer: true,
      renderEdgeLabels: false,
      defaultNodeType: 'circle',
      defaultEdgeType: 'line',
      labelFont: LABEL_FONT,
      labelSize: LABEL_SIZE,
      labelWeight: '500',
      labelColor: { color: GRAPH_CHROME[state.current.theme].label },
      labelDensity: 0.55,
      labelGridCellSize: 96,
      labelRenderedSizeThreshold: 9,
      zIndex: true,
      stagePadding: 28,
      minCameraRatio: 0.04,
      maxCameraRatio: 4,
      nodeReducer: (node, data) => reduceNode(sigmaRef.current?.getGraph() as RelationGraph ?? graphRef.current, state.current, node, data as NodeData),
      edgeReducer: (edge, data) => reduceEdge(sigmaRef.current?.getGraph() as RelationGraph ?? graphRef.current, state.current, edge, data as EdgeData),
      defaultDrawNodeLabel: drawLabel(state) as GraphSettings['defaultDrawNodeLabel'],
      defaultDrawNodeHover: drawHover(state) as GraphSettings['defaultDrawNodeHover'],
    })
    sigmaRef.current = sigma
    // 仅开发模式：暴露给 Playwright 做渲染计数 / 调试，生产包里这段会被常量折叠掉
    if (import.meta.env.DEV) (window as unknown as { __wmGraphSigma?: unknown }).__wmGraphSigma = sigma

    sigma.on('enterNode', ({ node }) => {
      const current = sigma.getGraph() as RelationGraph
      state.current = { ...state.current, hovered: node, hoveredNeighbors: neighborsOf(current, node) }
      container.style.cursor = 'pointer'
      sigma.refresh({ skipIndexation: true })
    })
    sigma.on('leaveNode', () => {
      state.current = { ...state.current, hovered: null, hoveredNeighbors: new Set() }
      container.style.cursor = ''
      sigma.refresh({ skipIndexation: true })
    })
    sigma.on('clickNode', ({ node }) => onSelectRef.current(node))
    sigma.on('clickStage', () => onSelectRef.current(null))

    let frame = 0
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => sigma.resize())
    })
    observer?.observe(container)

    return () => {
      observer?.disconnect()
      cancelAnimationFrame(frame)
      layoutRef.current?.stop('stopped')
      layoutRef.current = null
      sigma.kill()
      sigmaRef.current = null
    }
    // 渲染器只创建一次；reducer 通过 ref 读取最新状态
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 换视图：替换图数据并重新布局，不重建渲染器
  useEffect(() => {
    graphRef.current = graph
    const sigma = sigmaRef.current
    if (!sigma) return
    state.current.hovered = null
    state.current.hoveredNeighbors = new Set()
    state.current.selectedNeighbors = neighborsOf(graph, state.current.selected)
    if (sigma.getGraph() !== graph) sigma.setGraph(graph)
    sigma.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 })
    startLayout()
    return () => layoutRef.current?.stop('stopped')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph])

  // 选中 / 路径 / 主题 / 光晕：只刷新 reducer
  useEffect(() => {
    state.current = {
      ...state.current,
      theme,
      glow,
      selected: selectedNode,
      selectedNeighbors: neighborsOf(graph, selectedNode),
      pathNodes,
      pathPairs,
    }
    const sigma = sigmaRef.current
    if (!sigma) return
    sigma.setSetting('labelColor', { color: GRAPH_CHROME[theme].label })
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, glow, selectedNode, pathNodes, pathPairs, graph])

  useImperativeHandle(ref, () => ({
    focusNode: (id: string) => {
      const sigma = sigmaRef.current
      const display = sigma?.getNodeDisplayData(id)
      if (!sigma || !display) return
      sigma.getCamera().animate({ x: display.x, y: display.y, ratio: 0.35 }, { duration: 450 })
    },
    zoomIn: () => { void sigmaRef.current?.getCamera().animatedZoom({ duration: 250 }) },
    zoomOut: () => { void sigmaRef.current?.getCamera().animatedUnzoom({ duration: 250 }) },
    resetCamera: () => { void sigmaRef.current?.getCamera().animatedReset({ duration: 300 }) },
    relayout: () => startLayout(),
    stopLayout: () => layoutRef.current?.stop('stopped'),
  }))

  return (
    <div
      ref={containerRef}
      data-slot="graph-canvas"
      className={cn('relative size-full overflow-hidden', className)}
      style={{ background: GRAPH_CHROME[theme].background }}
    />
  )
})

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import ForceGraph3D, { type ForceGraph3DInstance } from '3d-force-graph'
import { Color, Group, Mesh, MeshBasicMaterial, SphereGeometry, Vector2 } from 'three'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js'

import { STAGE_BACKGROUND, supportsWebGL } from './showcase-stage'
import { decayIntensity, type ReplayPulse, type ShowcaseData, type ShowcaseLink, type ShowcaseNode } from '@/lib/graph/showcase'

const HOT = new Color('#ffd27a')
const LINK_COLOR = '#7d8ca3'
/** 被想起的节点亮度半衰期；邻居更短。 */
const LIT_HALF_LIFE_MS = 1400
const HALO_LEVEL = 0.32

export interface ShowcaseSceneHandle {
  pulse(pulse: ReplayPulse): void
  focus(id: string): void
  resetCamera(): void
}

interface ShowcaseSceneProps {
  data: ShowcaseData
  /** 回放模式：未被想起的节点压暗，突出正在点亮的部分。 */
  dimmed: boolean
  autoRotate: boolean
  reducedMotion: boolean
  onSelectNode(id: string | null): void
}

type SceneNode = ShowcaseNode & { x?: number; y?: number; z?: number }
type SceneLink = Omit<ShowcaseLink, 'source' | 'target'> & { source: string | SceneNode; target: string | SceneNode }

interface NodeVisual {
  node: ShowcaseNode
  mesh: Mesh<SphereGeometry, MeshBasicMaterial>
  base: Color
  label: CSS2DObject | null
  pinnedLabel: boolean
}


function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char] ?? char)
}

/** 文字用 HTML 叠加（CSS2DRenderer）：不进辉光、屏幕上字号固定、清晰可读。 */
function makeLabel(node: ShowcaseNode): CSS2DObject {
  const element = document.createElement('div')
  element.textContent = node.name
  element.className = 'pointer-events-none select-none whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium text-slate-100 transition-opacity duration-300'
  element.style.background = 'rgba(0,0,0,0.45)'
  element.style.textShadow = '0 0 4px rgba(0,0,0,0.9)'
  const label = new CSS2DObject(element)
  label.center.set(0.5, 1.4)
  return label
}

/**
 * 3D 星空画布：3d-force-graph（three.js）+ 辉光。布局在首帧前预热、随后短暂收敛即停；
 * 之后只有镜头缓慢环绕与回放亮度动画在动。页面隐藏时暂停渲染。
 */
export const ShowcaseScene = forwardRef<ShowcaseSceneHandle, ShowcaseSceneProps>(function ShowcaseScene(
  { data, dimmed, autoRotate, reducedMotion, onSelectNode },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraph3DInstance | null>(null)
  const visuals = useRef(new Map<string, NodeVisual>())
  const intensity = useRef(new Map<string, number>())
  const halo = useRef(new Map<string, number>())
  const litLabels = useRef(new Set<string>())
  const fgLinks = useRef(new Map<string, SceneLink>())
  const dimmedRef = useRef(dimmed)
  const labelIdsRef = useRef(data.labelIds)
  const neighborsRef = useRef(data.neighbors)
  const selectRef = useRef(onSelectNode)
  const [unsupported, setUnsupported] = useState(false)
  selectRef.current = onSelectNode

  const applyVisual = (visual: NodeVisual) => {
    const lit = intensity.current.get(visual.node.id) ?? 0
    const glow = halo.current.get(visual.node.id) ?? 0
    const level = Math.max(lit, glow)
    // 星空：被想起越多越亮；回放：其余节点压暗，邻居被带亮一些，被想起的节点趋近暖白
    const rest = dimmedRef.current ? 0.28 + 0.45 * Math.min(1, glow / HALO_LEVEL) : 0.5 + 0.5 * visual.node.glow
    visual.mesh.material.color.copy(visual.base).multiplyScalar(Math.min(1, rest)).lerp(HOT, lit * 0.75)
    visual.mesh.scale.setScalar(visual.node.size * (1 + 0.9 * lit + 0.2 * glow))
    const showLabel = visual.pinnedLabel && !dimmedRef.current ? true : lit > 0.2 && (litLabels.current.has(visual.node.id) || (visual.pinnedLabel && level > 0.05))
    if (showLabel && !visual.label) {
      visual.label = makeLabel(visual.node)
      visual.mesh.parent?.add(visual.label)
    }
    if (visual.label) {
      visual.label.visible = showLabel
      visual.label.element.style.opacity = String(visual.pinnedLabel && !dimmedRef.current ? 0.85 : Math.min(1, 0.35 + lit))
    }
  }

  /** 取景只算有连线的主体，孤立标签留在画面外围。 */
  const fitConnected = (graph: ForceGraph3DInstance, duration: number) => {
    const connected = neighborsRef.current
    graph.zoomToFit(duration, 20, connected.size ? (node) => connected.has((node as SceneNode).id) : undefined)
  }

  // ---- 场景：只创建一次 ----
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    if (!supportsWebGL()) {
      setUnsupported(true)
      return
    }
    const graph = new ForceGraph3D(container, {
      controlType: 'orbit',
      rendererConfig: { antialias: true, powerPreference: 'high-performance' },
      extraRenderers: [new CSS2DRenderer() as never],
    })
    graphRef.current = graph
    const sphere = new SphereGeometry(1, 18, 14)
    graph
      .backgroundColor(STAGE_BACKGROUND)
      .showNavInfo(false)
      .enableNodeDrag(false)
      .width(container.clientWidth)
      .height(container.clientHeight)
      .nodeLabel((node) => escapeHtml((node as SceneNode).name))
      .nodeThreeObject((raw) => {
        const node = raw as SceneNode
        const group = new Group()
        const base = new Color(node.color)
        const mesh = new Mesh(sphere, new MeshBasicMaterial({ color: base.clone() }))
        group.add(mesh)
        const visual: NodeVisual = { node, mesh, base, label: null, pinnedLabel: labelIdsRef.current.has(node.id) }
        visuals.current.set(node.id, visual)
        applyVisual(visual)
        return group
      })
      .linkColor(() => LINK_COLOR)
      .linkOpacity(0.16)
      .linkWidth(0)
      .linkDirectionalParticleWidth(2.4)
      .linkDirectionalParticleSpeed(0.02)
      .linkDirectionalParticleColor(() => '#fff4d6')
      .warmupTicks(90)
      .cooldownTicks(120)
      .onNodeClick((node) => selectRef.current((node as SceneNode).id))
      .onBackgroundClick(() => selectRef.current(null))

    const bloom = new UnrealBloomPass(new Vector2(container.clientWidth, container.clientHeight), 0.9, 0.12, 0.18)
    graph.postProcessingComposer().addPass(bloom)
    // 后处理链末尾做色调映射与 sRGB 输出，否则画面整体发灰
    graph.postProcessingComposer().addPass(new OutputPass())

    const resize = new ResizeObserver(() => {
      graph.width(container.clientWidth).height(container.clientHeight)
      bloom.setSize(container.clientWidth, container.clientHeight)
    })
    resize.observe(container)
    const onVisibility = () => (document.hidden ? graph.pauseAnimation() : graph.resumeAnimation())
    document.addEventListener('visibilitychange', onVisibility)

    // 亮度动画：只更新正在变化的节点
    let frame = 0
    let last = performance.now()
    const tick = (now: number) => {
      const elapsed = now - last
      last = now
      for (const [store, halfLife] of [[intensity.current, LIT_HALF_LIFE_MS], [halo.current, LIT_HALF_LIFE_MS * 0.8]] as const) {
        for (const [id, value] of store) {
          const next = decayIntensity(value, elapsed, halfLife)
          if (next) store.set(id, next)
          else store.delete(id)
          const visual = visuals.current.get(id)
          if (visual) applyVisual(visual)
        }
      }
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)

    const nodeVisuals = visuals.current
    return () => {
      cancelAnimationFrame(frame)
      resize.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
      graph._destructor()
      sphere.dispose()
      nodeVisuals.clear()
      graphRef.current = null
      container.replaceChildren()
    }
    // applyVisual 只读 ref，场景只需创建一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- 数据 ----
  useEffect(() => {
    const graph = graphRef.current
    if (!graph) return
    labelIdsRef.current = data.labelIds
    neighborsRef.current = data.neighbors
    visuals.current.clear()
    intensity.current.clear()
    halo.current.clear()
    const nodes: SceneNode[] = data.nodes.map((node) => ({ ...node }))
    const links: SceneLink[] = data.links.map((link) => ({ ...link }))
    fgLinks.current = new Map(links.map((link) => [link.id, link]))
    const linkForce = graph.d3Force('link') as unknown as { distance?: (fn: (link: SceneLink) => number) => void } | undefined
    linkForce?.distance?.((link) => 26 + 40 * (1 - Math.min(1, link.weight)))
    // distanceMax：没有连线的孤立标签不会被无限推远，留在主体周围当作背景星点
    const charge = graph.d3Force('charge') as unknown as { strength?: (value: number) => { distanceMax?: (value: number) => void } } | undefined
    charge?.strength?.(-55)?.distanceMax?.(220)
    let fitted = false
    graph.onEngineStop(() => {
      if (fitted) return
      fitted = true
      fitConnected(graph, reducedMotion ? 0 : 900)
    })
    // 节点对象在 graphData 之后由 nodeThreeObject 异步创建，常显标签在那里按 labelIdsRef 设置
    graph.graphData({ nodes, links })
  }, [data, reducedMotion])

  // ---- 回放压暗 / 自动环绕 ----
  useEffect(() => {
    dimmedRef.current = dimmed
    graphRef.current?.linkOpacity(dimmed ? 0.07 : 0.16)
    for (const visual of visuals.current.values()) applyVisual(visual)
  }, [dimmed])

  useEffect(() => {
    const controls = graphRef.current?.controls() as { autoRotate?: boolean; autoRotateSpeed?: number } | undefined
    if (!controls) return
    controls.autoRotate = autoRotate && !reducedMotion
    controls.autoRotateSpeed = 0.35
  }, [autoRotate, reducedMotion])

  useImperativeHandle(ref, () => ({
    pulse(pulse) {
      litLabels.current = new Set(pulse.labels)
      for (const id of pulse.lit) {
        intensity.current.set(id, 1)
        halo.current.delete(id)
      }
      for (const id of pulse.halo) halo.current.set(id, Math.max(halo.current.get(id) ?? 0, HALO_LEVEL))
      for (const id of [...pulse.lit, ...pulse.halo]) {
        const visual = visuals.current.get(id)
        if (visual) applyVisual(visual)
      }
      const graph = graphRef.current
      if (!graph || reducedMotion) return
      for (const link of pulse.links) {
        const target = fgLinks.current.get(link.id)
        if (target) graph.emitParticle(target as never)
      }
    },
    focus(id) {
      const graph = graphRef.current
      const node = graph?.graphData().nodes.find((item) => (item as SceneNode).id === id) as SceneNode | undefined
      if (!graph || !node || node.x === undefined) return
      const distance = 70
      const length = Math.hypot(node.x, node.y ?? 0, node.z ?? 0) || 1
      const ratio = 1 + distance / length
      graph.cameraPosition(
        { x: node.x * ratio, y: (node.y ?? 0) * ratio, z: (node.z ?? 0) * ratio },
        { x: node.x, y: node.y ?? 0, z: node.z ?? 0 },
        reducedMotion ? 0 : 1100,
      )
    },
    resetCamera() {
      if (graphRef.current) fitConnected(graphRef.current, reducedMotion ? 0 : 800)
    },
  }), [reducedMotion])

  return (
    <div className="absolute inset-0" data-slot="showcase-scene">
      <div ref={containerRef} className="absolute inset-0" />
      {unsupported ? (
        <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-slate-300" role="alert">
          当前浏览器不支持 WebGL，无法显示 3D 展示模式。请换用平面图。
        </div>
      ) : null}
    </div>
  )
})

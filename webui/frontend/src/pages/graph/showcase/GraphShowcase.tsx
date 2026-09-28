import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CrosshairIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  MaximizeIcon,
  MinimizeIcon,
  PauseIcon,
  PlayIcon,
  RotateCcwIcon,
  SkipBackIcon,
  SkipForwardIcon,
  XIcon,
} from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { getRecallReplay, type RecallReplayPayload, type TagGraphScope } from '@/api/tagGraph'
import { nodeTypeLabel } from '@/lib/graph-palette'
import {
  buildShowcaseData,
  eventPulse,
  formatReplayTime,
  REPLAY_SPEEDS,
  replayDelay,
  SHOWCASE_WINDOWS_HOURS,
} from '@/lib/graph/showcase'
import { formatWindow } from '@/lib/graph/views'
import { humanizeApiError } from '@/lib/reason-label'
import { cn } from '@/lib/utils'
import { ShowcaseScene, type ShowcaseSceneHandle } from './ShowcaseScene'
import { STAGE_BACKGROUND } from './showcase-stage'

type Mode = 'stars' | 'replay'

interface GraphShowcaseProps {
  scope: TagGraphScope
  botName: string
  hours: number
  onHoursChange(hours: number): void
  /** 在平面图里打开某个标签节点。 */
  onOpenIn2D(nodeId: string): void
}

const IDLE_HIDE_MS = 2500

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches))
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!query) return
    const onChange = () => setReduced(query.matches)
    query.addEventListener?.('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

/** 舞台上的半透明控件底：舞台固定深色，不跟随站点主题。 */
const PANEL = 'rounded-lg border border-white/10 bg-black/55 text-slate-100 backdrop-blur-md'
const CHIP = 'rounded-md px-2.5 py-1 text-sm transition-colors'

export function GraphShowcase({ scope, botName, hours, onHoursChange, onOpenIn2D }: GraphShowcaseProps) {
  const stageRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<ShowcaseSceneHandle>(null)
  const reducedMotion = usePrefersReducedMotion()
  const [payload, setPayload] = useState<RecallReplayPayload | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [mode, setMode] = useState<Mode>('stars')
  const [index, setIndex] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<number>(1)
  const [selected, setSelected] = useState<string | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [idle, setIdle] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(undefined)
    getRecallReplay(scope, { hours, limit: 400, maxNodes: 400, signal: controller.signal })
      .then((result) => { setPayload(result); setIndex(-1); setPlaying(false); setSelected(null) })
      .catch((reason) => { if (!isRequestCancelled(reason)) setError(reason) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [scope, hours, reload])

  const data = useMemo(() => (payload ? buildShowcaseData(payload) : null), [payload])
  const events = useMemo(() => payload?.events ?? [], [payload])
  const current = index >= 0 ? events[index] : undefined

  // ---- 回放时钟 ----
  useEffect(() => {
    if (!playing || mode !== 'replay') return
    if (index >= events.length - 1) { setPlaying(false); return }
    const timer = window.setTimeout(() => setIndex((value) => value + 1), index < 0 ? 400 : replayDelay(events[index], speed))
    return () => window.clearTimeout(timer)
  }, [events, index, mode, playing, speed])

  useEffect(() => {
    if (!data || !current) return
    sceneRef.current?.pulse(eventPulse(current, data))
  }, [current, data])

  const enterMode = (next: Mode) => {
    setMode(next)
    setSelected(null)
    if (next === 'replay') {
      setIndex(-1)
      setPlaying(events.length > 0)
    } else {
      setPlaying(false)
      setIndex(-1)
    }
  }
  const step = (delta: number) => {
    setPlaying(false)
    setIndex((value) => Math.max(0, Math.min(events.length - 1, value + delta)))
  }
  const togglePlay = useCallback(() => {
    if (!events.length) return
    if (index >= events.length - 1) { setIndex(-1); setPlaying(true); return }
    setPlaying((value) => !value)
  }, [events.length, index])

  // ---- 全屏与录屏：全屏时鼠标静止一会儿就隐藏控件 ----
  const toggleFullscreen = () => {
    const stage = stageRef.current
    if (!stage) return
    if (document.fullscreenElement) void document.exitFullscreen?.()
    else void stage.requestFullscreen?.()
  }
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === stageRef.current)
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])
  useEffect(() => {
    if (!fullscreen) { setIdle(false); return }
    const stage = stageRef.current
    if (!stage) return
    let timer = window.setTimeout(() => setIdle(true), IDLE_HIDE_MS)
    const wake = () => {
      setIdle(false)
      window.clearTimeout(timer)
      timer = window.setTimeout(() => setIdle(true), IDLE_HIDE_MS)
    }
    stage.addEventListener('pointermove', wake)
    stage.addEventListener('keydown', wake)
    return () => { window.clearTimeout(timer); stage.removeEventListener('pointermove', wake); stage.removeEventListener('keydown', wake) }
  }, [fullscreen])

  const onKeyDown = (event: KeyboardEvent) => {
    if ((event.target as HTMLElement).closest('button, select, input')) return
    if (event.key === 'f' || event.key === 'F') { event.preventDefault(); toggleFullscreen() }
    if (mode !== 'replay') return
    if (event.key === ' ') { event.preventDefault(); togglePlay() }
    if (event.key === 'ArrowRight') { event.preventDefault(); step(1) }
    if (event.key === 'ArrowLeft') { event.preventDefault(); step(-1) }
  }

  const selectNode = useCallback((id: string | null) => {
    setSelected(id)
    if (id) sceneRef.current?.focus(id)
  }, [])

  const selectedNode = selected && data ? data.nodeById.get(selected) ?? null : null
  const stats = payload?.stats
  const overlayHidden = fullscreen && idle
  const litNames = current && data ? current.tags.map((id) => data.nodeById.get(id)?.name).filter(Boolean).slice(0, 10) as string[] : []

  return (
    <div
      ref={stageRef}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className={cn('relative overflow-hidden outline-none', fullscreen ? 'h-full w-full' : 'h-[72svh] min-h-[480px] rounded-xl border', overlayHidden && 'cursor-none')}
      style={{ background: STAGE_BACKGROUND }}
      data-slot="graph-showcase"
      data-mode={mode}
      aria-label="3D 记忆星空"
    >
      {data && data.nodes.length ? (
        <ShowcaseScene
          ref={sceneRef}
          data={data}
          dimmed={mode === 'replay'}
          autoRotate={!selected}
          reducedMotion={reducedMotion}
          onSelectNode={selectNode}
        />
      ) : null}

      {loading && !payload ? (
        <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-slate-300" role="status">
          <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />正在读取最近 {formatWindow(hours)} 的回忆…
        </div>
      ) : error && !payload ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center text-sm text-slate-300" role="alert">
          <p>{humanizeApiError(error, '回忆数据读取失败。')}</p>
          <button type="button" className={cn(PANEL, CHIP)} onClick={() => setReload((value) => value + 1)}>重试</button>
        </div>
      ) : data && !data.nodes.length ? (
        <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-slate-300">当前群还没有标签，星空是空的。</div>
      ) : null}

      <div className={cn('pointer-events-none absolute inset-0 transition-opacity duration-500', overlayHidden && 'opacity-0')}>
        {/* 左上：标题与模式 */}
        <div className="pointer-events-auto absolute top-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-col gap-2">
          <div className={cn(PANEL, 'px-3 py-2')}>
            <p className="text-sm font-semibold tracking-wide">{botName} 的记忆星空</p>
            <p className="mt-0.5 text-xs text-slate-300" data-testid="showcase-stats">
              {stats
                ? `最近 ${formatWindow(hours)} · ${stats.event_count} 次回复 · 想起 ${stats.memory_hits} 条记忆 · ${data?.nodes.length ?? 0} 个标签`
                : '读取中…'}
            </p>
          </div>
          <div role="radiogroup" aria-label="展示模式" className={cn(PANEL, 'inline-flex w-fit gap-1 p-1')}>
            {([['stars', '星空'], ['replay', '回忆回放']] as const).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={mode === id}
                disabled={!data}
                onClick={() => enterMode(id)}
                className={cn(CHIP, mode === id ? 'bg-white/15 text-white' : 'text-slate-300 hover:text-white')}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* 右上：窗口、复位、全屏 */}
        <div className="pointer-events-auto absolute top-3 right-3 flex items-center gap-1.5">
          <div className={cn(PANEL, 'inline-flex gap-1 p-1')} role="radiogroup" aria-label="时间窗口">
            {SHOWCASE_WINDOWS_HOURS.map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={hours === value}
                onClick={() => onHoursChange(value)}
                className={cn(CHIP, 'px-2', hours === value ? 'bg-white/15 text-white' : 'text-slate-300 hover:text-white')}
              >
                {formatWindow(value)}
              </button>
            ))}
          </div>
          <button type="button" className={cn(PANEL, 'p-2')} aria-label="复位视角" onClick={() => { setSelected(null); sceneRef.current?.resetCamera() }}>
            <CrosshairIcon className="size-4" aria-hidden="true" />
          </button>
          <button type="button" className={cn(PANEL, 'p-2')} aria-label={fullscreen ? '退出全屏' : '全屏'} onClick={toggleFullscreen}>
            {fullscreen ? <MinimizeIcon className="size-4" aria-hidden="true" /> : <MaximizeIcon className="size-4" aria-hidden="true" />}
          </button>
        </div>

        {/* 右侧：节点卡片 */}
        {selectedNode ? (
          <div className={cn(PANEL, 'pointer-events-auto absolute top-28 right-3 w-72 max-w-[calc(100%-1.5rem)] p-3')} data-slot="showcase-node">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-base font-semibold">{selectedNode.name}</p>
                <p className="text-xs text-slate-300">
                  {nodeTypeLabel(selectedNode.type)} · {selectedNode.memoryCount} 条记忆
                  {selectedNode.recallCount ? ` · ${formatWindow(hours)}内被想起 ${selectedNode.recallCount} 次` : ''}
                </p>
              </div>
              <button type="button" className="rounded p-1 text-slate-300 hover:text-white" aria-label="关闭" onClick={() => setSelected(null)}>
                <XIcon className="size-4" aria-hidden="true" />
              </button>
            </div>
            {selectedNode.memories.length ? (
              <ul className="mt-2 flex flex-col gap-1.5 text-xs leading-relaxed text-slate-200">
                {selectedNode.memories.map((text, position) => <li key={position} className="line-clamp-2 border-l border-white/20 pl-2">{text}</li>)}
              </ul>
            ) : null}
            <button type="button" className="mt-3 inline-flex items-center gap-1 text-xs text-sky-300 hover:text-sky-200" onClick={() => onOpenIn2D(selectedNode.id)}>
              <ExternalLinkIcon className="size-3.5" aria-hidden="true" />在平面图中查看关系与证据
            </button>
          </div>
        ) : null}

        {/* 底部：回放 */}
        {mode === 'replay' ? (
          <div className="pointer-events-auto absolute right-3 bottom-3 left-3 flex flex-col gap-2" data-slot="showcase-replay">
            {current ? (
              <div className={cn(PANEL, 'max-w-xl px-3 py-2')} data-testid="replay-event" aria-live="polite">
                <p className="text-xs text-slate-400">{formatReplayTime(current.timestamp)}</p>
                <p className="mt-0.5 line-clamp-2 text-sm"><span className="text-slate-300">{current.sender_name || '群友'}：</span>{current.message_preview}</p>
                <p className="mt-1 text-xs text-amber-200">
                  {botName}想起了 {current.memory_count} 条记忆
                  {litNames.length ? `，点亮 ${current.tags.length} 个标签：${litNames.join('、')}${current.tags.length > litNames.length ? '…' : ''}` : '（这些记忆没有标签，图上没有可点亮的节点）'}
                </p>
                {current.memories.length ? (
                  <ul className="mt-1.5 hidden flex-col gap-1 text-xs text-slate-300 sm:flex">
                    {current.memories.slice(0, 3).map((memory) => <li key={memory.id} className="line-clamp-1 border-l border-white/20 pl-2">{memory.preview}</li>)}
                  </ul>
                ) : null}
              </div>
            ) : events.length ? null : (
              <div className={cn(PANEL, 'max-w-xl px-3 py-2 text-sm text-slate-300')}>最近 {formatWindow(hours)} 没有注入记录，换一个更长的时间窗口试试。</div>
            )}
            {events.length ? (
              <div className={cn(PANEL, 'flex items-center gap-2 px-2 py-1.5')}>
                <button type="button" className="rounded p-1.5 hover:bg-white/10" aria-label="上一条" onClick={() => step(-1)}><SkipBackIcon className="size-4" aria-hidden="true" /></button>
                <button type="button" className="rounded p-1.5 hover:bg-white/10" aria-label={playing ? '暂停' : index >= events.length - 1 ? '重播' : '播放'} onClick={togglePlay}>
                  {playing ? <PauseIcon className="size-4" aria-hidden="true" /> : index >= events.length - 1 ? <RotateCcwIcon className="size-4" aria-hidden="true" /> : <PlayIcon className="size-4" aria-hidden="true" />}
                </button>
                <button type="button" className="rounded p-1.5 hover:bg-white/10" aria-label="下一条" onClick={() => step(1)}><SkipForwardIcon className="size-4" aria-hidden="true" /></button>
                <input
                  type="range"
                  min={0}
                  max={Math.max(0, events.length - 1)}
                  value={Math.max(0, index)}
                  onChange={(event) => { setPlaying(false); setIndex(Number(event.target.value)) }}
                  className="min-w-0 flex-1 accent-amber-300"
                  aria-label="回放进度"
                />
                <span className="shrink-0 text-xs tabular-nums text-slate-300" data-testid="replay-position">{Math.max(0, index + 1)} / {events.length}</span>
                <div className="hidden shrink-0 gap-0.5 sm:flex" role="radiogroup" aria-label="回放速度">
                  {REPLAY_SPEEDS.map((value) => (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={speed === value}
                      onClick={() => setSpeed(value)}
                      className={cn('rounded px-1.5 py-0.5 text-xs', speed === value ? 'bg-white/15 text-white' : 'text-slate-400 hover:text-white')}
                    >
                      {value}×
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <p className="absolute bottom-3 left-3 hidden text-xs text-slate-400 sm:block">
            拖动旋转 · 滚轮缩放 · 点节点查看 · 越亮 = 最近越常被想起 · F 全屏
          </p>
        )}
      </div>
    </div>
  )
}

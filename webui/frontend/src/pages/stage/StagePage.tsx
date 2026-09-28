import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { getScopeOptions } from '@/api/options'
import { getRecallReplay, type RecallReplayEvent, type RecallReplayPayload } from '@/api/tagGraph'
import { buildShowcaseData, eventPulse, formatReplayTime } from '@/lib/graph/showcase'
import {
  type CaptionCorner,
  describeEvent,
  GRAPH_REFRESH_MS,
  IDLE_AFTER_MS,
  IDLE_BATCH,
  IDLE_REST_MS,
  IDLE_STEP_MS,
  idleCandidates,
  LIVE_FOCUS_MS,
  mergeEvents,
  POLL_MS,
  type StagePrivacy,
} from '@/lib/graph/stage'
import { cn } from '@/lib/utils'
import { ShowcaseScene, type ShowcaseSceneHandle } from '@/pages/graph/showcase/ShowcaseScene'
import { STAGE_BACKGROUND } from '@/pages/graph/showcase/showcase-stage'

interface Shown {
  event: RecallReplayEvent
  live: boolean
  at: number
}

const CORNERS: Record<Exclude<CaptionCorner, 'none'>, string> = {
  bl: 'bottom-[4%] left-[3%]',
  br: 'bottom-[4%] right-[3%]',
  tl: 'top-[4%] left-[3%]',
  tr: 'top-[4%] right-[3%]',
}

function pickHours(value: string | null): number {
  const hours = Number(value)
  return [24, 72, 168].includes(hours) ? hours : 24
}

/**
 * 直播舞台页：不带任何控件，给 OBS 浏览器源用。
 *
 * - 星空 = 请求作用域（主群）的标签图；每 15 分钟重建一次。
 * - 每 3 秒增量拉取该 Bot 全部会话（弹幕、各群）的新注入，逐条点亮并显示字幕卡片。
 * - 一段时间没有新对话后空闲回放最近的回忆：一轮 5 条，之后留 25 秒纯星空。
 *
 * URL：bot_id、session_id（星空所用的群）、layout=full|widget、caption=bl|br|tl|tr|none、
 * privacy=strict|open（默认 strict）、hours=24|72|168、idle=秒、brand=0。
 */
export function StagePage() {
  const [params] = useSearchParams()
  const botId = params.get('bot_id') ?? ''
  const sessionId = params.get('session_id') ?? ''
  const widget = params.get('layout') === 'widget'
  const cornerParam = params.get('caption') as CaptionCorner | null
  const corner: CaptionCorner = cornerParam && (cornerParam === 'none' || cornerParam in CORNERS) ? cornerParam : 'bl'
  const privacy: StagePrivacy = params.get('privacy') === 'open' ? 'open' : 'strict'
  const hours = pickHours(params.get('hours'))
  const idleAfterMs = Number(params.get('idle')) > 0 ? Number(params.get('idle')) * 1000 : IDLE_AFTER_MS
  const showBrand = params.get('brand') !== '0' && !widget
  const scope = useMemo(() => (botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' as const } : null), [botId, sessionId])

  const sceneRef = useRef<ShowcaseSceneHandle>(null)
  const [payload, setPayload] = useState<RecallReplayPayload | null>(null)
  const [botName, setBotName] = useState(botId)
  const [shown, setShown] = useState<Shown | null>(null)
  const [focused, setFocused] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const eventsRef = useRef<RecallReplayEvent[]>([])
  const seen = useRef(new Set<string>())
  const queue = useRef<Shown[]>([])
  const lastLiveAt = useRef(Date.now())
  const restUntil = useRef(0)
  const idlePointer = useRef(0)
  const pollSince = useRef(0)

  const data = useMemo(() => (payload ? buildShowcaseData(payload, { labelCount: widget ? 0 : 12, maxLinksPerNode: widget ? 3 : 5 }) : null), [payload, widget])
  const dataRef = useRef(data)
  dataRef.current = data

  useEffect(() => {
    getScopeOptions().then((options) => {
      const name = options.bots.find((bot) => bot.db_id === botId)?.name
      if (name) setBotName(name)
    }).catch(() => undefined)
  }, [botId])

  // ---- 星空：首次加载 + 定时重建；失败 30 秒后重试，画面上只留一行小字 ----
  useEffect(() => {
    if (!scope) return
    let stopped = false
    let timer = 0
    const load = () => {
      getRecallReplay(scope, { events: 'bot', hours, limit: 400, maxNodes: widget ? 150 : 400 })
        .then((next) => {
          if (stopped) return
          setPayload(next)
          eventsRef.current = mergeEvents(eventsRef.current, next.events)
          for (const event of next.events) seen.current.add(event.trace_id)
          pollSince.current = Math.max(pollSince.current, next.generated_at)
          setStatus(null)
          timer = window.setTimeout(load, GRAPH_REFRESH_MS)
        })
        .catch(() => {
          if (stopped) return
          setStatus('记忆服务未连接，稍后重试')
          timer = window.setTimeout(load, 30_000)
        })
    }
    load()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [scope, hours, widget])

  // ---- 实时：增量拉新注入；新事件打断空闲回放 ----
  useEffect(() => {
    if (!scope || !payload) return
    let stopped = false
    let timer = 0
    const poll = () => {
      // 注入开始时记时间戳、结束后才写入，回看 30 秒并按 trace_id 去重，避免漏掉
      getRecallReplay(scope, { events: 'bot', since: Math.max(0, pollSince.current - 30), graph: false, hours: 1, limit: 50 })
        .then((next) => {
          if (stopped) return
          pollSince.current = Math.max(pollSince.current, next.generated_at)
          const fresh = next.events.filter((event) => !seen.current.has(event.trace_id))
          if (fresh.length) {
            for (const event of fresh) seen.current.add(event.trace_id)
            eventsRef.current = mergeEvents(eventsRef.current, fresh)
            queue.current = queue.current.filter((item) => item.live)
            for (const event of fresh) queue.current.push({ event, live: true, at: 0 })
            lastLiveAt.current = Date.now()
            restUntil.current = 0
          }
          setStatus(null)
        })
        .catch(() => { if (!stopped) setStatus('记忆服务未连接，稍后重试') })
        .finally(() => { if (!stopped) timer = window.setTimeout(poll, POLL_MS) })
    }
    timer = window.setTimeout(poll, POLL_MS)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [scope, payload])

  const show = useCallback((item: Shown) => {
    const current = dataRef.current
    if (!current) return
    const entry = { ...item, at: Date.now() }
    setShown(entry)
    setFocused(true)
    sceneRef.current?.pulse(eventPulse(item.event, current))
  }, [])

  // ---- 调度：先放队列里的实时事件；空闲时按轮回放 ----
  useEffect(() => {
    if (!data) return
    let stopped = false
    let timer = 0
    const loop = () => {
      if (stopped) return
      const now = Date.now()
      let delay = 400
      const next = queue.current.shift()
      if (next) {
        show(next)
        // 同时来了好几条时每条至少停 2.5 秒
        delay = next.live ? (queue.current.length ? 2_500 : 1_000) : IDLE_STEP_MS
      } else if (now - lastLiveAt.current > idleAfterMs && now >= restUntil.current) {
        const candidates = idleCandidates(eventsRef.current)
        if (candidates.length) {
          for (let i = 0; i < Math.min(IDLE_BATCH, candidates.length); i += 1) {
            queue.current.push({ event: candidates[idlePointer.current % candidates.length], live: false, at: 0 })
            idlePointer.current += 1
          }
          restUntil.current = now + IDLE_BATCH * IDLE_STEP_MS + IDLE_REST_MS
        }
      }
      timer = window.setTimeout(loop, delay)
    }
    loop()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [data, idleAfterMs, show])

  // 一条事件展示完后恢复星空（取消压暗、字幕淡出）
  useEffect(() => {
    if (!shown) return
    const timer = window.setTimeout(() => setFocused(false), shown.live ? LIVE_FOCUS_MS : IDLE_STEP_MS - 300)
    return () => window.clearTimeout(timer)
  }, [shown])

  const caption = shown && data ? describeEvent(shown.event, data, privacy) : null
  const liveNow = Boolean(shown?.live && focused)

  if (!scope) {
    return (
      <div className="flex h-svh items-center justify-center p-6 text-center text-sm text-slate-300" style={{ background: STAGE_BACKGROUND }}>
        舞台页需要 bot_id 与 session_id 参数，例如 /#/stage?bot_id=yushu&amp;session_id=羽书:group:群号
      </div>
    )
  }

  return (
    <div className="relative h-svh w-screen overflow-hidden text-slate-100" style={{ background: STAGE_BACKGROUND }} data-page="stage" data-layout={widget ? 'widget' : 'full'}>
      {data && data.nodes.length ? (
        <ShowcaseScene ref={sceneRef} data={data} dimmed={focused} autoRotate reducedMotion={false} onSelectNode={() => undefined} />
      ) : null}

      {showBrand ? (
        <div className="pointer-events-none absolute top-[4%] left-[3%] flex items-center gap-2 text-sm tracking-wide text-slate-300" data-slot="stage-brand">
          <span className={cn('size-2 rounded-full', liveNow ? 'animate-pulse bg-rose-400' : 'bg-slate-500')} aria-hidden="true" />
          {botName} 的记忆星空{liveNow ? ' · 正在回忆' : ''}
        </div>
      ) : null}

      {caption && corner !== 'none' ? (
        <div
          className={cn(
            'pointer-events-none absolute transition-opacity duration-700',
            widget ? 'right-2 bottom-2 left-2' : cn(CORNERS[corner], 'w-[34%] min-w-[320px]'),
            focused ? 'opacity-100' : 'opacity-0',
          )}
          data-slot="stage-caption"
          data-live={shown?.live ? '1' : '0'}
        >
          <div className={cn('rounded-xl border border-white/10 bg-black/60 backdrop-blur-md', widget ? 'px-2.5 py-1.5' : 'px-4 py-3')}>
            <p className={cn('flex items-center gap-2 text-slate-400', widget ? 'text-xs' : 'text-xs')}>
              <span className={cn('rounded px-1.5 py-0.5 font-medium', shown?.live ? 'bg-rose-500/80 text-white' : 'bg-white/10 text-slate-300')}>
                {shown?.live ? '实时' : '回放'}
              </span>
              <span>{caption.source}</span>
              {!shown?.live ? <span>{formatReplayTime(shown!.event.timestamp)}</span> : null}
            </p>
            {!widget ? (
              <p className="mt-1.5 line-clamp-2 text-base leading-snug">
                {caption.message !== null
                  ? <><span className="text-slate-400">{caption.speaker}：</span>{caption.message}</>
                  : <span className="text-slate-300">群里有人说了一句话</span>}
              </p>
            ) : null}
            <p className={cn('text-amber-200', widget ? 'mt-0.5 line-clamp-1 text-xs' : 'mt-1.5 text-sm')}>
              {botName}想起了 {caption.memoryCount} 条记忆{caption.litNames.length ? `：${caption.litNames.join('、')}` : ''}
            </p>
            {caption.memories.length && !widget ? (
              <ul className="mt-1.5 flex flex-col gap-1 text-xs text-slate-300">
                {caption.memories.map((text, index) => <li key={index} className="line-clamp-1 border-l border-white/20 pl-2">{text}</li>)}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {status ? <p className="pointer-events-none absolute right-2 bottom-1 text-xs text-slate-500" role="status">{status}</p> : null}
    </div>
  )
}

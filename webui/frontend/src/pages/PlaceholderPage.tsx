import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangleIcon, ArrowLeftIcon, CompassIcon, LoaderCircleIcon, RefreshCwIcon, Settings2Icon } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import { ScopeSelect } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Separator } from '@/components/ui/separator'
import { usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import {
  buildExploreFrameUrl,
  nextExploreFrameState,
  parseExploreFrameMessage,
  type ExploreFrameState,
} from '@/lib/explore-frame'

const EXPLORE_LOAD_TIMEOUT_MS = 15_000

export function ExplorePage() {
  const navigate = useNavigate()
  const pagination = usePaginationSearchParams()
  const [params] = useSearchParams()
  const botId = params.get('bot_id') ?? ''
  const sessionId = params.get('session_id') ?? ''
  const hasScope = Boolean(botId && sessionId)
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [scopeDialogOpen, setScopeDialogOpen] = useState(!hasScope)
  const [frameAttempt, setFrameAttempt] = useState(0)
  const [frameState, setFrameState] = useState<ExploreFrameState>({ status: 'loading' })
  const iframeUrl = hasScope ? buildExploreFrameUrl(botId, sessionId) : ''

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    const options = scopeOptionsFor(await getScopeOptions(), ['session'])
    return botId ? options.filter((option) => option.description?.startsWith(`${botId} ·`)) : []
  }, [botId])

  useEffect(() => {
    if (!hasScope) setScopeDialogOpen(true)
  }, [hasScope])

  const clearFrameTimeout = useCallback(() => {
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }, [])

  useEffect(() => {
    clearFrameTimeout()
    if (!iframeUrl) return

    setFrameState((current) => nextExploreFrameState(current, { type: 'reset' }))
    timeoutRef.current = setTimeout(() => {
      timeoutRef.current = null
      setFrameState((current) => nextExploreFrameState(current, { type: 'timeout' }))
    }, EXPLORE_LOAD_TIMEOUT_MS)

    return clearFrameTimeout
  }, [clearFrameTimeout, frameAttempt, iframeUrl])

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      const message = parseExploreFrameMessage(
        event,
        iframeRef.current?.contentWindow ?? null,
        window.location.origin,
      )
      if (!message) return

      clearFrameTimeout()
      setFrameState((current) => nextExploreFrameState(current, { type: 'message', message }))
    }

    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [clearFrameTimeout])

  const retryFrame = useCallback(() => {
    clearFrameTimeout()
    setFrameState((current) => nextExploreFrameState(current, { type: 'reset' }))
    setFrameAttempt((attempt) => attempt + 1)
  }, [clearFrameTimeout])

  const statusLabel = frameState.status === 'ready'
    ? 'ready · 已就绪'
    : frameState.status === 'loading'
      ? 'loading · 加载中'
      : frameState.status === 'timeout'
        ? 'timeout · 已超时'
        : 'error · 加载失败'
  const statusVariant = frameState.status === 'error' || frameState.status === 'timeout' ? 'destructive' : frameState.status === 'ready' ? 'secondary' : 'outline'

  return (
    <main data-page="explore-galaxy" className="fixed inset-0 z-40 h-[100svh] w-[100vw] overflow-hidden bg-black">
      {hasScope ? (
        <iframe
          key={`${iframeUrl}:${frameAttempt}`}
          ref={iframeRef}
          title="3D Cosmic NeuroGalaxy"
          src={iframeUrl}
          className="absolute inset-0 h-full w-full border-none bg-black"
          sandbox="allow-scripts allow-same-origin allow-popups"
        />
      ) : (
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,oklch(0.35_0.14_290/.35),transparent_38%),radial-gradient(circle_at_bottom_right,oklch(0.35_0.14_240/.25),transparent_42%),#05070b]" />
      )}

      <div className="absolute left-1/2 top-3 z-30 flex max-w-[calc(100vw-1.5rem)] -translate-x-1/2 flex-wrap items-center justify-center gap-2 rounded-xl border border-border bg-background/88 p-2 shadow-2xl backdrop-blur-xl">
        <Button type="button" size="sm" variant="ghost" onClick={() => navigate('/dashboard')}>
          <ArrowLeftIcon data-icon="inline-start" aria-hidden="true" />
          返回总览
        </Button>
        <Separator orientation="vertical" className="hidden data-[orientation=vertical]:h-5 sm:block" />
        <Badge variant="secondary" className="max-w-[min(52vw,34rem)] truncate">
          <CompassIcon data-icon="inline-start" aria-hidden="true" />
          {hasScope ? `${botId} · ${sessionId}` : '请先选择 Bot 和群'}
        </Badge>
        {hasScope ? <Badge variant={statusVariant}>{statusLabel}</Badge> : null}
        <Button type="button" size="sm" variant="outline" onClick={() => setScopeDialogOpen(true)}>
          <Settings2Icon data-icon="inline-start" aria-hidden="true" />
          切换 Bot / 群
        </Button>
        {hasScope && (frameState.status === 'error' || frameState.status === 'timeout') ? (
          <Button type="button" size="sm" variant="secondary" onClick={retryFrame}>
            <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
            重试
          </Button>
        ) : null}
      </div>

      {hasScope && frameState.status === 'loading' ? (
        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/75 p-6 text-center" role="status" aria-live="polite">
          <div className="flex max-w-sm flex-col items-center gap-3 text-slate-200">
            <LoaderCircleIcon className="size-8 animate-spin text-primary" aria-hidden="true" />
            <p className="font-medium">正在加载神经云图…</p>
            <p className="text-xs text-slate-400">首次图谱请求完成后会自动显示。</p>
          </div>
        </div>
      ) : null}
      {hasScope && (frameState.status === 'error' || frameState.status === 'timeout') ? (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/85 p-6 text-center" role="alert">
          <div className="flex max-w-md flex-col items-center gap-3 text-slate-200">
            <AlertTriangleIcon className="size-8 text-destructive" aria-hidden="true" />
            <p className="font-medium">神经云图未能就绪</p>
            <p className="text-sm text-slate-400">{frameState.message}</p>
            <Button type="button" variant="secondary" onClick={retryFrame}>
              <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
              重新加载图谱
            </Button>
          </div>
        </div>
      ) : null}

      <Dialog
        open={scopeDialogOpen}
        onOpenChange={(open) => {
          if (!open && !hasScope) return
          setScopeDialogOpen(open)
        }}
      >
        <DialogContent showCloseButton={hasScope} className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>选择神经云图的 Bot 和群</DialogTitle>
            <DialogDescription>
              必须选择服务端授权的 Bot 和群。嵌入页只接收当前群，不会把认证信息放进网址。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 sm:grid-cols-2">
            <ScopeSelect
              value={botId || undefined}
              loadOptions={loadBots}
              label="Bot"
              placeholder="选择 Bot"
              required
              onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null, visibility: 'group' })}
            />
            <ScopeSelect
              value={sessionId || undefined}
              loadOptions={loadSessions}
              label="群 / 会话"
              placeholder="选择群"
              disabled={!botId}
              required
              onValueChange={(value) => {
                pagination.setFilters({ session_id: value, visibility: 'group' })
                setScopeDialogOpen(false)
              }}
            />
          </div>
          {!hasScope ? <p className="text-xs text-muted-foreground">完成两项选择后才会加载 3D 图谱。</p> : null}
        </DialogContent>
      </Dialog>
    </main>
  )
}

export function PlaceholderPage({ title, description }: { title: string; description: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">该页面的数据视图会在后续任务接入现有 WebUI API。</p>
      </CardContent>
    </Card>
  )
}

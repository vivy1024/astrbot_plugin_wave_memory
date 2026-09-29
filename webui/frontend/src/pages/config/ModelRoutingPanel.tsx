/**
 * 配置中心「模型分工」分组的实时视图：每个后台大模型任务当前实际用哪条回退链、来源、调用统计。
 * 只读；「修改」沿用配置中心的定位机制跳到真正的编辑位置（系统配置 / Bot 管理）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircleIcon, ChevronRightIcon, PencilIcon, RefreshCwIcon } from 'lucide-react'

import { getLlmRouting, type ChainEntry, type LlmRoutingPayload, type LlmTaskRoute, type RouteSource } from '@/api/llmRouting'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import { humanizeApiError } from '@/lib/reason-label'
import { cn } from '@/lib/utils'
import { locateConfig, type ConfigLocation } from '@/pages/config/config-location'

type LoadState = { status: 'loading' } | { status: 'error'; error: unknown } | { status: 'success'; data: LlmRoutingPayload }

const SOURCE_VARIANT: Record<RouteSource, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  bot: 'default',
  task: 'default',
  default: 'outline',
  legacy: 'secondary',
  none: 'destructive',
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return ''
  try {
    return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}

function Chain({ entries, empty = '未配置' }: { entries: ChainEntry[]; empty?: string }) {
  if (!entries.length) return <span className="text-xs text-destructive">{empty}</span>
  return (
    <span className="flex flex-wrap items-center gap-1 font-mono text-xs">
      {entries.map((entry, index) => (
        <span key={`${entry.id}-${index}`} className="flex items-center gap-1">
          {index > 0 ? <span className="text-muted-foreground" aria-hidden="true">→</span> : null}
          <span
            className={cn(index === 0 && 'font-semibold', entry.known === false && 'text-destructive line-through')}
            title={entry.known === false ? 'AstrBot 里找不到这个 provider，调用时会跳过它' : undefined}
          >
            {entry.id}
          </span>
          {entry.known === false ? <Badge variant="destructive" className="px-1 py-0 text-[10px]">找不到</Badge> : null}
        </span>
      ))}
    </span>
  )
}

function Stats({ task }: { task: LlmTaskRoute }) {
  const { stats } = task
  if (!stats.calls) return <p className="text-xs text-muted-foreground">本次启动以来还没有调用</p>
  return (
    <div className="space-y-0.5 text-xs text-muted-foreground">
      <p>
        本次启动：调用 {stats.calls} 次 · 成功 {stats.succeeded} · 失败 <span className={cn(stats.failed > 0 && 'font-medium text-destructive')}>{stats.failed}</span>
        {stats.provider_errors ? ` · 渠道报错后回退 ${stats.provider_errors} 次` : ''}
      </p>
      {stats.last_provider ? <p>最近成功：<span className="font-mono">{stats.last_provider}</span>{stats.last_success_at ? ` · ${formatTime(stats.last_success_at)}` : ''}</p> : null}
      {stats.last_error ? (
        <p className="line-clamp-2 text-destructive" title={stats.last_error}>
          最近错误{stats.last_error_at ? `（${formatTime(stats.last_error_at)}）` : ''}：{stats.last_error}
        </p>
      ) : null}
    </div>
  )
}

function TaskRow({ task, onLocate }: { task: LlmTaskRoute; onLocate: (location: ConfigLocation) => void }) {
  const location = locateConfig(task.config_key, { applyMode: 'service' })
  const inactive = task.active === false
  return (
    <div className="grid gap-2 rounded-lg border p-3 md:grid-cols-[minmax(0,1fr)_minmax(12rem,1.2fr)_auto] md:items-start" data-testid={`llm-task-${task.id}`}>
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium">{task.title}</span>
          <span className="font-mono text-xs text-muted-foreground">{task.id}</span>
          <Badge variant={SOURCE_VARIANT[task.source]} title={task.source_key}>{task.source_label}</Badge>
          {task.bot_overridable ? <Badge variant="outline">可按 Bot 覆盖</Badge> : null}
          {task.reserved ? <Badge variant="secondary">预留</Badge> : inactive ? <Badge variant="secondary">未启用</Badge> : null}
        </div>
        <p className="text-xs text-muted-foreground">{task.description}</p>
        {task.consumers.length ? <p className="text-xs text-muted-foreground">用在：{task.consumers.join('、')}</p> : null}
        {inactive && task.active_note ? <p className="text-xs text-muted-foreground">{task.active_note}</p> : null}
        {task.invalid ? <p className="text-xs text-destructive">配置无效：{task.invalid}</p> : null}
      </div>
      <div className="min-w-0 space-y-1.5">
        <Chain entries={task.chain_status} />
        <p className="text-xs text-muted-foreground">
          来源 <span className="font-mono">{task.source_key}</span>
          {task.source !== 'default' && task.default_chain.length ? <>；失败时回退到 {task.default_label}</> : null}
        </p>
        <Stats task={task} />
      </div>
      <div className="flex flex-col items-start gap-1 md:items-end">
        <Button type="button" size="sm" variant="outline" aria-label={`修改 ${task.config_key}`} onClick={() => onLocate(location)}>
          <PencilIcon />修改
        </Button>
        <Badge variant={task.apply_mode === 'restart' ? 'secondary' : 'outline'}>{task.apply_mode === 'restart' ? '需重启' : '保存即生效'}</Badge>
      </div>
      {task.bots.length ? (
        <div className="space-y-1 md:col-span-3">
          {task.bots.map((bot) => (
            <div key={bot.bot_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-muted/40 px-2 py-1.5">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
                <span className="font-medium">{bot.name}</span>
                <Badge variant="default">Bot 覆盖</Badge>
                <Chain entries={bot.chain_status} />
              </div>
              <Button type="button" size="sm" variant="ghost" aria-label={`修改 ${bot.source_key}`} onClick={() => onLocate(locateConfig(bot.source_key))}>
                <PencilIcon />修改
              </Button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function ModelRoutingPanel({ onLocate }: { onLocate: (location: ConfigLocation) => void }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [providersOpen, setProvidersOpen] = useState(false)
  const requestRef = useRef(0)

  const load = useCallback(async () => {
    const requestId = ++requestRef.current
    setState({ status: 'loading' })
    try {
      const data = await getLlmRouting()
      if (requestId === requestRef.current) setState({ status: 'success', data })
    } catch (error) {
      if (requestId === requestRef.current) setState({ status: 'error', error })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const header = (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium">当前各任务实际使用的模型</p>
        <p className="text-xs text-muted-foreground">
          每个任务按「Bot 覆盖 → 任务配置 → 所属默认」拼回退链，前面的不可用（找不到、报错、返回空）时依次回退。任务配置保存即生效；标签模型与 MetaThinking 默认模型属于旧配置，改了需重启。
        </p>
      </div>
      <Button type="button" size="sm" variant="outline" onClick={() => void load()} disabled={state.status === 'loading'}>
        <RefreshCwIcon className={cn(state.status === 'loading' && 'animate-spin')} />刷新
      </Button>
    </div>
  )

  if (state.status === 'loading') {
    return <div className="space-y-2 rounded-lg border border-dashed p-3">{header}<Skeleton className="h-24 w-full" /></div>
  }
  if (state.status === 'error') {
    return (
      <div className="space-y-2 rounded-lg border border-dashed p-3">
        {header}
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>模型分工加载失败</AlertTitle>
          <AlertDescription>{humanizeApiError(state.error, '模型分工加载失败')}</AlertDescription>
        </Alert>
      </div>
    )
  }

  const { data } = state
  const families = Object.entries(data.families ?? {})
  return (
    <div className="space-y-3 rounded-lg border border-dashed p-3" data-testid="model-routing-panel">
      {header}
      {!data.available ? (
        <Alert>
          <AlertCircleIcon />
          <AlertTitle>模型分工未就绪</AlertTitle>
          <AlertDescription>{data.error ?? '插件还没完成初始化。'}</AlertDescription>
        </Alert>
      ) : null}
      {families.length ? (
        <div className="grid gap-2 md:grid-cols-2">
          {families.map(([name, family]) => {
            const key = family.keys[0]
            return (
              <div key={name} className="space-y-1 rounded-md bg-muted/40 p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium">{family.title}</span>
                  <div className="flex items-center gap-1">
                    <Badge variant="secondary">需重启</Badge>
                    <Button type="button" size="sm" variant="ghost" aria-label={`修改 ${key}`} onClick={() => onLocate(locateConfig(key, { applyMode: 'restart' }))}>
                      <PencilIcon />修改
                    </Button>
                  </div>
                </div>
                <Chain entries={family.chain_status} />
                <p className="text-xs text-muted-foreground">{family.description}</p>
              </div>
            )
          })}
        </div>
      ) : null}
      {data.tasks.map((task) => <TaskRow key={task.id} task={task} onLocate={onLocate} />)}
      {data.piggyback.length ? (
        <p className="text-xs text-muted-foreground">
          {data.piggyback.map((item) => item.title).join('、')}没有单独的模型调用，搭在「{data.tasks.find((task) => task.id === data.piggyback[0].rides_on)?.title ?? data.piggyback[0].rides_on}」那一次调用里。
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-muted/40 p-2">
        <div className="min-w-0 space-y-0.5 text-xs">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">向量模型（只读）</span>
            <Badge variant="secondary">需重启</Badge>
            <span className="font-mono">{data.embedding.provider_id || '未配置'}</span>
            {data.embedding.dimension ? <span className="text-muted-foreground">· {data.embedding.dimension} 维</span> : null}
          </div>
          <p className="text-muted-foreground">{data.embedding.note}</p>
        </div>
        <Button type="button" size="sm" variant="ghost" aria-label={`修改 ${data.embedding.key}`} onClick={() => onLocate(locateConfig(data.embedding.key, { applyMode: 'restart' }))}>
          <PencilIcon />修改
        </Button>
      </div>
      <Collapsible open={providersOpen} onOpenChange={setProvidersOpen}>
        <CollapsibleTrigger className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none">
          <ChevronRightIcon className={cn('size-3.5 transition-transform', providersOpen && 'rotate-90')} aria-hidden="true" />
          AstrBot 可选 Provider（{data.providers.length} 个）
        </CollapsibleTrigger>
        <CollapsibleContent>
          {data.providers_error ? <p className="mt-1 text-xs text-destructive">Provider 列表读取失败：{data.providers_error}</p> : null}
          <ul className="mt-1 grid gap-1 sm:grid-cols-2">
            {data.providers.map((provider) => (
              <li key={provider.id} className="truncate font-mono text-xs" title={`${provider.id}（${provider.model || '未知模型'}，${provider.type}）`}>
                {provider.id} <span className="text-muted-foreground">{provider.model}</span>
              </li>
            ))}
          </ul>
        </CollapsibleContent>
      </Collapsible>
    </div>
  )
}

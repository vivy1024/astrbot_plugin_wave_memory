import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRightIcon, AlertTriangleIcon, DatabaseIcon, TagsIcon, UsersIcon, WavesIcon, CheckCircle2Icon, BookOpenIcon } from 'lucide-react'

import { getChannelConfig, type ChannelConfigPayload } from '@/api/channels'
import { isRequestCancelled } from '@/api/client'
import { getInjectionMetrics, getRecentErrors, getSystemStatus, type ErrorPayload, type InjectionMetricsPayload, type SystemPayload } from '@/api/system'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { InjectionTrendCard } from '@/pages/dashboard/InjectionTrendCard'
import { SystemHealthCard } from '@/pages/dashboard/SystemHealthCard'

interface SectionState<T> {
  data?: T
  loading: boolean
  error?: string
}

function RetryAlert({ title, error, onRetry }: { title: string; error: string; onRetry: () => void }) {
  return <Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>{title}</AlertTitle><AlertDescription className="flex flex-col gap-3"><span>{error}</span><Button type="button" size="sm" variant="outline" className="w-fit" onClick={onRetry}>重试</Button></AlertDescription></Alert>
}

function formatNumber(value: unknown): string {
  const number = Number(value)
  if (!Number.isFinite(number)) {
    return '-'
  }
  return new Intl.NumberFormat('zh-CN').format(number)
}

function formatStorage(value: unknown): string {
  const mb = Number(value)
  if (!Number.isFinite(mb) || mb <= 0) {
    return '-'
  }
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`
}

function DashboardSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {['memories', 'vectors', 'tags', 'storage'].map((item) => (
          <Card key={item}>
            <CardHeader>
              <Skeleton className="h-4 w-24" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-8 w-32" />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

function KpiCard({ title, value, description, icon: Icon }: { title: string; value: string; description: string; icon: typeof WavesIcon }) {
  return (
    <Card className="group relative overflow-hidden border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm transition-all duration-300 hover:border-primary/25">
      <CardHeader className="flex flex-row items-center justify-between gap-3 p-4 pb-1.5">
        <div className="min-w-0">
          <CardDescription className="truncate text-[11px] font-medium text-muted-foreground/80">{title}</CardDescription>
          <CardTitle className="truncate font-mono text-xl font-semibold tracking-tight text-foreground">{value}</CardTitle>
        </div>
        <div className="shrink-0 rounded-lg border border-primary/15 bg-primary/10 p-1.5 text-primary transition-transform duration-300 group-hover:scale-105">
          <Icon className="size-4" />
        </div>
      </CardHeader>
      <CardContent className="px-4 pb-3 pt-0">
        <p className="truncate text-[10px] text-muted-foreground">{description}</p>
      </CardContent>
    </Card>
  )
}

function errorLevelLabel(level: unknown): string {
  const value = String(level ?? 'error')
  if (value === 'warning' || value === 'warn') return '警告'
  if (value === 'error') return '错误'
  if (value === 'info') return '信息'
  return value
}

function moduleLabel(key: unknown): string {
  const value = String(key ?? 'unknown')
  const labels: Record<string, string> = {
    total_tokens: '总 token',
    memories_tokens: '主记忆',
    soul_tokens: '灵魂合计',
    belief_tokens: '信念',
    relation_memories_tokens: '关系记忆',
    jargon_tokens: '黑话',
    persona_tokens: '人格画像',
    facts_tokens: '事实',
    concern_tokens: '关切',
    mood_tokens: '情绪',
    lore_tokens: '世界知识',
    exp_memories_tokens: '时间线/经历',
    book_lore_tokens: '书设知识',
    timeline_tokens: '时间线',
    fts5_tokens: '全文原词命中',
    fewshot_tokens: '风格范例',
  }
  if (value === 'unknown') return '未知模块'
  return labels[value] ?? value
}

function moduleRoute(key: unknown): string | undefined {
  const value = String(key ?? '')
  const routes: Record<string, string> = {
    lore_tokens: '/knowledge/book-lore',
    book_lore_tokens: '/knowledge/book-lore',
    exp_memories_tokens: '/channels',
    timeline_tokens: '/channels',
    relation_memories_tokens: '/people',
    fewshot_tokens: '/knowledge/style-examples',
    jargon_tokens: '/jargon',
    belief_tokens: '/beliefs',
    facts_tokens: '/knowledge/facts',
  }
  return routes[value]
}

function NeedsAttentionCards({ system }: { system?: SystemPayload }) {
  const todos = system?.todos
  const untagged = todos?.untagged_count ?? 0
  const pendingFewShot = todos?.pending_fewshot ?? 0
  const hasErrors = todos?.has_errors ?? false

  const activeTodos = [
    ...(untagged > 0 ? [{
      title: '记忆标签待处理',
      description: `当前群中有 ${untagged} 条记忆尚未完成结构化标签提取，可运行批量分析。`,
      route: '/maintenance',
      badge: '标签待处理',
      statusClass: 'border-l-4 border-l-violet-500/80 shadow-[0_0_15px_rgba(139,92,246,0.03)]',
    }] : []),
    ...(pendingFewShot > 0 ? [{
      title: '风格特征范例待审核',
      description: `风格候选库目前积压了 ${pendingFewShot} 条待审核的风格范例。`,
      route: '/knowledge/style-examples',
      badge: '风格待审',
      statusClass: 'border-l-4 border-l-amber-500/80 shadow-[0_0_15px_rgba(245,158,11,0.03)]',
    }] : []),
    ...(hasErrors ? [{
      title: '注入链路错误记录',
      description: '监测到运行时注入链路中包含未处理的严重错误日志，需排查。',
      route: '/observatory',
      badge: '故障告警',
      statusClass: 'border-l-4 border-l-red-500/80 shadow-[0_0_15px_rgba(239,68,68,0.03)]',
    }] : []),
  ]

  if (!todos) {
    return (
      <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm overflow-hidden">
        <CardContent className="py-4 px-5">
          <p className="text-xs font-medium text-foreground">系统待办状态不可用</p>
          <p className="mt-1 text-xs text-muted-foreground">服务端未返回 todos，不能据此声明系统正常或没有待处理事项。</p>
        </CardContent>
      </Card>
    )
  }

  if (activeTodos.length === 0) {
    return (
      <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm overflow-hidden">
        <CardContent className="py-4 px-5 flex items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-500 animate-pulse">
            <CheckCircle2Icon className="size-4" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-foreground flex items-center gap-1.5">
              <span>系统状态正常</span>
              <Badge variant="outline" className="text-[9px] font-normal border-emerald-500/20 text-emerald-500 bg-emerald-500/5 px-2 py-0">就绪</Badge>
            </p>
            <p className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
              当前未检测到无标签记忆、待审风格样本或运行时错误。
            </p>
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm">
      <CardHeader className="flex flex-row items-center justify-between gap-4 p-4 pb-2">
        <div>
          <CardTitle className="text-base font-semibold tracking-tight">系统待办</CardTitle>
          <CardDescription className="text-xs mt-0.5">
            需要介入处理的系统任务和状态审计。
          </CardDescription>
        </div>
        <Badge variant="destructive" className="font-mono text-[10px] py-0.5 px-2 bg-destructive/10 text-destructive border border-destructive/15">
          {activeTodos.length} 项待办
        </Badge>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        <div className="grid gap-2 md:grid-cols-2">
          {activeTodos.map((item) => (
            <div key={item.title} className={`group grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-border/80 bg-muted/5 p-3 transition-all duration-300 hover:border-primary/15 hover:bg-muted/10 ${item.statusClass}`}>
              <div className="min-w-0 flex flex-col gap-1.5">
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <p className="min-w-0 text-sm font-semibold tracking-tight text-foreground/90">{item.title}</p>
                  <Badge variant="outline" className="shrink-0 bg-background/80 px-2 py-0.5 text-[10px] font-normal">
                    {item.badge}
                  </Badge>
                </div>
                <p className="line-clamp-1 text-xs leading-relaxed text-muted-foreground">
                  {item.description}
                </p>
              </div>
              <Button asChild variant="outline" size="sm" className="h-8 w-auto shrink-0 px-3 text-xs font-medium transition-all duration-300 hover:bg-primary/5 hover:text-primary">
                <Link to={item.route} className="inline-flex items-center gap-1.5 whitespace-nowrap">
                  <span>去处理</span>
                  <ArrowRightIcon className="size-3.5 transition-transform duration-300 group-hover:translate-x-0.5" data-icon="inline-end" />
                </Link>
              </Button>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

/* 仅展示可由注入指标与通道配置直接证明的数据 */
interface InjectionBreakdownCardProps {
  metrics?: InjectionMetricsPayload
  channels?: ChannelConfigPayload
  channelsUnavailable?: boolean
  metricsUnavailable?: boolean
}

function InjectionBreakdownCard({ metrics, channels, channelsUnavailable = false, metricsUnavailable = false }: InjectionBreakdownCardProps) {
  const ranking = (metrics?.ranking ?? []).slice(0, 5)
  const max = Math.max(...ranking.map((item) => Number(item.sum ?? item.total_tokens ?? 0)), 1)
  const experienceTokens = Number((metrics?.ranking ?? []).find((item) => item.key === 'exp_memories_tokens')?.sum ?? 0)
  const timelineConfig = channels?.current?.channels?.timeline
  const enabled = timelineConfig?.enabled
  const channelValue = (value: unknown, suffix = '') => channelsUnavailable || value === undefined || value === null ? '不可用 / 未返回' : `${formatNumber(value)}${suffix}`

  return (
    <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm">
      <CardHeader className="pb-3">
        <CardTitle className="tracking-tight text-base font-semibold">注入模块消耗</CardTitle>
        <CardDescription className="text-xs">当前所选时间范围内，各注入模块的实际 Token 累计值</CardDescription>
      </CardHeader>

      <CardContent className="flex-1 pb-4">
        {ranking.length === 0 ? (
          <p className="text-xs text-muted-foreground">{metricsUnavailable ? '注入指标不可用 / 未返回。' : '当前时间范围暂无排行数据。'}</p>
        ) : (
          <div className="flex flex-col gap-3.5">
            {ranking.map((item) => {
              const value = Number(item.sum ?? item.total_tokens ?? 0)
              const ratio = Math.max(4, Math.round((value / max) * 100))
              const key = item.key ?? item.name ?? 'unknown'
              const route = moduleRoute(key)
              const label = moduleLabel(key)
              return (
                <div key={key} className="flex flex-col gap-1">
                  <div className="flex min-w-0 items-center justify-between gap-3 text-[11px]">
                    {route ? (
                      <Link to={route} className="min-w-0 flex-1 truncate font-semibold text-foreground/80 hover:text-primary transition-colors hover:underline">
                        {label}
                      </Link>
                    ) : (
                      <span className="min-w-0 flex-1 truncate font-semibold text-foreground/70">{label}</span>
                    )}
                    <span className="shrink-0 font-mono font-medium text-muted-foreground/90">{formatNumber(value)} tkn</span>
                  </div>
                  <div className="h-1 overflow-hidden rounded-full bg-muted/40">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-primary via-violet-500 to-indigo-500 shadow-[0_0_6px_rgba(124,58,237,0.2)] transition-all duration-500"
                      style={{ width: `${ratio}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>

      <div className="px-6">
        <div className="border-t border-border/40" />
      </div>

      <CardHeader className="pt-4 pb-2.5 flex flex-row items-center justify-between gap-4">
        <div className="flex-1 min-w-0">
          <CardTitle className="tracking-tight text-sm font-semibold flex items-center gap-1.5">
            <BookOpenIcon className="size-4 text-primary" />
            <span>经历注入通道</span>
          </CardTitle>
          <CardDescription className="text-[10.5px] mt-0.5 leading-relaxed text-muted-foreground/80">
            配置经历通道的单次注入上限与 Token 预算；不代表 facts 表总量。
          </CardDescription>
        </div>
        <Badge variant="outline" className="px-2 py-0.5 text-[9px] font-mono shrink-0">
          {metricsUnavailable ? '指标不可用 / 未返回' : `${metrics?.range ?? '当前窗口'} 消耗 ${formatNumber(experienceTokens)} token`}
        </Badge>
      </CardHeader>

      <CardContent className="pt-0 pb-5 flex flex-col gap-3.5">
        <div className="grid gap-2 grid-cols-2">
          <div className="rounded-lg border bg-muted/5 p-2 flex flex-col justify-between">
            <span className="text-[9.5px] text-muted-foreground/80 font-medium">通道唤醒</span>
            <span className="mt-0.5 text-[11.5px] font-semibold flex items-center gap-1.5">
              <span className={`size-1.5 rounded-full ${enabled === true ? 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.3)]' : 'bg-muted-foreground/30'}`} />
              <span>{channelsUnavailable || enabled === undefined ? '不可用 / 未返回' : enabled ? '自动注入' : '已关闭'}</span>
            </span>
          </div>
          <div className="rounded-lg border bg-muted/5 p-2 flex flex-col justify-between">
            <span className="text-[9.5px] text-muted-foreground/80 font-medium">配置状态</span>
            <span className="mt-0.5 font-mono text-[11px] text-foreground/80">{channelsUnavailable ? '不可用' : timelineConfig?.status ? String(timelineConfig.status) : '未返回'}</span>
          </div>
          <div className="rounded-lg border bg-muted/5 p-2 flex flex-col justify-between">
            <span className="text-[9.5px] text-muted-foreground/80 font-medium">注入上限</span>
            <span className="mt-0.5 font-mono text-[11px] font-semibold">{channelValue(timelineConfig?.max_items, ' 条')}</span>
          </div>
          <div className="rounded-lg border bg-muted/5 p-2 flex flex-col justify-between">
            <span className="text-[9.5px] text-muted-foreground/80 font-medium">Token 预算</span>
            <span className="mt-0.5 font-mono text-[11px] font-semibold">{channelValue(timelineConfig?.token_budget)}</span>
          </div>
        </div>

        <Button asChild variant="outline" size="sm" className="group w-full justify-between h-7.5 text-[11px] font-medium hover:bg-primary/5 hover:text-primary transition-all duration-300">
          <Link to="/channels">
            <span>前往通道热配置修改</span>
            <ArrowRightIcon className="size-3.5 transition-transform duration-300 group-hover:translate-x-0.5" data-icon="inline-end" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  )
}

function CompactSystemHealth({ system }: { system: SystemPayload }) {
  const services = system.services_health
  const summary = system.services_summary
  const total = summary?.total ?? services?.length
  const okCount = summary?.ok_count ?? services?.filter((item) => item.severity === 'ok').length
  const degradedCount = summary?.degraded_count ?? services?.filter((item) => item.severity === 'degraded' || item.severity === 'disabled').length
  const criticalCount = summary?.critical_count ?? services?.filter((item) => item.severity === 'critical').length
  const overall = summary?.overall ?? (criticalCount ? 'critical' : degradedCount ? 'degraded' : services?.length ? 'healthy' : 'unknown')
  const statusLabel = summary?.label ?? (overall === 'healthy' ? '健康' : overall === 'critical' ? '核心异常' : overall === 'degraded' ? '降级' : '不可用 / 未返回')
  const stats = [['服务', total], ['正常', okCount], ['降级', degradedCount], ['核心异常', criticalCount]]

  return (
    <details className="group overflow-hidden rounded-xl border border-border/60 bg-card shadow-sm">
      <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3 px-4 py-3 marker:hidden">
        <div className="min-w-0">
          <div className="flex items-center gap-2"><span className="text-sm font-semibold">系统健康</span><Badge variant={overall === 'critical' ? 'destructive' : 'outline'} className="text-[10px]">{statusLabel}</Badge></div>
          <p className="mt-0.5 text-[10px] text-muted-foreground">健康矩阵已压缩；展开查看服务级原因，未知状态不会按正常处理。</p>
        </div>
        <div className="grid grid-cols-4 divide-x overflow-hidden rounded-md border bg-muted/10">
          {stats.map(([label, value]) => <div key={String(label)} className="min-w-14 px-2 py-1 text-center"><div className="text-[9px] text-muted-foreground">{label}</div><div className="font-mono text-xs font-semibold">{value === undefined ? '—' : String(value)}</div></div>)}
        </div>
        <span className="text-[10px] font-medium text-primary group-open:hidden">展开详情</span><span className="hidden text-[10px] font-medium text-primary group-open:inline">收起详情</span>
      </summary>
      <div className="border-t bg-muted/5 p-3"><SystemHealthCard services={services} summary={summary} /></div>
    </details>
  )
}

function RecentErrors({ errors }: { errors?: ErrorPayload }) {
  const items = errors?.errors ?? []
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})

  const toggleExpand = (index: number) => {
    setExpanded(prev => ({ ...prev, [index]: !prev[index] }))
  }

  if (items.length === 0) return null // 平时100%零占用，不占大盘任何空白！

  return (
    <Card className="border-border/60 bg-gradient-to-b from-card to-card/95 shadow-sm border-l-4 border-l-destructive/80">
      <CardHeader className="pb-2.5">
        <CardTitle className="tracking-tight text-base font-semibold flex items-center gap-2 text-destructive">
          <AlertTriangleIcon className="size-4.5" />
          <span>检测到系统异常故障</span>
        </CardTitle>
        <CardDescription className="text-xs">注入与认知管道运行时警告、异常与断点记录，建议排查</CardDescription>
      </CardHeader>
      <CardContent className="px-6 pb-5 flex flex-col gap-3">
        {items.slice(0, 2).map((item, index) => {
          const errMsg = String(item.message ?? item.error ?? JSON.stringify(item))
          const isLong = errMsg.length > 80
          const isExpanded = !!expanded[index]
          const displayMsg = !isExpanded && isLong ? `${errMsg.substring(0, 80)}...` : errMsg

          return (
            <div
              key={`${index}-${errMsg}`}
              className="flex flex-col gap-2 rounded-xl border border-destructive/15 bg-destructive-foreground/[0.01] p-3.5 transition-all duration-300"
            >
              <div className="flex items-center justify-between gap-3 w-full">
                <span className="font-semibold text-xs text-destructive/90">{errorLevelLabel(item.level ?? item.type)}</span>
                {isLong && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => toggleExpand(index)}
                    className="h-5.5 px-2 text-[10px] font-semibold text-destructive/80 hover:text-destructive hover:bg-destructive/10 shrink-0"
                  >
                    {isExpanded ? '收起详情' : '展开故障堆栈'}
                  </Button>
                )}
              </div>
              <pre className="mt-1 max-h-48 w-full overflow-auto rounded-md bg-destructive-foreground/[0.03] p-2 font-mono text-[10px] text-destructive/80 whitespace-pre-wrap break-all leading-relaxed transition-all duration-300">
                {displayMsg}
              </pre>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

export function DashboardPage() {
  const [system, setSystem] = useState<SectionState<SystemPayload>>({ loading: true })
  const [errors, setErrors] = useState<SectionState<ErrorPayload>>({ loading: true })
  const [channels, setChannels] = useState<SectionState<ChannelConfigPayload>>({ loading: true })
  const [metrics, setMetrics] = useState<SectionState<InjectionMetricsPayload>>({ loading: true })
  const [metricsRange, setMetricsRange] = useState('7d')
  const [systemVersion, setSystemVersion] = useState(0)
  const [errorsVersion, setErrorsVersion] = useState(0)
  const [channelsVersion, setChannelsVersion] = useState(0)
  const [metricsVersion, setMetricsVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setSystem({ loading: true })
    getSystemStatus(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setSystem({ loading: false, data }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && !isRequestCancelled(reason)) setSystem({ loading: false, error: reason instanceof Error ? reason.message : '系统状态读取失败' }) })
    return () => controller.abort()
  }, [systemVersion])

  useEffect(() => {
    const controller = new AbortController()
    setErrors({ loading: true })
    getRecentErrors(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setErrors({ loading: false, data }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && !isRequestCancelled(reason)) setErrors({ loading: false, error: reason instanceof Error ? reason.message : '最近错误读取失败' }) })
    return () => controller.abort()
  }, [errorsVersion])

  useEffect(() => {
    const controller = new AbortController()
    setChannels({ loading: true })
    getChannelConfig(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setChannels({ loading: false, data }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && !isRequestCancelled(reason)) setChannels({ loading: false, error: reason instanceof Error ? reason.message : '通道配置读取失败' }) })
    return () => controller.abort()
  }, [channelsVersion])

  useEffect(() => {
    const controller = new AbortController()
    setMetrics({ loading: true })
    getInjectionMetrics(metricsRange, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setMetrics({ loading: false, data }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted && !isRequestCancelled(reason)) setMetrics({ loading: false, error: reason instanceof Error ? reason.message : '注入指标读取失败' }) })
    return () => controller.abort()
  }, [metricsRange, metricsVersion])

  const storageWarning = Number(system.data?.db_size_mb ?? 0) > 2048
  const kpis = useMemo(() => {
    if (!system.data) return []
    return [
      { title: '记忆总量', value: formatNumber(system.data.memories?.total), description: `向量覆盖 ${system.data.coverage?.vector_pct ?? '未返回'}%`, icon: WavesIcon },
      { title: '标签覆盖率', value: system.data.coverage?.tag_pct === undefined ? '未返回' : `${system.data.coverage.tag_pct}%`, description: `结构化标签 ${formatNumber(system.data.tags?.structured)}`, icon: TagsIcon },
      { title: '用户画像记录', value: formatNumber(system.data.lifecycle?.user_profiles), description: `有互动记录 ${formatNumber(system.data.lifecycle?.active_users)} 条`, icon: UsersIcon },
      { title: '数据库体积', value: formatStorage(system.data.db_size_mb), description: `共现 ${formatNumber(system.data.cooccurrence?.nodes)} 节点 / ${formatNumber(system.data.cooccurrence?.edges)} 边`, icon: DatabaseIcon },
    ]
  }, [system.data])

  return (
    <div className="flex flex-col gap-5">
      {system.loading ? <DashboardSkeleton /> : system.error ? <RetryAlert title="系统总览加载失败" error={system.error} onRetry={() => setSystemVersion((value) => value + 1)} /> : system.data ? <>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">{kpis.map((item) => <KpiCard key={item.title} {...item} />)}</div>
        {storageWarning ? <Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>数据库体积超过 2GB</AlertTitle><AlertDescription>建议在维护窗口执行清理与备份，避免 SQLite 体积继续膨胀。</AlertDescription></Alert> : null}
        <NeedsAttentionCards system={system.data} />
      </> : null}

      {metrics.error ? <RetryAlert title="注入指标加载失败" error={`${metrics.error}；未沿用上一时间范围的旧数据。`} onRetry={() => setMetricsVersion((value) => value + 1)} /> : null}
      {channels.error ? <RetryAlert title="通道配置加载失败" error={`${channels.error}；经历注入通道配置标记为不可用。`} onRetry={() => setChannelsVersion((value) => value + 1)} /> : null}
      <div className="grid items-start gap-5 lg:grid-cols-3">
        <InjectionTrendCard metrics={metrics.data} range={metricsRange} onRangeChange={setMetricsRange} loading={metrics.loading} />
        <InjectionBreakdownCard metrics={metrics.data} channels={channels.data} channelsUnavailable={Boolean(channels.error)} metricsUnavailable={!metrics.data} />
      </div>

      {system.data ? <CompactSystemHealth system={system.data} /> : null}

      {errors.loading ? <Card><CardContent className="py-6 text-sm text-muted-foreground">正在读取最近错误…</CardContent></Card> : errors.error ? <RetryAlert title="最近错误加载失败" error={errors.error} onRetry={() => setErrorsVersion((value) => value + 1)} /> : <RecentErrors errors={errors.data} />}
    </div>
  )
}

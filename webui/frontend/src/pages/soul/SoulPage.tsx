import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ActivityIcon, AlertCircleIcon, Clock3Icon, CompassIcon, GitBranchIcon, Globe2Icon, HeartHandshakeIcon, MessageSquareQuoteIcon, RefreshCwIcon, TargetIcon } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'

import { isRequestCancelled } from '@/api/client'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import {
  getRelationshipHistoricalAudit,
  getRelationships,
  type HistoricalAuditPage,
  type RelationshipItem,
} from '@/api/people'
import { getSoulState, refreshSoulState, type RelationshipHistoryItem, type SoulScopeSelection, type SoulStatePayload } from '@/api/soul'
import { RelationshipCalibrationPanel } from '@/components/relationship/RelationshipCalibrationPanel'
import { TimeAnchorsExplorer } from '@/components/soul/TimeAnchorsExplorer'
import { EvidenceList, ObjectDeepLink, PaginationControls, QueryState, ScopeSelect } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Input } from '@/components/ui/input'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'

const componentChartConfig = {
  value: { label: '分量值', color: 'var(--chart-2)' },
} satisfies ChartConfig

const relationshipChartConfig = {
  automatic_value: { label: '自动', color: 'var(--chart-1)' },
  manual_adjustment: { label: '人工', color: 'var(--chart-2)' },
  effective_value: { label: '生效', color: 'var(--chart-3)' },
} satisfies ChartConfig

const RELATIONSHIP_DIMENSIONS = [
  ['familiarity', '熟悉度'],
  ['trust', '信任'],
  ['fun', '趣味'],
  ['hostility', '敌意'],
  ['depth', '深度'],
] as const

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function reasonText(reason: string | null | undefined): string {
  if (!reason) return '服务端未提供原因'
  const labels: Record<string, string> = {
    soul_scoped_repository_unavailable: '心智数据还没准备好',
    scoped_soul_mutation_unavailable: '还不能在这里改心智数据',
    soul_runtime_refresh_unavailable: '还不能强制刷新心智',
    formal_soul_context_unavailable: '还没有时区、精力或困倦记录',
    relationship_subject_required: '请先选择当前群友，再看关系变化',
    relationship_unknown: '当前群友还没有正式关系记录',
    relationship_values_unknown: '当前群友还没有关系维度记录',
    alias_session_readonly: '这是同一群的旧平台残留，只能看不能改',
    scope_required: '请先选择 Bot 和群',
  }
  return labels[reason] ?? reason
}

function parseTimestampParam(value: string | null): number | undefined {
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function localDateTimeValue(seconds: number | undefined): string {
  if (seconds === undefined) return ''
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function timestampFromInput(value: string): number | undefined {
  if (!value) return undefined
  const timestamp = new Date(value).getTime()
  return Number.isNaN(timestamp) ? undefined : timestamp / 1000
}

function displayRelationshipValue(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未知 / 未记录'
}

function dimensionLabel(dimension: string): string {
  return RELATIONSHIP_DIMENSIONS.find(([key]) => key === dimension)?.[1] ?? dimension
}

function SectionUnavailable({ reason }: { reason?: string | null }) {
  return (
    <div className="rounded-xl border border-primary/10 bg-gradient-to-b from-card to-primary/5 p-6 text-center shadow-sm relative overflow-hidden group">
      <div className="absolute -right-6 -bottom-6 size-24 rounded-full bg-primary/5 blur-xl motion-safe:group-hover:scale-125 transition-transform duration-500" />
      <CompassIcon className="mx-auto mb-3.5 size-6 text-primary/60 motion-safe:animate-pulse" />
      <h4 className="text-xs font-semibold text-foreground/80 tracking-wide">等待心智唤醒</h4>
      <p className="mt-1.5 text-[11px] text-muted-foreground/90 max-w-[200px] mx-auto leading-normal">{reasonText(reason)}</p>
    </div>
  )
}

function RelationshipTrajectory({ history, dimension }: { history: RelationshipHistoryItem[]; dimension: string }) {
  const data = [...history].reverse().filter((item) => item.dimension === dimension && item.after).map((item) => ({
    timestamp: item.timestamp,
    automatic_value: item.after?.automatic_value ?? null,
    manual_adjustment: item.after?.manual_adjustment ?? null,
    effective_value: item.after?.effective_value ?? null,
  }))
  if (data.length < 1) return <div className="rounded-xl border border-dashed bg-muted/10 p-8 text-center text-sm text-muted-foreground">当前时间范围没有 {dimensionLabel(dimension)} 的正式层变更，暂不绘制轨迹。</div>
  return (
    <ChartContainer config={relationshipChartConfig} className="h-[260px] w-full min-w-0">
      <LineChart data={data} margin={{ left: 4, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" opacity={0.2} />
        <XAxis dataKey="timestamp" tickFormatter={(value) => formatTime(value).slice(5, 16)} tickLine={false} axisLine={false} minTickGap={28} className="text-[10px]" />
        <YAxis tickLine={false} axisLine={false} width={42} className="text-[10px]" />
        <ChartTooltip labelFormatter={(value) => formatTime(value)} content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        <Line dataKey="automatic_value" type="monotone" stroke="var(--chart-1)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="manual_adjustment" type="monotone" stroke="var(--chart-2)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="effective_value" type="monotone" stroke="var(--chart-3)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
      </LineChart>
    </ChartContainer>
  )
}

function RelationshipHistoryList({ history }: { history: RelationshipHistoryItem[] }) {
  if (!history.length) return <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">当前群没有可追踪的关系变化或人工校准记录。</p>
  return <div className="flex flex-col gap-3">{history.map((item) => <div key={item.id} className="rounded-lg border bg-muted/10 p-3"><div className="flex flex-wrap items-center gap-2"><Badge variant={item.kind === 'manual' ? 'secondary' : 'outline'}>{item.kind === 'manual' ? 'manual calibration' : 'automatic RelationshipEvent'}</Badge><Badge variant="outline">{dimensionLabel(item.dimension)}</Badge>{item.action ? <Badge variant="outline">{item.action}</Badge> : null}<span className="text-[10px] text-muted-foreground">{formatTime(item.timestamp)} · revision {item.revision ?? '未记录'}</span></div><p className="mt-2 text-sm font-medium">{item.reason || '服务端未提供原因'}</p><div className="mt-2 grid gap-2 text-xs sm:grid-cols-2"><div className="rounded border bg-background/60 p-2"><span className="text-muted-foreground">变化</span><span className="ml-2 font-mono">{item.delta === null ? '人工层变更' : displayRelationshipValue(item.delta)}</span></div><div className="rounded border bg-background/60 p-2"><span className="text-muted-foreground">来源</span><span className="ml-2 font-mono">{item.source_memory_id !== null ? `memory:${item.source_memory_id}` : item.source_episode_id !== null ? `episode:${item.source_episode_id}` : '未提供真实消息引用'}</span></div></div><div className="mt-2 flex flex-wrap gap-2 text-[10px] text-muted-foreground">{item.operation_id ? <span className="font-mono">operation:{item.operation_id}</span> : null}{item.actor ? <span>actor:{item.actor}</span> : null}</div><div className="mt-3"><EvidenceList evidence={item.evidence} /></div></div>)}</div>
}

function HistoricalAuditSideChannel({
  botId,
  sessionId,
  subjectPrincipalId,
  embedded,
}: {
  botId: string
  sessionId: string
  subjectPrincipalId: string
  /** Preferred: summary from /api/soul/state (no second request). */
  embedded?: SoulStatePayload['historical_audit'] | null
}) {
  const [data, setData] = useState<HistoricalAuditPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const hasEmbedded = Boolean(embedded && typeof embedded === 'object')

  useEffect(() => {
    // When soul/state already carried historical_audit, skip extra API call.
    if (hasEmbedded) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }
    if (!botId || !sessionId || !subjectPrincipalId) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }
    let active = true
    setLoading(true)
    setError(null)
    getRelationshipHistoricalAudit({
      bot_id: botId,
      session_id: sessionId,
      visibility: 'group',
      subject_principal_id: subjectPrincipalId,
      limit: 25,
      offset: 0,
    })
      .then((page) => {
        if (active) setData(page)
      })
      .catch((reason: unknown) => {
        if (active) {
          setData(null)
          setError(reason instanceof Error ? reason.message : '历史审计读取失败')
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [botId, hasEmbedded, sessionId, subjectPrincipalId])

  const summary = hasEmbedded ? embedded : data?.summary
  const total = summary?.total ?? 0
  const typeBits = (summary?.by_type ?? [])
    .slice(0, 4)
    .map((item) => `${item.event_type}×${item.count}`)
    .join('，')
  const recentRows = hasEmbedded
    ? (embedded?.recent ?? []).slice(0, 8)
    : (data?.items ?? []).slice(0, 8)

  return (
    <Card className="overflow-hidden border-amber-500/20 bg-gradient-to-br from-card to-amber-500/[0.04]">
      <CardHeader className="border-b bg-muted/10 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <MessageSquareQuoteIcon className="size-4 text-amber-600" />
          <CardTitle className="text-sm">历史事件审计（侧写）</CardTitle>
          <Badge variant="outline">只读</Badge>
          <Badge variant="secondary">不改变好感度</Badge>
          {hasEmbedded ? <Badge variant="outline">来自 soul/state</Badge> : null}
        </div>
        <CardDescription>
          来自本群历史关系事件；与上方实时关系变化分开，不参与好感计算。
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-5">
        {loading ? (
          <p className="text-sm text-muted-foreground">正在读取历史审计…</p>
        ) : error ? (
          <p className="text-sm text-muted-foreground">{error}</p>
        ) : !summary?.available || total <= 0 ? (
          <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
            当前 subject 没有 historical audit 记录。
          </p>
        ) : (
          <div className="grid gap-3 text-sm">
            <p>
              共 <span className="font-mono font-semibold">{total}</span> 条侧写事件
              {typeBits ? <span className="text-muted-foreground"> · {typeBits}</span> : null}
            </p>
            <div className="grid gap-2">
              {recentRows.map((item, index) => {
                const row = item as Record<string, unknown>
                const key = String(row.id ?? row.legacy_event_id ?? index)
                const eventType = String(row.event_type ?? '?')
                const dimension = String(row.dimension ?? '?')
                const delta = row.delta
                const reason = String(row.reason ?? '无原因')
                const occurredAt = row.occurred_at
                return (
                  <div key={key} className="rounded-lg border bg-muted/10 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline" className="font-mono text-[10px]">{eventType}</Badge>
                      <Badge variant="secondary" className="font-mono text-[10px]">{dimension}</Badge>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {typeof delta === 'number'
                          ? delta > 0
                            ? `+${delta}`
                            : String(delta)
                          : String(delta ?? '')}
                      </span>
                      <span className="text-[10px] text-muted-foreground">{formatTime(occurredAt)}</span>
                    </div>
                    <p className="mt-2 text-xs text-muted-foreground">{reason}</p>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function SoulContextCard({ context }: { context: SoulStatePayload['soul_context'] | undefined }) {
  const current = context ?? { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null }
  const available = current.status === 'available'
  return <Card><CardHeader className="border-b bg-muted/10 py-4"><div className="flex items-center gap-2"><Globe2Icon className="size-4 text-primary" /><CardTitle className="text-sm">作息与精力</CardTitle></div><CardDescription>时区、节律、精力与困倦只展示已记录的正式字段</CardDescription></CardHeader><CardContent className="pt-5">{!available ? <SectionUnavailable reason={current.reason_code} /> : <dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">时区</dt><dd className="mt-1 font-mono">{current.timezone ?? '未知 / 未记录'}</dd></div><div><dt className="text-muted-foreground">精力</dt><dd className="mt-1 font-mono">{displayRelationshipValue(current.energy)}</dd></div><div><dt className="text-muted-foreground">困倦</dt><dd className="mt-1 font-mono">{displayRelationshipValue(current.sleepiness)}</dd></div><div><dt className="text-muted-foreground">节律</dt><dd className="mt-1 break-all font-mono">{typeof current.circadian === 'string' ? current.circadian : JSON.stringify(current.circadian ?? '未知 / 未记录')}</dd></div></dl>}</CardContent></Card>
}

export function SoulPage() {
  const pagination = usePaginationSearchParams()
  const [searchParams] = useSearchParams()
  const [payload, setPayload] = useState<SoulStatePayload | null>(null)
  const [relationshipOptions, setRelationshipOptions] = useState<RelationshipItem[]>([])
  const [relationshipOptionsLoading, setRelationshipOptionsLoading] = useState(false)
  const [selectedRelationship, setSelectedRelationship] = useState<RelationshipItem | null>(null)
  const subjectId = searchParams.get('subject_principal_id') ?? ''
  const fromTs = parseTimestampParam(searchParams.get('from_ts'))
  const toTs = parseTimestampParam(searchParams.get('to_ts'))
  const dimensionParam = searchParams.get('relationship_dimension') ?? 'trust'
  const relationshipDimension = RELATIONSHIP_DIMENSIONS.some(([key]) => key === dimensionParam) ? dimensionParam : 'trust'
  const [status, setStatus] = useState<'loading' | 'success' | 'empty' | 'unknown' | 'error'>('empty')
  const [error, setError] = useState<unknown>()
  const formalRequestRef = useRef<AbortController | null>(null)
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const scope = useMemo<SoulScopeSelection | null>(() => botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId])
  const relationshipItem = useMemo<RelationshipItem | null>(() => {
    const relationship = payload?.relationship
    if (!relationship) return null
    const ref = relationship.people_ref
    if (!ref || !relationship.values || relationship.revision === null) return null
    const locator = String(ref.locator ?? subjectId)
    const selected = selectedRelationship?.subject_principal_id === locator
      ? selectedRelationship
      : relationshipOptions.find((item) => item.subject_principal_id === locator)
    if (!selected) return null
    return {
      ...selected,
      affinity: relationship.affinity,
      state: relationship.state,
      revision: Number(relationship.revision),
      values: relationship.values,
      evidence: relationship.evidence,
      evidence_summaries: relationship.evidence_summaries ?? selected.evidence_summaries,
      object_ref: ref,
      calibration: relationship.calibration ?? { available: false, reason_code: 'relationship_unknown' },
    }
  }, [payload, relationshipOptions, selectedRelationship, subjectId])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])

  useEffect(() => {
    if (!scope) {
      setRelationshipOptions([])
      setRelationshipOptionsLoading(false)
      return
    }
    const controller = new AbortController()
    let active = true
    setRelationshipOptionsLoading(true)
    getRelationships({ ...scope, limit: 100, offset: 0 }, controller.signal)
      .then((value) => { if (active && !controller.signal.aborted) setRelationshipOptions(value.items) })
      .catch(() => { if (active && !controller.signal.aborted) setRelationshipOptions([]) })
      .finally(() => { if (active && !controller.signal.aborted) setRelationshipOptionsLoading(false) })
    return () => { active = false; controller.abort() }
  }, [scope])

  useEffect(() => {
    if (!scope || !subjectId) {
      setSelectedRelationship(null)
      return
    }
    const controller = new AbortController()
    const userId = subjectId.includes(':user:') ? subjectId.slice(subjectId.lastIndexOf(':user:') + 6) : subjectId
    getRelationships({ ...scope, user_id: userId, limit: 25, offset: 0 }, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return
        setSelectedRelationship(value.items.find((item) => item.subject_principal_id === subjectId) ?? value.items[0] ?? null)
      })
      .catch(() => {
        if (!controller.signal.aborted) setSelectedRelationship(null)
      })
    return () => controller.abort()
  }, [scope, subjectId])

  const loadFormal = useCallback(async () => {
    formalRequestRef.current?.abort()
    if (!scope) {
      setPayload(null)
      setStatus('empty')
      return
    }
    const controller = new AbortController()
    formalRequestRef.current = controller
    setStatus('loading')
    setError(undefined)
    try {
      const formal = await getSoulState({ ...scope, ...(subjectId ? { subject_principal_id: subjectId } : {}) }, pagination.limit, pagination.offset, controller.signal, { from_ts: fromTs, to_ts: toTs })
      if (formalRequestRef.current !== controller || controller.signal.aborted) return
      setPayload(formal)
      setStatus('success')
    } catch (reason) {
      if (formalRequestRef.current !== controller || controller.signal.aborted || isRequestCancelled(reason)) return
      setPayload(null)
      setError(reason)
      setStatus('error')
    }
  }, [fromTs, pagination.limit, pagination.offset, scope, subjectId, toTs])

  // 强制自省：先走 api 层 refresh 通道触发只读投影重算，再复用原加载链路拉最新数据，避免丢当前页
  const [refreshing, setRefreshing] = useState(false)
  const refreshNow = useCallback(async () => {
    if (!scope) return
    setRefreshing(true)
    setError(undefined)
    try {
      await refreshSoulState({ ...scope, ...(subjectId ? { subject_principal_id: subjectId } : {}) }, { from_ts: fromTs, to_ts: toTs })
      await loadFormal()
    } catch (reason) {
      setError(reason)
      setStatus('error')
    } finally {
      setRefreshing(false)
    }
  }, [scope, subjectId, fromTs, toTs, loadFormal])

  useEffect(() => {
    void loadFormal()
    return () => formalRequestRef.current?.abort()
  }, [loadFormal])
  const componentData = useMemo(() => Object.entries(payload?.mood.components ?? {}).map(([name, value]) => ({ name, value })), [payload?.mood.components])
  const formalUnavailable = payload?.source.health === 'unavailable' || payload?.source.health === 'error'

  return (
    <div data-slot="soul-page" className="flex flex-col gap-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">心智状态与时间线</h1>
          <p className="text-sm text-muted-foreground">Bot 在当前群里的心情、关切、关系和时间线。</p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => void loadFormal()} disabled={!scope || status === 'loading'}>
            <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
            刷新数据
          </Button>
          <Button size="sm" variant="outline" onClick={() => void refreshNow()} disabled={!scope || status === 'loading' || refreshing}>
            <ActivityIcon data-icon="inline-start" aria-hidden="true" />
            {refreshing ? '自省中…' : '强制自省'}
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader className="py-4">
          <CardTitle>当前群心智</CardTitle>
          <CardDescription>心情、关切、时间线和关系只按所选 Bot 和群读取，不接受私聊或未绑定群。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 pt-0 md:grid-cols-3 lg:grid-cols-5">
          <ScopeSelect value={botId || undefined} loadOptions={loadBots} label="Bot" onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null, subject_principal_id: null })} />
          <ScopeSelect value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" disabled={!botId} onValueChange={(value) => pagination.setFilters({ session_id: value, subject_principal_id: null })} />
          <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium"><span>群友</span><select className="h-8 min-w-0 rounded-md border bg-background px-2 font-mono text-xs font-normal" value={subjectId} onChange={(event) => pagination.setFilters({ subject_principal_id: event.target.value || null })} disabled={!botId || !sessionId || relationshipOptionsLoading || relationshipOptions.length === 0}><option value="">{relationshipOptionsLoading ? '正在读取当前群友…' : '选择当前群友'}</option>{relationshipOptions.map((item) => <option key={item.subject_principal_id} value={item.subject_principal_id}>{item.person.display_name} · {item.person.user_id}</option>)}{subjectId && !relationshipOptions.some((item) => item.subject_principal_id === subjectId) ? <option value={subjectId}>{subjectId}（当前链接）</option> : null}</select><span className="text-xs font-normal text-muted-foreground">名单来自当前 Bot 和群，不会跨群复用</span></label>
          <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium"><span>开始时间</span><Input type="datetime-local" value={localDateTimeValue(fromTs)} onChange={(event) => pagination.setFilters({ from_ts: timestampFromInput(event.target.value) })} disabled={!scope} /><span className="text-xs font-normal text-muted-foreground">留空表示不限</span></label>
          <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium"><span>结束时间</span><Input type="datetime-local" value={localDateTimeValue(toTs)} onChange={(event) => pagination.setFilters({ to_ts: timestampFromInput(event.target.value) })} disabled={!scope} /><span className="text-xs font-normal text-muted-foreground">按本地时区转为 Unix 秒</span></label>
        </CardContent>
      </Card>

      <QueryState status={status} error={error} onRetry={() => void loadFormal()} title={!scope ? '请选择 Bot 与群' : undefined} description={!scope ? '心智页只读取已绑定的群，不接受私聊或未绑定群。' : undefined}>
        {payload ? <div className="flex flex-col gap-5">
          {formalUnavailable ? <Alert><AlertCircleIcon /><AlertTitle>心智数据不可用</AlertTitle><AlertDescription>{reasonText(payload.source.reason_code)}。下面会保持空白，不会用其他群的旧数据顶上。</AlertDescription></Alert> : null}

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="overflow-hidden border-primary/15 bg-gradient-to-br from-card to-primary/5 lg:col-span-2">
              <CardHeader className="border-b bg-muted/10 py-4"><div className="flex items-center gap-2"><ActivityIcon className="size-4 text-primary" /><CardTitle className="text-sm">当前心情</CardTitle></div><CardDescription>版本 {payload.mood.revision ?? '未记录'}</CardDescription></CardHeader>
              <CardContent className="pt-5">
                {formalUnavailable ? <SectionUnavailable reason={payload.source.reason_code} /> : <div className="grid gap-5 md:grid-cols-[180px_1fr]"><div className="flex min-h-40 flex-col items-center justify-center rounded-xl border bg-muted/10 text-center"><Badge variant={payload.mood.state === 'known' ? 'secondary' : 'outline'}>{payload.mood.state}</Badge><p className="mt-3 text-xl font-semibold">{payload.mood.value ?? '未知 / 未记录'}</p><p className="mt-1 text-xs text-muted-foreground">当前可信心境</p></div><div>{componentData.length ? <ChartContainer config={componentChartConfig} className="h-[190px] w-full"><BarChart data={componentData} layout="vertical" margin={{ left: 8, right: 16 }}><CartesianGrid horizontal={false} opacity={0.2} /><XAxis type="number" tickLine={false} axisLine={false} /><YAxis dataKey="name" type="category" tickLine={false} axisLine={false} width={88} className="text-[10px]" /><ChartTooltip content={<ChartTooltipContent />} /><Bar dataKey="value" fill="var(--chart-2)" radius={4} /></BarChart></ChartContainer> : <div className="flex h-[190px] items-center justify-center rounded-xl border text-sm text-muted-foreground">无可信分量</div>}</div></div>}
                {!formalUnavailable ? <div className="mt-4"><EvidenceList evidence={payload.mood.evidence} /></div> : null}
              </CardContent>
            </Card>

            <Card className="overflow-hidden border-pink-500/15 bg-gradient-to-br from-card to-pink-500/5">
              <CardHeader className="border-b bg-muted/10 py-4"><div className="flex items-center gap-2"><HeartHandshakeIcon className="size-4 text-pink-500" /><CardTitle className="text-sm">关系</CardTitle></div><CardDescription>版本 {payload.relationship.revision ?? '未记录'}</CardDescription></CardHeader>
              <CardContent className="flex flex-col gap-4 pt-5">
                {formalUnavailable ? <SectionUnavailable reason={payload.source.reason_code} /> : <><div className="rounded-xl border bg-background/50 p-4 text-center"><Badge variant={payload.relationship.state === 'known' ? 'secondary' : 'outline'}>{payload.relationship.state === 'known' ? '已记录' : '未记录'}</Badge><p className="mt-3 text-3xl font-bold font-mono">{payload.relationship.affinity ?? '—'}</p><p className="text-xs text-muted-foreground">好感</p>{typeof payload.relationship.affinity === 'number' ? <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-pink-500" style={{ width: `${Math.max(0, Math.min(100, payload.relationship.affinity <= 1 ? payload.relationship.affinity * 100 : payload.relationship.affinity))}%` }} /></div> : null}</div><EvidenceList evidence={payload.relationship.evidence} />{(payload.relationship.evidence_summaries?.length ?? 0) > 0 ? <div className="rounded-lg border bg-muted/10 p-3"><div className="mb-2 flex flex-wrap items-center gap-2"><Badge variant="outline">可读历史摘要</Badge><Badge variant="secondary">只读</Badge><Badge variant="outline">不改变好感度</Badge></div><div className="grid gap-1.5 text-xs text-muted-foreground">{payload.relationship.evidence_summaries!.map((summary, index) => <p key={`${index}-${summary.slice(0, 24)}`} className="rounded border bg-background/70 px-2 py-1.5">{summary}</p>)}</div></div> : null}{payload.relationship.people_ref ? <ObjectDeepLink to="/people" objectRef={payload.relationship.people_ref}>打开当前人物关系</ObjectDeepLink> : null}{relationshipItem ? <RelationshipCalibrationPanel item={relationshipItem} query={{ bot_id: botId, session_id: sessionId, visibility: 'group', user_id: relationshipItem.person.user_id, subject_principal_id: relationshipItem.subject_principal_id }} onChanged={() => void loadFormal()} /> : <Alert><AlertTitle>当前关系未知</AlertTitle><AlertDescription>请先选择当前群友。没有正式关系记录时不会写成 0，也不能校准。</AlertDescription></Alert>}</>}
              </CardContent>
            </Card>
          </div>

          <Card className="overflow-hidden border-violet-500/20 bg-gradient-to-br from-card to-violet-500/[0.04]">
            <CardHeader className="border-b bg-muted/10 py-4"><div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><GitBranchIcon className="size-4 text-violet-500" /><CardTitle className="text-sm">关系变化</CardTitle></div><label className="flex items-center gap-2 text-xs font-medium"><span>维度</span><select className="h-8 rounded-md border bg-background px-2 font-mono text-xs" value={relationshipDimension} onChange={(event) => pagination.setFilters({ relationship_dimension: event.target.value })}>{RELATIONSHIP_DIMENSIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div><CardDescription>自动学习、人工调整和最终生效值来自本群真实事件；缺的层就留空</CardDescription></CardHeader>
            <CardContent className="grid gap-5 pt-5 lg:grid-cols-[minmax(0,1.2fr)_minmax(20rem,0.8fr)]">{formalUnavailable || !subjectId ? <SectionUnavailable reason={!subjectId ? 'relationship_subject_required' : payload.source.reason_code} /> : <><div className="min-w-0"><RelationshipTrajectory history={payload.relationship_history?.items ?? []} dimension={relationshipDimension} /></div><div className="min-w-0"><div className="mb-3 flex items-center gap-2 text-xs font-semibold"><MessageSquareQuoteIcon className="size-4 text-muted-foreground" />导致变化的真实来源</div><RelationshipHistoryList history={payload.relationship_history?.items ?? []} /></div></>}</CardContent>
          </Card>

          {!formalUnavailable && subjectId ? (
            <HistoricalAuditSideChannel
              botId={botId}
              sessionId={sessionId}
              subjectPrincipalId={subjectId}
              embedded={payload.historical_audit}
            />
          ) : null}

          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <CardHeader className="border-b bg-muted/10 py-4"><div className="flex items-center gap-2"><TargetIcon className="size-4 text-primary" /><CardTitle className="text-sm">当前关切</CardTitle></div><CardDescription>本群正在记着的事</CardDescription></CardHeader>
              <CardContent className="flex flex-col gap-3 pt-5">{formalUnavailable || payload.concerns.page.total_status === 'unavailable' ? <SectionUnavailable reason={payload.concerns.page.reason_code ?? payload.source.reason_code} /> : payload.concerns.items.length ? payload.concerns.items.map((item) => <div key={item.id} className="rounded-lg border bg-muted/10 p-3"><div className="flex items-start justify-between gap-3"><p className="text-sm font-semibold">{item.topic || item.summary || '未命名关切'}</p><Badge variant="outline">版本 {item.revision ?? '—'}</Badge></div><p className="mt-1 text-[10px] text-muted-foreground">最近触发 {formatTime(item.last_triggered)}</p><div className="mt-3"><EvidenceList evidence={item.evidence} /></div></div>) : <p className="p-6 text-center text-sm text-muted-foreground">当前群没有关切记录。</p>}</CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b bg-muted/10 py-4"><div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2"><Clock3Icon className="size-4 text-primary" /><CardTitle className="text-sm">时间线</CardTitle></div><TimeAnchorsExplorer botId={botId} /></div><CardDescription>本群事件锚点，已按上面的时间范围过滤</CardDescription></CardHeader>
              <CardContent className="pt-5">{formalUnavailable || payload.timeline.page.total_status === 'unavailable' ? <SectionUnavailable reason={payload.timeline.page.reason_code ?? payload.source.reason_code} /> : payload.timeline.items.length ? <div className="ml-2 flex flex-col gap-5 border-l-2 border-muted pl-5">{payload.timeline.items.map((item) => <div key={item.id} className="relative"><span className="absolute -left-[27px] top-1 size-3 rounded-full border-2 border-background bg-primary" /><div className="rounded-lg border bg-muted/10 p-3"><p className="text-sm font-semibold">{item.event_summary || item.summary || '未命名事件'}</p><p className="mt-1 text-[10px] text-muted-foreground">{item.event_type || 'unknown'} · {formatTime(item.timestamp)} · 版本 {item.revision ?? '未记录'}</p><div className="mt-3"><EvidenceList evidence={item.evidence} /></div></div></div>)}</div> : <p className="p-6 text-center text-sm text-muted-foreground">当前群没有时间线记录。</p>}</CardContent>
            </Card>
            <SoulContextCard context={payload.soul_context} />
          </div>

          <div className="flex flex-col gap-2"><p className="text-xs text-muted-foreground">关切和时间线共用下面的分页，翻页会同时移动这两份列表。</p><PaginationControls page={payload.concerns.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} /></div>

          <Alert><AlertTitle>写入与刷新</AlertTitle><AlertDescription>改关系：{payload.capabilities.mutate.available ? '可用' : `不可用（${reasonText(payload.capabilities.mutate.reason_code)}）`}；强制自省：{payload.runtime_refresh.status === 'available' || payload.runtime_refresh.status === 'refreshed' ? '可用' : '不可用'}。本页只展示当前群的正式数据。</AlertDescription></Alert>
        </div> : null}
      </QueryState>
    </div>
  )
}

export default SoulPage

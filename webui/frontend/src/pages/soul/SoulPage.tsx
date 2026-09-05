import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ActivityIcon, AlertCircleIcon, Clock3Icon, CompassIcon, Globe2Icon, RefreshCwIcon, TargetIcon } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'

import { isRequestCancelled } from '@/api/client'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import { getRelationships, type RelationshipItem } from '@/api/people'
import { getSoulState, refreshSoulState, type RelationshipHistoryItem, type SoulScopeSelection, type SoulStatePayload } from '@/api/soul'
import { GroupRelationshipRadarCard } from '@/components/relationship/GroupRelationshipRadarCard'
import { type RelationshipRadarDimension } from '@/components/relationship/RelationshipRadarCard'
import { TimeAnchorsExplorer } from '@/components/soul/TimeAnchorsExplorer'
import { EvidenceList, ObjectDeepLink, PaginationControls, QueryState, ScopeSelect } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Input } from '@/components/ui/input'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import { formatDisplayNumber } from '@/lib/format-number'

const componentChartConfig = {
  value: { label: '分量值', color: 'var(--chart-2)' },
} satisfies ChartConfig

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
  return formatDisplayNumber(value, '未知 / 未记录')
}

function hasFormalRelationshipValues(item: RelationshipItem): boolean {
  return Object.values(item.values ?? {}).some((value) => typeof value?.effective_value === 'number' && Number.isFinite(value.effective_value))
}

function relationshipSortScore(item: RelationshipItem): [number, number, number, string] {
  const affinity = typeof item.affinity === 'number' && Number.isFinite(item.affinity) ? item.affinity : -1_000_000_000
  const interactions = typeof item.person.interaction_count === 'number' && Number.isFinite(item.person.interaction_count) ? item.person.interaction_count : -1
  const name = item.person.display_name || item.person.nickname || item.person.user_id
  return [hasFormalRelationshipValues(item) ? 1 : 0, affinity, interactions, name]
}

function sortGroupRelationships(items: RelationshipItem[]): RelationshipItem[] {
  return items
    .filter((item) => hasFormalRelationshipValues(item) && Boolean(item.object_ref?.ref?.trim()))
    .sort((left, right) => {
      const [leftFormal, leftAffinity, leftInteractions, leftName] = relationshipSortScore(left)
      const [rightFormal, rightAffinity, rightInteractions, rightName] = relationshipSortScore(right)
      return rightFormal - leftFormal
        || rightAffinity - leftAffinity
        || rightInteractions - leftInteractions
        || leftName.localeCompare(rightName, 'zh-CN')
    })
}

function relationshipErrorText(reason: unknown): string {
  if (reason instanceof Error && reason.message) return reason.message
  if (typeof reason === 'string' && reason) return reason
  return '本群关系对照暂时读取失败'
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

function SoulContextCard({ context }: { context: SoulStatePayload['soul_context'] | undefined }) {
  const current = context ?? { status: 'unavailable', reason_code: 'formal_soul_context_unavailable', timezone: null, circadian: null, energy: null, sleepiness: null }
  const available = current.status === 'available'
  return (
    <Card>
      <CardHeader className="border-b bg-muted/10 py-4">
        <div className="flex items-center gap-2">
          <Globe2Icon className="size-4 text-primary" />
          <CardTitle className="text-sm">作息与精力</CardTitle>
        </div>
        <CardDescription>时区、节律、精力与困倦只展示已记录的正式字段</CardDescription>
      </CardHeader>
      <CardContent className="pt-5">
        {!available ? (
          <SectionUnavailable reason={current.reason_code} />
        ) : (
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">时区</dt>
              <dd className="mt-1 font-mono">{current.timezone ?? '未知 / 未记录'}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">精力</dt>
              <dd className="mt-1 font-mono">{displayRelationshipValue(current.energy)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">困倦</dt>
              <dd className="mt-1 font-mono">{displayRelationshipValue(current.sleepiness)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">节律</dt>
              <dd className="mt-1 break-all font-mono">{typeof current.circadian === 'string' ? current.circadian : JSON.stringify(current.circadian ?? '未知 / 未记录')}</dd>
            </div>
          </dl>
        )}
      </CardContent>
    </Card>
  )
}

export function SoulPage() {
  const pagination = usePaginationSearchParams()
  const [searchParams] = useSearchParams()
  const [payload, setPayload] = useState<SoulStatePayload | null>(null)
  const fromTs = parseTimestampParam(searchParams.get('from_ts'))
  const toTs = parseTimestampParam(searchParams.get('to_ts'))
  const [status, setStatus] = useState<'loading' | 'success' | 'empty' | 'unknown' | 'error'>('empty')
  const [error, setError] = useState<unknown>()
  const formalRequestRef = useRef<AbortController | null>(null)
  const relationshipRequestRef = useRef<AbortController | null>(null)
  const relationshipHistoryRequestRef = useRef<AbortController | null>(null)
  const [relationships, setRelationships] = useState<RelationshipItem[]>([])
  const [relationshipStatus, setRelationshipStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('empty')
  const [relationshipError, setRelationshipError] = useState<unknown>()
  const [selectedRelationshipSubjectId, setSelectedRelationshipSubjectId] = useState<string | null>(null)
  const [selectedRelationshipDimension, setSelectedRelationshipDimension] = useState<RelationshipRadarDimension>('familiarity')
  const [relationshipHistory, setRelationshipHistory] = useState<RelationshipHistoryItem[]>([])
  const [relationshipHistoryStatus, setRelationshipHistoryStatus] = useState<'idle' | 'loading' | 'success' | 'empty' | 'error'>('idle')
  const [relationshipHistoryError, setRelationshipHistoryError] = useState<unknown>()
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const scope = useMemo<SoulScopeSelection | null>(() => botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])

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
      const formal = await getSoulState(scope, pagination.limit, pagination.offset, controller.signal, { from_ts: fromTs, to_ts: toTs })
      if (formalRequestRef.current !== controller || controller.signal.aborted) return
      setPayload(formal)
      setStatus('success')
    } catch (reason) {
      if (formalRequestRef.current !== controller || controller.signal.aborted || isRequestCancelled(reason)) return
      setPayload(null)
      setError(reason)
      setStatus('error')
    }
  }, [fromTs, pagination.limit, pagination.offset, scope, toTs])

  const loadRelationships = useCallback(async () => {
    relationshipRequestRef.current?.abort()
    relationshipHistoryRequestRef.current?.abort()
    if (!scope) {
      setRelationships([])
      setRelationshipStatus('empty')
      setRelationshipError(undefined)
      setSelectedRelationshipSubjectId(null)
      setRelationshipHistory([])
      setRelationshipHistoryStatus('idle')
      setRelationshipHistoryError(undefined)
      return
    }
    const controller = new AbortController()
    relationshipRequestRef.current = controller
    setRelationshipStatus('loading')
    setRelationshipError(undefined)
    try {
      const page = await getRelationships(
        {
          bot_id: scope.bot_id,
          session_id: scope.session_id,
          visibility: scope.visibility,
          relationship_state: 'known',
          sort_by: 'affinity',
          sort_order: 'desc',
          limit: 500,
          offset: 0,
        },
        controller.signal,
      )
      if (relationshipRequestRef.current !== controller || controller.signal.aborted) return
      const next = sortGroupRelationships(page.items)
      setRelationships(next)
      setSelectedRelationshipSubjectId(next[0]?.subject_principal_id ?? null)
      setSelectedRelationshipDimension('familiarity')
      setRelationshipStatus(next.length ? 'success' : 'empty')
    } catch (reason) {
      if (relationshipRequestRef.current !== controller || controller.signal.aborted || isRequestCancelled(reason)) return
      setRelationships([])
      setSelectedRelationshipSubjectId(null)
      setRelationshipError(reason)
      setRelationshipStatus('error')
    }
  }, [scope])

  const loadRelationshipHistory = useCallback(async () => {
    relationshipHistoryRequestRef.current?.abort()
    if (!scope || !selectedRelationshipSubjectId) {
      setRelationshipHistory([])
      setRelationshipHistoryStatus('idle')
      setRelationshipHistoryError(undefined)
      return
    }
    const controller = new AbortController()
    relationshipHistoryRequestRef.current = controller
    setRelationshipHistory([])
    setRelationshipHistoryStatus('loading')
    setRelationshipHistoryError(undefined)
    try {
      const historicalState = await getSoulState(
        { ...scope, subject_principal_id: selectedRelationshipSubjectId },
        100,
        0,
        controller.signal,
        { from_ts: fromTs, to_ts: toTs },
      )
      if (relationshipHistoryRequestRef.current !== controller || controller.signal.aborted) return
      const next = historicalState.relationship_history?.items ?? []
      setRelationshipHistory(next)
      setRelationshipHistoryStatus(next.length ? 'success' : 'empty')
    } catch (reason) {
      if (relationshipHistoryRequestRef.current !== controller || controller.signal.aborted || isRequestCancelled(reason)) return
      setRelationshipHistory([])
      setRelationshipHistoryError(reason)
      setRelationshipHistoryStatus('error')
    }
  }, [fromTs, scope, selectedRelationshipSubjectId, toTs])

  useEffect(() => {
    void loadRelationships()
    return () => relationshipRequestRef.current?.abort()
  }, [loadRelationships])

  useEffect(() => {
    void loadRelationshipHistory()
    return () => relationshipHistoryRequestRef.current?.abort()
  }, [loadRelationshipHistory])

  const [refreshing, setRefreshing] = useState(false)
  const refreshNow = useCallback(async () => {
    if (!scope) return
    setRefreshing(true)
    setError(undefined)
    try {
      await refreshSoulState(scope, { from_ts: fromTs, to_ts: toTs })
      await loadFormal()
    } catch (reason) {
      setError(reason)
      setStatus('error')
    } finally {
      setRefreshing(false)
    }
  }, [scope, fromTs, toTs, loadFormal])

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
          <h1 className="text-xl font-bold tracking-tight">心智状态</h1>
          <p className="text-sm text-muted-foreground">当前群心智状态：Bot 在本群的心境情绪、关注事项、作息精力与时间线。群友社交关系与好感请至「人物与关系」查看。</p>
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
          <CardTitle>当前群</CardTitle>
          <CardDescription>心情、关切、作息和时间线只按所选 Bot 和群读取，不接受私聊或未绑定群。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 pt-0 md:grid-cols-2 lg:grid-cols-4">
          <ScopeSelect value={botId || undefined} loadOptions={loadBots} label="Bot" onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null, subject_principal_id: null })} />
          <ScopeSelect value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" disabled={!botId} onValueChange={(value) => pagination.setFilters({ session_id: value, subject_principal_id: null })} />
          <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
            <span>开始时间</span>
            <Input type="datetime-local" value={localDateTimeValue(fromTs)} onChange={(event) => pagination.setFilters({ from_ts: timestampFromInput(event.target.value) })} disabled={!scope} />
            <span className="text-xs font-normal text-muted-foreground">用于关切与时间线，留空不限</span>
          </label>
          <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
            <span>结束时间</span>
            <Input type="datetime-local" value={localDateTimeValue(toTs)} onChange={(event) => pagination.setFilters({ to_ts: timestampFromInput(event.target.value) })} disabled={!scope} />
            <span className="text-xs font-normal text-muted-foreground">按本地时区</span>
          </label>
        </CardContent>
      </Card>

      <QueryState status={status} error={error} onRetry={() => void loadFormal()} title={!scope ? '请选择 Bot 与群' : undefined} description={!scope ? '心智页只读取已绑定的群，不接受私聊或未绑定群。' : undefined}>
        {payload ? (
          <div className="flex flex-col gap-5">
            {formalUnavailable ? (
              <Alert>
                <AlertCircleIcon />
                <AlertTitle>心智数据不可用</AlertTitle>
                <AlertDescription>{reasonText(payload.source.reason_code)}。下面会保持空白，不会用其他群的旧数据顶上。</AlertDescription>
              </Alert>
            ) : null}

            <GroupRelationshipRadarCard
              relationships={relationships}
              scopeQuery={{ bot_id: scope?.bot_id, session_id: scope?.session_id, visibility: scope?.visibility }}
              relationshipLoading={relationshipStatus === 'loading'}
              relationshipError={relationshipStatus === 'error' ? relationshipErrorText(relationshipError) : null}
              onRetryRelationships={() => void loadRelationships()}
              history={relationshipHistory}
              historyLoading={relationshipHistoryStatus === 'loading'}
              historyError={relationshipHistoryStatus === 'error' ? relationshipErrorText(relationshipHistoryError) : null}
              onRetryHistory={() => void loadRelationshipHistory()}
              selectedSubjectId={selectedRelationshipSubjectId}
              selectedDimension={selectedRelationshipDimension}
              onSelectedSubjectIdChange={setSelectedRelationshipSubjectId}
              onSelectedDimensionChange={setSelectedRelationshipDimension}
            />

            <div className="grid gap-4 lg:grid-cols-3">
              <Card className="overflow-hidden border-primary/15 bg-gradient-to-br from-card to-primary/5 lg:col-span-2">
                <CardHeader className="border-b bg-muted/10 py-4">
                  <div className="flex items-center gap-2">
                    <ActivityIcon className="size-4 text-primary" />
                    <CardTitle className="text-sm">当前心情</CardTitle>
                  </div>
                  <CardDescription>Bot 自己现在的心境 · 版本 {payload.mood.revision ?? '未记录'}</CardDescription>
                </CardHeader>
                <CardContent className="pt-5">
                  {formalUnavailable ? (
                    <SectionUnavailable reason={payload.source.reason_code} />
                  ) : (
                    <div className="grid gap-5 md:grid-cols-[180px_1fr]">
                      <div className="flex min-h-40 flex-col items-center justify-center rounded-xl border bg-muted/10 text-center">
                        <Badge variant={payload.mood.state === 'known' ? 'secondary' : 'outline'}>
                          {payload.mood.state === 'known' ? '已记录' : '未记录'}
                        </Badge>
                        <p className="mt-3 text-xl font-semibold">{payload.mood.value ?? '未知 / 未记录'}</p>
                        <p className="mt-1 text-xs text-muted-foreground">当前可信心境</p>
                      </div>
                      <div>
                        {componentData.length ? (
                          <ChartContainer config={componentChartConfig} className="h-[190px] w-full">
                            <BarChart data={componentData} layout="vertical" margin={{ left: 8, right: 16 }}>
                              <CartesianGrid horizontal={false} opacity={0.2} />
                              <XAxis type="number" tickLine={false} axisLine={false} />
                              <YAxis dataKey="name" type="category" tickLine={false} axisLine={false} width={88} className="text-[10px]" />
                              <ChartTooltip content={<ChartTooltipContent />} />
                              <Bar dataKey="value" fill="var(--chart-2)" radius={4} />
                            </BarChart>
                          </ChartContainer>
                        ) : (
                          <div className="flex h-[190px] items-center justify-center rounded-xl border text-sm text-muted-foreground">无可信分量</div>
                        )}
                      </div>
                    </div>
                  )}
                  {!formalUnavailable ? <div className="mt-4"><EvidenceList evidence={payload.mood.evidence} /></div> : null}
                </CardContent>
              </Card>
              <SoulContextCard context={payload.soul_context} />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="border-b bg-muted/10 py-4">
                  <div className="flex items-center gap-2">
                    <TargetIcon className="size-4 text-primary" />
                    <CardTitle className="text-sm">当前关切</CardTitle>
                  </div>
                  <CardDescription>Bot 在本群正在记着的事</CardDescription>
                </CardHeader>
                <CardContent className="flex flex-col gap-3 pt-5">
                  {formalUnavailable || payload.concerns.page.total_status === 'unavailable' ? (
                    <SectionUnavailable reason={payload.concerns.page.reason_code ?? payload.source.reason_code} />
                  ) : payload.concerns.items.length ? (
                    payload.concerns.items.map((item) => (
                      <div key={item.id} className="rounded-lg border bg-muted/10 p-3">
                        <div className="flex items-start justify-between gap-3">
                          <p className="text-sm font-semibold">{item.topic || item.summary || '未命名关切'}</p>
                          <Badge variant="outline">版本 {item.revision ?? '—'}</Badge>
                        </div>
                        <p className="mt-1 text-[10px] text-muted-foreground">最近触发 {formatTime(item.last_triggered)}</p>
                        <div className="mt-3"><EvidenceList evidence={item.evidence} /></div>
                      </div>
                    ))
                  ) : (
                    <p className="p-6 text-center text-sm text-muted-foreground">当前群没有关切记录。</p>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="border-b bg-muted/10 py-4">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Clock3Icon className="size-4 text-primary" />
                      <CardTitle className="text-sm">时间线</CardTitle>
                    </div>
                    <TimeAnchorsExplorer botId={botId} />
                  </div>
                  <CardDescription>本群事件锚点，已按上面的时间范围过滤</CardDescription>
                </CardHeader>
                <CardContent className="pt-5">
                  {formalUnavailable || payload.timeline.page.total_status === 'unavailable' ? (
                    <SectionUnavailable reason={payload.timeline.page.reason_code ?? payload.source.reason_code} />
                  ) : payload.timeline.items.length ? (
                    <div className="ml-2 flex flex-col gap-5 border-l-2 border-muted pl-5">
                      {payload.timeline.items.map((item) => (
                        <div key={item.id} className="relative">
                          <span className="absolute -left-[27px] top-1 size-3 rounded-full border-2 border-background bg-primary" />
                          <div className="rounded-lg border bg-muted/10 p-3">
                            <p className="text-sm font-semibold">{item.event_summary || item.summary || '未命名事件'}</p>
                            <p className="mt-1 text-[10px] text-muted-foreground">{item.event_type || 'unknown'} · {formatTime(item.timestamp)} · 版本 {item.revision ?? '未记录'}</p>
                            <div className="mt-3"><EvidenceList evidence={item.evidence} /></div>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="p-6 text-center text-sm text-muted-foreground">当前群没有时间线记录。</p>
                  )}
                </CardContent>
              </Card>
            </div>

            <div className="flex flex-col gap-2">
              <p className="text-xs text-muted-foreground">关切和时间线共用下面的分页，翻页会同时移动这两份列表。</p>
              <PaginationControls page={payload.concerns.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} />
            </div>

            <Alert>
              <AlertTitle>自省与认知状态</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
                <span>当前心智状态为只读安全聚合；点击右上角「强制自省」可触发只读重算，不会新增外部事件或调用大模型。</span>
                {payload.relationship?.people_ref ? (
                  <ObjectDeepLink to="/people" objectRef={payload.relationship.people_ref}>
                    前往人物页校准社交关系
                  </ObjectDeepLink>
                ) : null}
              </AlertDescription>
            </Alert>
          </div>
        ) : null}
      </QueryState>
    </div>
  )
}

export default SoulPage

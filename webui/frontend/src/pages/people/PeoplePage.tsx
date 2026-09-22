import { useCallback, useEffect, useState, useMemo, useRef, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { humanizeApiError } from '@/lib/reason-label'
import { AlertCircleIcon, EyeIcon, RefreshCwIcon, SearchIcon, SlidersHorizontalIcon, ArrowUpDownIcon, SparklesIcon } from 'lucide-react'

import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import {
  clearImpression,
  getLegacyPeople,
  getPeople,
  getPersonTimeline,
  getRelationshipHistoricalAudit,
  getRelationships,
  type HistoricalAuditPage,
  type PersonItem,
  type PersonTimelineEventItem,
  type RelationshipItem,
} from '@/api/people'
import { RelationshipCalibrationPanel } from '@/components/relationship/RelationshipCalibrationPanel'
import { RelationshipRadarCard } from '@/components/relationship/RelationshipRadarCard'
import { RelationshipTrajectoryCard } from '@/components/relationship/RelationshipTrajectoryCard'
import { PaginationControls, QueryState, DeclarativeDataTable, ScopeSelect, usePaginationSearchParams, type PageResponse } from '@/components/shared'
import { useCanonicalScopeDefault } from '@/hooks/use-pagination-search-params'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { formatSignedDisplayNumber } from '@/lib/format-number'
import { scopedHref } from '@/lib/navigation-search'

function aliasLabels(aliases: unknown[]): string[] {
  return aliases.map((alias) => typeof alias === 'string' ? alias : '').filter(Boolean)
}

function interactionCount(item: PersonItem): number | null {
  if (typeof item.interaction_count === 'number' && Number.isFinite(item.interaction_count)) return item.interaction_count
  const registryCount = item.person_registry?.message_count
  return typeof registryCount === 'number' && Number.isFinite(registryCount) ? registryCount : null
}

function defaultSortOrder(sortBy: 'name' | 'interactions' | 'affinity'): 'asc' | 'desc' {
  return sortBy === 'name' ? 'asc' : 'desc'
}

function SummaryTile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return <div className="min-w-[4.75rem] rounded-lg border bg-muted/20 px-3 py-1.5 text-center text-xs"><div className="text-[10px] text-muted-foreground">{label}</div><div className={`font-semibold ${tone ?? ''}`}>{value}</div></div>
}

function HistoricalAuditPanel({
  query,
  userId,
}: {
  query: { bot_id: string; session_id: string; visibility: 'group' }
  userId: string
}) {
  const [data, setData] = useState<HistoricalAuditPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    getRelationshipHistoricalAudit({
      bot_id: query.bot_id,
      session_id: query.session_id,
      visibility: 'group',
      user_id: userId,
      limit: 25,
      offset: 0,
    })
      .then((page) => {
        if (active) setData(page)
      })
      .catch((reason: unknown) => {
        if (active) {
          setData(null)
          setError(humanizeApiError(reason, '历史审计读取失败'))
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [query.bot_id, query.session_id, userId])

  const summary = data?.summary
  const total = summary?.total ?? 0
  const typeBits = (summary?.by_type ?? [])
    .slice(0, 4)
    .map((item) => `${timelineEventLabel(item.event_type)}×${item.count}`)
    .join('，')

  return (
    <div className="rounded-lg border bg-muted/10 p-3.5">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant="outline">正式关系事件</Badge>
        <Badge variant="secondary">只读</Badge>
        <Badge variant="outline">不改变好感度</Badge>
      </div>
      {loading ? (
        <p className="text-xs text-muted-foreground">正在读取正式关系事件…</p>
      ) : error ? (
        <p className="text-xs text-muted-foreground">{error}</p>
      ) : !summary?.available || total <= 0 ? (
        <p className="text-xs text-muted-foreground">当前群暂无已记录的关系事件。</p>
      ) : (
        <div className="grid gap-2 text-xs">
          <p>
            共 <span className="font-mono font-semibold">{total}</span> 条侧写事件
            {typeBits ? <span className="text-muted-foreground"> · {typeBits}</span> : null}
          </p>
          <div className="grid gap-1.5">
            {(data?.items ?? []).slice(0, 5).map((item) => (
              <div key={`${item.id}-${item.legacy_event_id}`} className="rounded border bg-background/70 px-2 py-1.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant="outline" className="font-mono text-[10px]">{item.event_type}</Badge>
                  <Badge variant="secondary" className="font-mono text-[10px]">{item.dimension}</Badge>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {typeof item.delta === 'number' ? formatSignedDisplayNumber(item.delta) : String(item.delta ?? '')}
                  </span>
                </div>
                <p className="mt-1 text-[11px] text-muted-foreground">{item.reason || '无原因'}</p>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-muted-foreground">
            来自本群历史关系事件；仅审计展示，不参与好感计算。
          </p>
        </div>
      )}
    </div>
  )
}

function impressionOf(item: PersonItem): string {
  const value = item.metadata?.impression
  return typeof value === 'string' ? value.trim() : ''
}

function formatImpressionTime(value: unknown): string {
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toLocaleString('zh-CN') : ''
}

const TIMELINE_KIND_LABELS: Record<string, string> = {
  impression: '印象',
  affinity: '好感',
  person_fact: '人物事实',
}

/** 印象时间线的事件细分类型；未知取值不再原样显示英文 code。 */
const TIMELINE_EVENT_LABELS: Record<string, string> = {
  interaction: '互动',
  conversation: '对话',
  impression: '印象',
  mood_shift: '情绪变化',
  affinity_change: '好感变化',
  person_fact: '人物事实',
  fact_update: '事实更新',
  relationship: '关系',
  conflict: '摩擦',
  repair: '修复',
  milestone: '里程碑',
  daily: '日常',
}

function timelineKindLabel(kind: string): string {
  return TIMELINE_KIND_LABELS[kind] ?? kind ?? '事件'
}

function timelineEventLabel(eventType: string | null | undefined): string {
  const key = String(eventType ?? '').trim()
  if (!key) return '事件'
  return TIMELINE_EVENT_LABELS[key] ?? TIMELINE_EVENT_LABELS[key.toLowerCase()] ?? '其他事件'
}

function ImpressionTimelinePanel({ query, userId }: { query: { bot_id: string; session_id: string; visibility: 'group' }; userId: string }) {
  const { bot_id: botId, session_id: sessionId, visibility } = query
  const [kind, setKind] = useState<'' | 'impression' | 'affinity' | 'person_fact'>('')
  const [search, setSearch] = useState('')
  const [searchDraft, setSearchDraft] = useState('')
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const limit = 25
  const [data, setData] = useState<{ items: PersonTimelineEventItem[]; total: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { setOffset(0) }, [kind, search, userId])

  useEffect(() => {
    if (!botId || !sessionId || !userId) return
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    getPersonTimeline({
      bot_id: botId,
      session_id: sessionId,
      visibility,
      user_id: userId,
      kind: kind || undefined,
      search: search || undefined,
      limit,
      offset,
    }, controller.signal)
      .then((page) => { if (!controller.signal.aborted) setData({ items: page.items ?? [], total: page.page?.total ?? 0 }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) { setData(null); setError(humanizeApiError(reason, '印象时间线读取失败')) } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [botId, kind, limit, offset, reload, search, sessionId, userId, visibility])

  const items = data?.items ?? []
  const total = data?.total ?? 0
  const page = Math.floor(offset / limit) + 1
  const pageCount = Math.max(1, Math.ceil(total / limit))

  return (
    <div className="mt-3 border-t border-amber-500/20 pt-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[10px] text-muted-foreground">印象时间线（新在上，只读 · 全量分页）</p>
        <div className="flex flex-wrap items-center gap-1.5">
          <select aria-label="印象类型筛选" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)} className="h-6 rounded-md border bg-background px-1.5 text-[11px]">
            <option value="">全部类型</option>
            <option value="impression">印象</option>
            <option value="affinity">好感</option>
            <option value="person_fact">人物事实</option>
          </select>
          <form className="flex items-center gap-1" onSubmit={(event) => { event.preventDefault(); setSearch(searchDraft.trim()) }}>
            <Input aria-label="搜索印象时间线" className="h-6 w-28 text-[11px]" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索…" />
            <Button type="submit" size="sm" variant="outline" className="h-6 px-1.5 text-[11px]">搜索</Button>
          </form>
          <Button type="button" size="sm" variant="ghost" className="h-6 px-1.5 text-[11px]" onClick={() => setReload((value) => value + 1)}><RefreshCwIcon className="size-3" aria-hidden="true" /></Button>
        </div>
      </div>
      {loading ? (
        <p className="text-xs text-muted-foreground">正在读取该群友的印象时间线…</p>
      ) : error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : !items.length ? (
        <p className="text-xs text-muted-foreground">该筛选下暂无印象记录。旧画像只有当前一句时不补历史；下次印象更新、好感结算或人物事实提审后才会写入时间线。</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {items.map((entry, index) => (
            <li key={`${entry.id}-${index}`} className="rounded-md bg-background/60 px-2.5 py-2">
              <p className="whitespace-pre-wrap break-words text-sm">{entry.summary || entry.detail || '未命名事件'}</p>
              {entry.source_quote ? (
                <div className="mt-1.5 rounded bg-muted/40 px-2 py-1 text-xs text-muted-foreground border-l-2 border-primary/60">
                  <span className="font-semibold text-foreground/80">原话证据：</span>“{entry.source_quote}”
                </div>
              ) : null}
              <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                <Badge variant="outline" className="font-mono text-[10px]">{timelineKindLabel(entry.kind)}</Badge>
                <span>{formatImpressionTime(entry.occurred_at) || '未记录时间'}</span>
                {entry.dimension && entry.delta !== null && entry.delta !== undefined ? (
                  <span className="font-mono">{entry.dimension}{typeof entry.delta === 'number' ? formatSignedDisplayNumber(entry.delta) : String(entry.delta)}</span>
                ) : null}
                {entry.event_type && entry.event_type !== entry.kind ? <span>{timelineEventLabel(entry.event_type)}</span> : null}
              </p>
            </li>
          ))}
        </ol>
      )}
      {total > limit ? (
        <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
          <span>共 {total} 条</span>
          <div className="flex items-center gap-1">
            <Button type="button" size="sm" variant="outline" className="h-6 px-1.5 text-[11px]" disabled={offset <= 0 || loading} onClick={() => setOffset((value) => Math.max(0, value - limit))}>上一页</Button>
            <span className="px-1">{page} / {pageCount}</span>
            <Button type="button" size="sm" variant="outline" className="h-6 px-1.5 text-[11px]" disabled={offset + limit >= total || loading} onClick={() => setOffset((value) => value + limit)}>下一页</Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function PersonDetail({ item, relationship, relationshipError, query, onChanged }: { item: PersonItem; relationship: RelationshipItem | null; relationshipError?: string | null; query: { bot_id: string; session_id: string; visibility: 'group'; user_id?: string }; onChanged?: () => void }) {
  const experienceHref = scopedHref('/knowledge/experiences', '', { bot_id: query.bot_id, session_id: query.session_id, visibility: 'group' })
  const soulHref = scopedHref('/soul', '', { bot_id: query.bot_id, session_id: query.session_id, visibility: 'group' })
  const aliases = aliasLabels(item.aliases)
  const metadataCount = Object.keys(item.metadata ?? {}).length + Object.keys(item.registry_metadata ?? {}).length
  const actualAffinity = relationship?.affinity !== undefined && relationship?.affinity !== null ? relationship.affinity : null
  const impression = impressionOf(item)
  const [clearing, setClearing] = useState(false)
  const [clearError, setClearError] = useState('')

  async function handleClearImpression() {
    const reason = window.prompt('清除当前印象的原因（会写入审计，不会删除历史）')?.trim() ?? ''
    if (reason.length < 4) {
      setClearError(reason ? '原因至少 4 个字' : '')
      return
    }
    setClearing(true)
    setClearError('')
    try {
      await clearImpression(query, { user_id: item.user_id, reason })
      onChanged?.()
    } catch (error) {
      setClearError(humanizeApiError(error, '清除印象失败'))
    } finally {
      setClearing(false)
    }
  }

  return     <div className="flex flex-col gap-5 text-sm">
    <div className="flex flex-wrap items-center gap-2"><Badge variant="outline">正式人物画像</Badge><Badge variant="secondary">当前群</Badge><Badge variant="outline">好感以本群记录为准</Badge></div>


    <div className="grid gap-3 rounded-lg border bg-muted/10 p-3.5">
      <div><span className="mb-0.5 block text-xs text-muted-foreground">显示名称</span><span className="break-words font-medium">{item.display_name}</span></div>
      <div><span className="mb-0.5 block text-xs text-muted-foreground">用户 ID</span><span className="break-all font-mono text-xs">{item.user_id}</span></div>
      <div><span className="mb-0.5 block text-xs text-muted-foreground">所在群</span><span className="break-all font-mono text-xs">{item.bot_id} · {item.group_id}</span></div>
    </div>

    <div className="grid grid-cols-2 gap-3 rounded-lg border p-3.5">
      <div><span className="mb-0.5 block text-xs text-muted-foreground">互动数</span><span className="font-mono font-medium">{interactionCount(item) ?? '未记录'}</span></div>
      <div><span className="mb-0.5 block text-xs text-muted-foreground">好感</span>
        {actualAffinity !== null ? (
          <Badge className={`text-xs font-mono font-semibold ${
            actualAffinity >= 15 ? 'bg-rose-500 text-white' :
            actualAffinity >= 5 ? 'bg-pink-500 text-white' :
            actualAffinity > 0 ? 'bg-pink-400/80 text-white' :
            actualAffinity < 0 ? 'bg-blue-500 text-white' : 'bg-muted text-muted-foreground'
          }`}>
            {formatSignedDisplayNumber(actualAffinity)}
          </Badge>
        ) : (
          <Badge variant="outline">未记录</Badge>
        )}
      </div>
      <div className="col-span-2"><span className="mb-1.5 block text-xs text-muted-foreground">登记别名</span><div className="flex flex-wrap gap-1">{aliases.length ? aliases.map((alias) => <Badge key={alias} variant="outline" className="font-normal">{alias}</Badge>) : <span className="text-muted-foreground">未登记别名</span>}</div></div>
    </div>

    {impression || (Array.isArray(item.metadata?.impression_history) && item.metadata.impression_history.length > 0) || (Array.isArray(item.metadata?.impression_ledger) && item.metadata.impression_ledger.length > 0) ? (
      <div className="rounded-lg border border-amber-500/25 bg-amber-500/[0.06] p-3.5">
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="border-amber-500/40 text-amber-600 dark:text-amber-400">Bot 当前印象</Badge>
          <span className="text-[10px] text-muted-foreground">我眼中的他；下方可按类型与关键词浏览该群友当前群的全部印象、好感与人物事实</span>
          {impression ? (
            <Button type="button" size="sm" variant="outline" disabled={clearing} onClick={() => void handleClearImpression()}>
              {clearing ? '清除中…' : '清除当前印象'}
            </Button>
          ) : null}
        </div>
        {impression ? <p className="whitespace-pre-wrap break-words text-sm text-foreground/90">{impression}</p> : <p className="text-sm text-muted-foreground">当前印象已清除，历史仍保留。</p>}
        {clearError ? <p className="mt-2 text-xs text-destructive">{clearError}</p> : null}
        <ImpressionTimelinePanel query={{ bot_id: query.bot_id, session_id: query.session_id, visibility: 'group' }} userId={item.user_id} />
      </div>
    ) : null}

    <div className="flex flex-wrap gap-2 text-xs">
      <Link className="text-primary hover:underline" to={soulHref}>Bot 经历时间线</Link>
      <Link className="text-primary hover:underline" to={experienceHref}>经历片段</Link>
    </div>

    {relationshipError ? <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>关系读取失败</AlertTitle><AlertDescription>{relationshipError}</AlertDescription></Alert> : relationship ? <><RelationshipRadarCard values={relationship.values} /><RelationshipCalibrationPanel item={relationship} query={query} onChanged={onChanged} /></> : <Alert><AlertCircleIcon /><AlertTitle>当前关系未知</AlertTitle><AlertDescription>当前群没有正式关系记录，不会用其他群的好感或默认 0 顶上。</AlertDescription></Alert>}

    {relationship ? (
      <RelationshipTrajectoryCard
        botId={query.bot_id}
        sessionId={query.session_id}
        subjectPrincipalId={relationship.subject_principal_id}
      />
    ) : null}

    {(relationship?.evidence_summaries?.length ?? 0) > 0 ? (
      <div className="rounded-lg border bg-muted/10 p-3.5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Badge variant="outline">可读历史摘要</Badge>
          <Badge variant="secondary">只读</Badge>
          <Badge variant="outline">不改变好感度</Badge>
        </div>
        <div className="grid gap-1.5 text-xs">
          {relationship!.evidence_summaries!.map((summary, index) => (
            <p key={`${index}-${summary.slice(0, 24)}`} className="rounded border bg-background/70 px-2 py-1.5 text-muted-foreground">
              {summary}
            </p>
          ))}
        </div>
        <p className="mt-2 text-[10px] text-muted-foreground">
          来自 formal evidence 中的 historical_audit_summary；与下方 legacy 事件审计表并列，不参与 affinity 计算。
        </p>
      </div>
    ) : null}

    <HistoricalAuditPanel
      query={{ bot_id: query.bot_id, session_id: query.session_id, visibility: 'group' }}
      userId={item.user_id}
    />

    {actualAffinity === null && (
      <Alert>
        <AlertCircleIcon />
        <AlertTitle>好感当前不可用</AlertTitle>
        <AlertDescription>当前群没有正式好感记录，不会用其他群的数字顶上。</AlertDescription>
      </Alert>
    )}

    <details className="rounded-lg border bg-muted/10 p-3 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium text-foreground">技术字段与安全边界</summary>
      <div className="mt-3 grid gap-2">
        <p>复合键：<span className="break-all font-mono">user_id + group_id + bot_id</span>。列表键为当前响应的 scope_key，不用于裸 ID mutation。</p>
        <p>服务端另返回 {metadataCount} 项画像元数据；主界面不直出内部 JSON，也不会将不同 Bot 或群中的同名用户合并。</p>
      </div>
    </details>
  </div>
}

export function PeoplePage() {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const search = pagination.searchParams.get('search') ?? ''
  const relationshipState = (pagination.searchParams.get('relationship_state') as 'all' | 'known' | 'unknown' | null) ?? 'all'
  const minAffinity = pagination.searchParams.get('min_affinity') ?? ''
  const maxAffinity = pagination.searchParams.get('max_affinity') ?? ''
  const minInteractions = pagination.searchParams.get('min_interactions') ?? ''
  const aliasFilter = (pagination.searchParams.get('alias_filter') as 'all' | 'has' | 'none' | null) ?? 'all'
  const sortBy = (pagination.searchParams.get('sort_by') as 'name' | 'interactions' | 'affinity' | null) ?? 'name'
  const sortOrder = (pagination.searchParams.get('sort_order') as 'asc' | 'desc' | null) ?? defaultSortOrder(sortBy)
  const objectRef = pagination.searchParams.get('ref') ?? ''
  const objectSubjectId = pagination.searchParams.get('subject_principal_id') ?? ''
  const deepLinkKey = objectRef && objectSubjectId ? `${objectRef}|${objectSubjectId}` : ''
  const legacyScope = sessionId.startsWith('legacy:') ? sessionId.split(':') : null
  const legacyGroupId = legacyScope && legacyScope.length >= 3 ? legacyScope.slice(2).join(':') : ''
  const isLegacyScope = Boolean(legacyGroupId)
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters, enabled: !isLegacyScope })
  const listFilters = useMemo(() => ({
    search: search || undefined,
    relationship_state: relationshipState !== 'all' ? relationshipState : undefined,
    min_affinity: minAffinity || undefined,
    max_affinity: maxAffinity || undefined,
    min_interactions: minInteractions || undefined,
    alias_filter: aliasFilter !== 'all' ? aliasFilter : undefined,
    sort_by: sortBy !== 'name' ? sortBy : undefined,
    // 数字排序必须显式带方向：旧后端缺省是升序，省略 desc 会把「高→低」排成低到高。
    sort_order: sortBy === 'name'
      ? (sortOrder !== 'asc' ? sortOrder : undefined)
      : sortOrder,
  }), [aliasFilter, maxAffinity, minAffinity, minInteractions, relationshipState, search, sortBy, sortOrder])
  const hasAdvancedFilters = Boolean(listFilters.relationship_state || listFilters.min_affinity || listFilters.max_affinity || listFilters.min_interactions || listFilters.alias_filter || listFilters.sort_by || listFilters.sort_order)

  const [searchDraft, setSearchDraft] = useState(search)
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(hasAdvancedFilters)
  const [data, setData] = useState<PageResponse<PersonItem> | null>(null)
  const [relationshipData, setRelationshipData] = useState<PageResponse<RelationshipItem> | null>(null)
  const [relationshipError, setRelationshipError] = useState<string | null>(null)
  const [detailRelationship, setDetailRelationship] = useState<RelationshipItem | null>(null)
  const [detailRelationshipError, setDetailRelationshipError] = useState<string | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const [selectedPerson, setSelectedPerson] = useState<PersonItem | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const handledDeepLinkRef = useRef('')

  function applyListFilters(next: Record<string, string | null>) {
    pagination.setFilters(next)
    setShowAdvancedFilters(true)
  }

  function applyAffinityPreset(min: string, max: string) {
    applyListFilters({ relationship_state: 'known', min_affinity: min || null, max_affinity: max || null })
  }

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])

  useEffect(() => { setSearchDraft(search) }, [search])
  useEffect(() => {
    if (!botId || !sessionId) {
      setData(null)
      setRelationshipData(null)
      setRelationshipError(null)
      setLoading(false)
      setError(undefined)
      return
    }
    let active = true
    setLoading(true)
    setError(undefined)
    setRelationshipError(null)
    const peopleRequest = isLegacyScope
      ? getLegacyPeople({ bot_id: botId, group_id: legacyGroupId, search: search || undefined, limit: pagination.limit, offset: pagination.offset })
      : getPeople({ bot_id: botId, session_id: sessionId, visibility: 'group', ...listFilters, limit: pagination.limit, offset: pagination.offset })
    peopleRequest
      .then((people) => { if (active) setData(people) })
      .catch((reason: unknown) => {
        if (!active) return
        setData(null)
        setError(reason)
      })
      .finally(() => { if (active) setLoading(false) })
    if (isLegacyScope) {
      setRelationshipData(null)
      setRelationshipError('未绑定群只读，不能读取或校准正式关系')
      return () => { active = false }
    }
    getRelationships({ bot_id: botId, session_id: sessionId, visibility: 'group', ...listFilters, limit: pagination.limit, offset: pagination.offset })
      .then((relationships) => {
        if (!active) return
        setRelationshipData(relationships)
        setRelationshipError(null)
      })
      .catch((reason: unknown) => {
        if (!active) return
        setRelationshipData(null)
        setRelationshipError(humanizeApiError(reason, '关系读取失败'))
      })
    return () => { active = false }
  }, [botId, isLegacyScope, legacyGroupId, listFilters, pagination.limit, pagination.offset, reload, sessionId])

  useEffect(() => {
    if (!detailOpen || !selectedPerson || !botId || !sessionId || isLegacyScope) {
      setDetailRelationship(null)
      setDetailRelationshipError(isLegacyScope ? '未绑定群只读，不能校准好感度' : null)
      return
    }
    let active = true
    setDetailRelationshipError(null)
    getRelationships({
      bot_id: botId,
      session_id: sessionId,
      visibility: 'group',
      user_id: selectedPerson.user_id,
      limit: 25,
      offset: 0,
    })
      .then((page) => {
        if (!active) return
        setDetailRelationship(page.items.find((entry) => entry.person.user_id === selectedPerson.user_id) ?? null)
      })
      .catch((reason: unknown) => {
        if (!active) return
        setDetailRelationship(null)
        setDetailRelationshipError(humanizeApiError(reason, '关系读取失败'))
      })
    return () => { active = false }
  }, [botId, detailOpen, isLegacyScope, reload, selectedPerson, sessionId])

  useEffect(() => {
    if (!deepLinkKey) {
      handledDeepLinkRef.current = ''
      return
    }
    if (isLegacyScope || !relationshipData || handledDeepLinkRef.current === deepLinkKey) return
    const match = relationshipData.items.find((entry) => entry.subject_principal_id === objectSubjectId && entry.object_ref?.ref === objectRef)
    if (!match) return
    handledDeepLinkRef.current = deepLinkKey
    setSelectedPerson(match.person)
    setDetailRelationship(match)
    setDetailRelationshipError(null)
    setDetailOpen(true)
  }, [deepLinkKey, isLegacyScope, objectRef, objectSubjectId, relationshipData])

  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    pagination.setFilters({ search: searchDraft.trim() || null })
  }
  const clearSearch = () => {
    setSearchDraft('')
    pagination.setFilters({
      search: null,
      relationship_state: null,
      min_affinity: null,
      max_affinity: null,
      min_interactions: null,
      alias_filter: null,
      sort_by: null,
      sort_order: null,
    })
  }
  const openDetail = (item: PersonItem) => {
    setSelectedPerson(item)
    setDetailOpen(true)
  }

  const people = useMemo(() => (data?.items ?? []).map((person) => {
    const relation = relationshipData?.items.find((entry) => entry.person.user_id === person.user_id)
    const affinity = relation?.affinity ?? (typeof person.affinity === 'number' && Number.isFinite(person.affinity) ? person.affinity : null)
    return { ...person, relation: relation || null, affinity }
  }), [data, relationshipData])

  const aliasCount = data ? people.reduce((sum, item) => sum + aliasLabels(item.aliases).length, 0) : '—'
  const interactionTotal = data && people.every((item) => interactionCount(item) !== null) ? people.reduce((sum, item) => sum + interactionCount(item)!, 0) : '未提供'
  const total = data?.page.total_status === 'exact' && data.page.total !== null ? data.page.total : (data?.items.length ?? '—')
  const status = !botId || !sessionId ? 'unknown' : loading ? 'loading' : error ? 'error' : !people.length ? 'empty' : 'success'

  // 计算本页平均/整体的 Affinity 指标
  const activeRelationships = relationshipData?.items.filter(r => r.affinity !== null) ?? []
  const averageAffinity = activeRelationships.length > 0
    ? (activeRelationships.reduce((sum, r) => sum + (r.affinity ?? 0), 0) / activeRelationships.length).toFixed(1)
    : '不可用'

  return <div className="flex flex-col gap-4" data-page="people">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <header className="max-w-2xl">
        <h1 className="text-xl font-bold tracking-tight">人物与关系画像</h1>
        <p className="text-xs text-muted-foreground">按当前 Bot 和群查看身份、别名与互动；同名用户不会跨 Bot 或跨群合并。点开人物可在详情里按类型与关键词全量浏览「我眼中的他」印象时间线。</p>
      </header>
      <div className="flex flex-wrap gap-2">
        <Button asChild size="sm" variant="outline">
          <Link to={scopedHref('/soul', pagination.searchParams.toString())}><SparklesIcon data-icon="inline-start" aria-hidden="true" />Bot 经历时间线</Link>
        </Button>
        <SummaryTile label="筛选人物" value={loading ? '…' : total} />
        <SummaryTile label="本页别名" value={loading ? '…' : aliasCount} tone="text-pink-600" />
        <SummaryTile label="本页互动" value={loading ? '…' : interactionTotal} tone="text-blue-600" />
        <SummaryTile label="本页平均好感" value={loading ? '…' : averageAffinity} tone="text-rose-600" />
      </div>
    </div>

    <Card className="overflow-hidden border-border/60">
      <CardContent className="p-0">
        <div className="flex flex-col gap-3 bg-muted/[0.035] p-3">
          <div className="flex flex-wrap items-end gap-2" data-slot="people-scope-context">
            <Badge variant="outline" className="mb-0.5 h-7">当前群</Badge>
            <ScopeSelect className="min-w-48 flex-1 xl:max-w-64" value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择 Bot" required onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null })} />
            <ScopeSelect className="min-w-56 flex-[1.4] xl:max-w-80" value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择该 Bot 的群" disabled={!botId} required onValueChange={(value) => pagination.setFilters({ session_id: value })} />
            <span className="pb-1 text-[10px] text-muted-foreground">这里的 Bot 不是 QQ 号</span>
          </div>
          {isLegacyScope ? (
            <Alert>
              <AlertCircleIcon />
              <AlertTitle>未绑定群只读</AlertTitle>
              <AlertDescription>这个群有人物或旧记忆，但还没有正式绑定。可以查看名单，不能改好感，也不会把数据写成新群身份。</AlertDescription>
            </Alert>
          ) : null}

          <form className="flex flex-col gap-3" onSubmit={submitSearch}>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-60 flex-1 xl:max-w-xl">
                <SearchIcon className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input aria-label="搜索人物" className="h-8 pl-8 text-xs" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索用户 ID、昵称或登记别名" disabled={!botId || !sessionId} />
              </div>
              <Button type="submit" size="sm" className="h-8 text-xs" disabled={loading || !botId || !sessionId}>搜索</Button>
              <Button type="button" size="sm" className="h-8 text-xs" variant="ghost" onClick={clearSearch}>清除</Button>
              <Button type="button" size="sm" className="h-8 text-xs gap-1.5" variant="outline" onClick={() => setShowAdvancedFilters(!showAdvancedFilters)} disabled={!botId || !sessionId}>
                <SlidersHorizontalIcon className="size-3.5" />
                高级筛选
              </Button>
              <Button type="button" size="icon-sm" className="h-8 w-8" variant="outline" disabled={loading || !botId || !sessionId} onClick={() => setReload((value) => value + 1)} aria-label="刷新人物画像">
                <RefreshCwIcon className={loading ? 'animate-spin' : undefined} aria-hidden="true" />
              </Button>
              <span className="ml-auto text-xs text-muted-foreground">{data?.page ? `当前第 ${Math.floor(pagination.offset / pagination.limit) + 1} 页` : '请先选择 Bot 和群'}</span>
            </div>

            {showAdvancedFilters && (
              <div className="grid gap-3 rounded-lg border bg-muted/20 p-3.5 text-xs animate-in fade-in slide-in-from-top-2 duration-150">
                <div className="flex items-center justify-between text-[11px] text-muted-foreground border-b pb-2">
                  <span>筛选和排序发给服务端，按全群结果分页；总数是匹配人数，不是当前页人数。</span>
                  <span className="font-mono">本页 {people.length} 人</span>
                </div>
                <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
                  {/* 关系激活状态 */}
                  <div className="flex flex-col gap-1.5">
                    <span className="font-semibold text-muted-foreground">关系记录</span>
                    <select
                      className="h-8 rounded-md border bg-background px-2 text-xs"
                      value={relationshipState}
                      onChange={(e) => applyListFilters({ relationship_state: e.target.value === 'all' ? null : e.target.value })}
                    >
                      <option value="all">全部人物</option>
                      <option value="known">仅已记录关系</option>
                      <option value="unknown">仅未记录关系</option>
                    </select>
                  </div>

                  {/* 好感范围 */}
                  <div className="flex flex-col gap-1.5">
                    <span className="font-semibold text-muted-foreground">好感范围</span>
                    <div className="flex items-center gap-1.5">
                      <Input
                        type="number"
                        placeholder="最小"
                        className="h-8 text-xs font-mono"
                        value={minAffinity}
                        onChange={(e) => applyListFilters({ min_affinity: e.target.value || null, relationship_state: relationshipState === 'unknown' ? 'known' : (relationshipState === 'all' ? null : relationshipState) })}
                        disabled={relationshipState === 'unknown'}
                      />
                      <span className="text-muted-foreground">-</span>
                      <Input
                        type="number"
                        placeholder="最大"
                        className="h-8 text-xs font-mono"
                        value={maxAffinity}
                        onChange={(e) => applyListFilters({ max_affinity: e.target.value || null, relationship_state: relationshipState === 'unknown' ? 'known' : (relationshipState === 'all' ? null : relationshipState) })}
                        disabled={relationshipState === 'unknown'}
                      />
                    </div>
                    <div className="flex flex-wrap gap-1">
                      <Button type="button" size="sm" variant="outline" className="h-6 px-1.5 text-[10px]" disabled={relationshipState === 'unknown'} onClick={() => applyAffinityPreset('15', '')}>高好感 ≥15</Button>
                      <Button type="button" size="sm" variant="outline" className="h-6 px-1.5 text-[10px]" disabled={relationshipState === 'unknown'} onClick={() => applyAffinityPreset('5', '14')}>中好感</Button>
                      <Button type="button" size="sm" variant="outline" className="h-6 px-1.5 text-[10px]" disabled={relationshipState === 'unknown'} onClick={() => applyAffinityPreset('', '-1')}>负好感</Button>
                    </div>
                  </div>

                  {/* 最少互动数 */}
                  <div className="flex flex-col gap-1.5">
                    <span className="font-semibold text-muted-foreground">最少互动次数</span>
                    <Input
                      type="number"
                      placeholder="例如 10"
                      className="h-8 text-xs font-mono"
                      value={minInteractions}
                      onChange={(e) => applyListFilters({ min_interactions: e.target.value || null })}
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <span className="font-semibold text-muted-foreground">别名</span>
                    <select className="h-8 rounded-md border bg-background px-2 text-xs" value={aliasFilter} onChange={(e) => applyListFilters({ alias_filter: e.target.value === 'all' ? null : e.target.value })}>
                      <option value="all">不限</option>
                      <option value="has">有登记别名</option>
                      <option value="none">无别名</option>
                    </select>
                  </div>

                  {/* 排序属性与方向 */}
                  <div className="flex flex-col gap-1.5">
                    <span className="font-semibold text-muted-foreground">列表排序</span>
                    <div className="flex items-center gap-1.5">
                      <select
                        aria-label="列表排序字段"
                        className="h-8 flex-1 rounded-md border bg-background px-2 text-xs"
                        value={sortBy}
                        onChange={(e) => {
                          const next = e.target.value as 'name' | 'interactions' | 'affinity'
                          applyListFilters({
                            sort_by: next === 'name' ? null : next,
                            sort_order: null,
                          })
                        }}
                      >
                        <option value="name">显示名称</option>
                        <option value="interactions">互动次数</option>
                        <option value="affinity">好感</option>
                      </select>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8 shrink-0 px-2 text-[10px]"
                        aria-label={sortOrder === 'desc' ? '当前从高到低，点击改为从低到高' : '当前从低到高，点击改为从高到低'}
                        onClick={() => {
                          const next = sortOrder === 'asc' ? 'desc' : 'asc'
                          applyListFilters({ sort_order: next === defaultSortOrder(sortBy) ? null : next })
                        }}
                      >
                        <ArrowUpDownIcon className={`size-3.5 transition-transform ${sortOrder === 'desc' ? 'rotate-180' : ''}`} />
                        {sortBy === 'name' ? (sortOrder === 'desc' ? 'Z→A' : 'A→Z') : (sortOrder === 'desc' ? '高→低' : '低→高')}
                      </Button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </form>
        </div>

        <Separator />

        <div className="flex flex-col gap-3 p-3">
          <QueryState
            status={status}
            error={error}
            title="人物画像读取失败"
            description={!botId || !sessionId ? '请先选择 Bot 和群；页面不会读取跨群人物。' : undefined}
            emptyTitle="当前群没有匹配的人物画像"
            emptyDescription="当前群和搜索条件下没有正式人物画像。换一个关键词或清除筛选试试；这里只读当前 Bot、当前群，不会跨群汇总。"
            onRetry={() => setReload((value) => value + 1)}
          >
            <DeclarativeDataTable
              label="人物画像清单"
              items={people}
              keyExtractor={(row) => row.scope_key}
              onRowClick={(row) => openDetail(row)}
              columns={[
                { key: 'user_id', header: '用户 ID', className: 'max-w-44 truncate py-1 font-mono text-[11px]', render: (row) => row.user_id },
                { key: 'display_name', header: '显示名称', isTitle: true, className: 'max-w-44 truncate py-1 text-xs font-medium', render: (row) => row.display_name },
                { key: 'aliases', header: '登记别名', className: 'max-w-48 truncate py-1 text-xs text-muted-foreground', render: (row) => { const aliases = aliasLabels(row.aliases); return aliases.length ? aliases.join('、') : '未登记' } },
                { key: 'impression', header: 'Bot 印象', className: 'max-w-56 truncate py-1 text-xs text-muted-foreground', render: (row) => impressionOf(row) || '—' },
                { key: 'group', header: '群', className: 'max-w-36 truncate py-1 font-mono text-[11px]', render: (row) => row.group_id },
                { key: 'bot', header: 'Bot', className: 'max-w-32 truncate py-1', render: (row) => <Badge variant="secondary" className="max-w-full truncate px-1.5 font-mono text-[10px] font-normal">{row.bot_id}</Badge> },
                { key: 'count', header: '互动数', className: 'py-1 text-center font-mono text-[11px]', render: (row) => interactionCount(row) ?? '—' },
                { key: 'affinity', header: '好感', className: 'py-1', render: (row) => { if (relationshipError) return <Badge variant="outline" className="text-[10px] text-destructive">关系读取失败</Badge>; const has = row.affinity !== null; return has ? <Badge className={`text-[10px] font-mono font-semibold ${row.affinity! >= 15 ? 'bg-rose-500 text-white' : row.affinity! >= 5 ? 'bg-pink-500 text-white' : row.affinity! > 0 ? 'bg-pink-400/80 text-white' : row.affinity! < 0 ? 'bg-blue-500 text-white' : 'bg-muted text-muted-foreground'}`}>{formatSignedDisplayNumber(row.affinity)}</Badge> : <Badge variant="outline" className="text-[10px] text-muted-foreground">未记录</Badge> } },
                { key: 'actions', header: null, hideOnMobile: false, render: (row) => <Button type="button" variant="ghost" size="icon-xs" aria-label={`查看 ${row.display_name} 详情`} onClick={(event) => { event.stopPropagation(); openDetail(row) }}><EyeIcon aria-hidden="true" /></Button> },
              ]}
            />
          </QueryState>
          {data?.page ? <PaginationControls page={data.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={loading} label="人物分页" /> : null}
        </div>
      </CardContent>
    </Card>


    <Sheet
      open={detailOpen}
      onOpenChange={(open) => {
        setDetailOpen(open)
        if (!open && deepLinkKey) {
          pagination.setFilters({ ref: null, object_id: null, subject_principal_id: null }, true)
        }
      }}
    >
      <SheetContent className="w-[min(94vw,34rem)] sm:max-w-xl">
        <SheetHeader className="border-b pr-12"><SheetTitle>人物画像详情</SheetTitle><SheetDescription>只读查看当前群里的身份、别名、互动与好感。</SheetDescription></SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{selectedPerson ? <PersonDetail item={selectedPerson} relationship={detailRelationship} relationshipError={detailRelationshipError} query={{ bot_id: botId, session_id: sessionId, visibility: 'group', user_id: selectedPerson.user_id }} onChanged={() => setReload((value) => value + 1)} /> : null}</div>
      </SheetContent>
    </Sheet>
  </div>
}

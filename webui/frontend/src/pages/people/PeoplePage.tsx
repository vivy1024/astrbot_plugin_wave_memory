import { useCallback, useEffect, useState, useMemo, useRef, type FormEvent } from 'react'
import { AlertCircleIcon, EyeIcon, RefreshCwIcon, SearchIcon, SlidersHorizontalIcon, ArrowUpDownIcon } from 'lucide-react'

import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import {
  clearImpression,
  getLegacyPeople,
  getPeople,
  getRelationshipHistoricalAudit,
  getRelationships,
  type HistoricalAuditPage,
  type PersonItem,
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

function aliasLabels(aliases: unknown[]): string[] {
  return aliases.map((alias) => typeof alias === 'string' ? alias : '').filter(Boolean)
}

function interactionCount(item: PersonItem): number | null {
  if (typeof item.interaction_count === 'number' && Number.isFinite(item.interaction_count)) return item.interaction_count
  const registryCount = item.person_registry?.message_count
  return typeof registryCount === 'number' && Number.isFinite(registryCount) ? registryCount : null
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
          setError(reason instanceof Error ? reason.message : '历史审计读取失败')
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
    .map((item) => `${item.event_type}×${item.count}`)
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
        <p className="text-xs text-muted-foreground">当前群没有正式关系事件。旧审计表不存在时不会用假数据填满。</p>
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

function impressionEventText(entry: Record<string, unknown>): string {
  const event = entry.event
  if (!event || typeof event !== 'object' || Array.isArray(event)) return ''
  const payload = event as Record<string, unknown>
  const type = typeof payload.event_type === 'string' ? payload.event_type.trim() : ''
  const reason = typeof payload.reason === 'string' ? payload.reason.trim() : ''
  const dimension = typeof payload.dimension === 'string' ? payload.dimension.trim() : ''
  const delta = typeof payload.delta === 'number' && Number.isFinite(payload.delta) ? formatSignedDisplayNumber(payload.delta) : ''
  return [type, reason, dimension && delta ? `${dimension}${delta}` : ''].filter(Boolean).join(' · ')
}

function impressionLedgerOf(item: PersonItem): Array<{ eventType: string; dimension: string; delta: string; reason: string; at: string }> {
  const raw = item.metadata?.impression_ledger
  if (!Array.isArray(raw)) return []
  return raw
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === 'object' && !Array.isArray(entry))
    .map((entry) => ({
      eventType: typeof entry.event_type === 'string' ? entry.event_type.trim() : '',
      dimension: typeof entry.dimension === 'string' ? entry.dimension.trim() : '',
      delta: typeof entry.delta === 'number' && Number.isFinite(entry.delta) ? formatSignedDisplayNumber(entry.delta) : '',
      reason: typeof entry.reason === 'string' ? entry.reason.trim() : '',
      at: formatImpressionTime(entry.at),
    }))
    .filter((entry) => entry.eventType && entry.eventType !== 'message_seen' && entry.reason !== '看见一条群友消息' && entry.reason !== '消息带来趣味感' && entry.reason !== '行为统计关系变化')
    .reverse()
}

function impressionHistoryOf(item: PersonItem): Array<{ text: string; at: string; reason: string; event: string }> {
  const raw = item.metadata?.impression_history
  if (!Array.isArray(raw)) return []
  return raw
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === 'object' && !Array.isArray(entry))
    .map((entry) => ({
      text: typeof entry.text === 'string' ? entry.text.trim() : '',
      at: formatImpressionTime(entry.cleared_at ?? entry.superseded_at ?? entry.updated_at),
      reason: typeof entry.cleared_reason === 'string' ? entry.cleared_reason.trim() : '',
      event: impressionEventText(entry),
    }))
    .filter((entry) => entry.text)
    .reverse()
}

function PersonDetail({ item, relationship, relationshipError, query, onChanged }: { item: PersonItem; relationship: RelationshipItem | null; relationshipError?: string | null; query: { bot_id: string; session_id: string; visibility: 'group'; user_id?: string }; onChanged?: () => void }) {
  const aliases = aliasLabels(item.aliases)
  const metadataCount = Object.keys(item.metadata ?? {}).length + Object.keys(item.registry_metadata ?? {}).length
  const actualAffinity = relationship?.affinity !== undefined && relationship?.affinity !== null ? relationship.affinity : null
  const impression = impressionOf(item)
  const impressionHistory = impressionHistoryOf(item)
  const impressionLedger = impressionLedgerOf(item)
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
      setClearError(error instanceof Error ? error.message : '清除印象失败')
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

    {impression || impressionHistory.length || impressionLedger.length ? (
      <div className="rounded-lg border border-amber-500/25 bg-amber-500/[0.06] p-3.5">
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="border-amber-500/40 text-amber-600 dark:text-amber-400">Bot 当前印象</Badge>
          <span className="text-[10px] text-muted-foreground">由主对话自动生成、随互动更新；历史只读，不进入关系事件</span>
          {impression ? (
            <Button type="button" size="sm" variant="outline" disabled={clearing} onClick={() => void handleClearImpression()}>
              {clearing ? '清除中…' : '清除当前印象'}
            </Button>
          ) : null}
        </div>
        {impression ? <p className="whitespace-pre-wrap break-words text-sm text-foreground/90">{impression}</p> : <p className="text-sm text-muted-foreground">当前印象已清除，历史仍保留。</p>}
        {clearError ? <p className="mt-2 text-xs text-destructive">{clearError}</p> : null}
        <div className="mt-3 border-t border-amber-500/20 pt-3">
          <p className="mb-2 text-[10px] text-muted-foreground">印象时间线（新在上，只读）</p>
          {impressionHistory.length ? (
            <ol className="flex flex-col gap-2">
              {impressionHistory.map((entry, index) => (
                <li key={`${entry.text}-${entry.at}-${index}`} className="rounded-md bg-background/60 px-2.5 py-2">
                  <p className="whitespace-pre-wrap break-words text-sm">{entry.text}</p>
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    {[entry.at, entry.event, entry.reason ? `清除原因：${entry.reason}` : ''].filter(Boolean).join(' · ') || '历史记录'}
                  </p>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-xs text-muted-foreground">尚无演变记录。旧画像只有当前一句时不会补历史；下次印象更新或手动清除后才会写入时间线。</p>
          )}
        </div>
        <div className="mt-3 border-t border-amber-500/20 pt-3">
          <p className="mb-2 text-[10px] text-muted-foreground">关系账本（事件 + 加减分，新在上）</p>
          {impressionLedger.length ? (
            <ol className="flex flex-col gap-2">
              {impressionLedger.map((entry, index) => (
                <li key={`${entry.eventType}-${entry.dimension}-${entry.at}-${index}`} className="rounded-md bg-background/60 px-2.5 py-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant="outline" className="font-mono text-[10px]">{entry.eventType}</Badge>
                    <Badge variant="secondary" className="font-mono text-[10px]">{entry.dimension}{entry.delta}</Badge>
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground">{[entry.reason, entry.at].filter(Boolean).join(' · ') || '账本记录'}</p>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-xs text-muted-foreground">尚无带分数的关系账本。路过看见不会入账。</p>
          )}
        </div>
      </div>
    ) : null}

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
  const sortOrder = (pagination.searchParams.get('sort_order') as 'asc' | 'desc' | null) ?? 'asc'
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
    sort_order: sortOrder !== 'asc' ? sortOrder : undefined,
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
        setRelationshipError(reason instanceof Error ? reason.message : '关系读取失败')
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
        setDetailRelationshipError(reason instanceof Error ? reason.message : '关系读取失败')
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
        <p className="text-xs text-muted-foreground">按当前 Bot 和群查看身份、别名与互动；同名用户不会跨 Bot 或跨群合并。</p>
      </header>
      <div className="flex flex-wrap gap-2">
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
                {hasAdvancedFilters && (
                  <Badge variant="secondary" className="px-1 py-0 h-4 min-w-4 text-[10px] bg-rose-500 text-white rounded-full">!</Badge>
                )}
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
                        className="h-8 flex-1 rounded-md border bg-background px-2 text-xs"
                        value={sortBy}
                        onChange={(e) => applyListFilters({ sort_by: e.target.value === 'name' ? null : e.target.value })}
                      >
                        <option value="name">显示名称</option>
                        <option value="interactions">互动次数</option>
                        <option value="affinity">好感</option>
                      </select>
                      <Button
                        type="button"
                        variant="outline"
                        size="icon-sm"
                        className="h-8 w-8 shrink-0"
                        onClick={() => applyListFilters({ sort_order: sortOrder === 'asc' ? 'desc' : null })}
                      >
                        <ArrowUpDownIcon className={`size-3.5 transition-transform ${sortOrder === 'desc' ? 'rotate-180' : ''}`} />
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
          <QueryState status={status} error={error} title="人物画像读取失败" description={!botId || !sessionId ? '请先选择 Bot 和群；页面不会读取跨群人物。' : '当前群和搜索条件下没有正式人物画像。'} onRetry={() => setReload((value) => value + 1)}>
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

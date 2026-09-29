import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { CheckIcon, LockIcon, MessageSquareQuoteIcon, NetworkIcon, ShieldCheckIcon, XIcon } from 'lucide-react'
import { toast } from 'sonner'
import { useGlobalScope } from '@/app/global-scope'

import {
  batchReviewFacts,
  getFactEvidence,
  listFacts,
  reviewFact,
  type FactBatchReviewAction,
  type FactConflictCandidate,
  type FactEvidencePayload,
  type FactReviewAction,
  type FactReviewHint,
  type FactVisibility,
  type ScopedFactItem,
  type ScopedFactsResponse,
} from '@/api/facts'
import { getScopeOptions, groupSessionOptions, isPrivateSessionId, scopeOptionsFor } from '@/api/options'
import {
  BatchActionBar,
  ChatContextEvidenceDialog,
  EvidenceList,
  HeroHeader,
  PaginationControls,
  QueryState,
  ResponsiveTable,
  ScopeFilterBar,
  usePaginationSearchParams,
} from '@/components/shared'
import { useCanonicalScopeDefault } from '@/hooks/use-pagination-search-params'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldLabel } from '@/components/ui/field'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { humanizeApiError } from '@/lib/reason-label'

const REVIEWABLE = new Set(['pending', 'quarantined', 'conflict'])
const ACTION_LABEL: Record<FactReviewAction, string> = { approve: '批准', reject: '拒绝', replace: '替换', coexist: '共存' }
const INEFFECTIVE_LABEL: Record<string, string> = {
  pending: '待审核', conflict: '冲突待裁决', rejected: '已拒绝', quarantined: '已隔离',
  deleted: '已删除', superseded: '已被取代', invalid_validity: '有效期无效', not_yet_valid: '尚未生效', expired: '已过期',
}

function canReview(fact: ScopedFactItem, action: FactReviewAction): boolean {
  return REVIEWABLE.has(fact.status) && fact.editable && Boolean(fact.capabilities?.review?.available)
    && Boolean(fact.object_ref?.ref) && Boolean(fact.review_actions?.includes(action))
}

function canBatchReview(fact: ScopedFactItem, action: FactBatchReviewAction): boolean {
  return canReview(fact, action) && Boolean(fact.capabilities?.batch_review?.available)
}

const HINT_LABEL: Record<FactReviewHint, string> = {
  '': '',
  suggest_approve: '建议批准',
  suggest_reject: '建议拒绝',
  needs_review: '需人工判断',
}

const HINT_TONE: Record<FactReviewHint, string> = {
  '': '',
  suggest_approve: 'text-emerald-600',
  suggest_reject: 'text-amber-600',
  needs_review: 'text-muted-foreground',
}

function confidence(value: number | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '未知'
}

function FactValidityTime({ value, emptyLabel }: { value: number | null | undefined; emptyLabel: string }) {
  if (value === null) return <>{emptyLabel}</>
  if (typeof value !== 'number' || !Number.isFinite(value)) return <>未记录</>
  const date = new Date(value * 1000)
  if (Number.isNaN(date.getTime())) return <>时间不可用</>
  return <time dateTime={date.toISOString()}>{date.toLocaleString('zh-CN')}</time>
}

function FactValidityBadge({ fact, now }: { fact: ScopedFactItem; now: number }) {
  const expired = typeof fact.valid_until === 'number' && Number.isFinite(fact.valid_until) && fact.valid_until <= now
  const upcoming = typeof fact.valid_from === 'number' && Number.isFinite(fact.valid_from) && fact.valid_from > now
  if (!expired && !upcoming) return null
  return <Badge variant="outline" className="text-xs text-amber-600" title="按本机时间辅助判断，保留服务端审核状态">{expired ? '已过期' : '尚未生效'}</Badge>
}

function FactEvidenceDialog({
  fact,
  scope,
  onClose,
}: {
  fact: ScopedFactItem | null
  scope: { bot_id: string; session_id: string; visibility: FactVisibility }
  onClose: () => void
}) {
  const [before, setBefore] = useState(15)
  const [after, setAfter] = useState(15)
  const [payload, setPayload] = useState<FactEvidencePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const evidenceRequest = useRef(0)

  const loadEvidence = useCallback(async (windowBefore: number, windowAfter: number) => {
    if (!fact) return
    const request = ++evidenceRequest.current
    setLoading(true)
    setError('')
    try {
      const next = await getFactEvidence(fact, scope, windowBefore, windowAfter)
      if (request === evidenceRequest.current) setPayload(next)
    } catch (reason) {
      if (request !== evidenceRequest.current) return
      setPayload(null)
      setError(humanizeApiError(reason, '事实证据加载失败'))
    } finally {
      if (request === evidenceRequest.current) setLoading(false)
    }
  }, [fact, scope])

  useEffect(() => {
    if (!fact) {
      evidenceRequest.current += 1
      setPayload(null)
      setLoading(false)
      setError('')
      return
    }
    setBefore(15)
    setAfter(15)
    void loadEvidence(15, 15)
  }, [fact, loadEvidence])

  const beforeCount = payload?.messages.filter((m) => m.role === 'before').length ?? 0
  const afterCount = payload?.messages.filter((m) => m.role === 'after').length ?? 0

  return (
    <ChatContextEvidenceDialog
      open={Boolean(fact)}
      onClose={onClose}
      title={`事实证据${fact ? ` · #${fact.id} ${fact.subject} ${fact.predicate} ${fact.object}` : ''}`}
      description="只还原当前 Bot 和群里提取该事实的前后聊天，高亮支撑原话，不跨群猜传。"
      before={before}
      after={after}
      onBeforeChange={setBefore}
      onAfterChange={setAfter}
      onRefresh={() => void loadEvidence(before, after)}
      loading={loading}
      error={error}
      sourceQuote={payload?.source_quote}
      messages={payload?.messages ?? []}
      anchorBadgeLabel="事实关联原话锚点"
      statusBadges={
        payload ? (
          <>
            <Badge variant={payload.used_fallback ? 'outline' : 'secondary'}>
              {payload.used_fallback ? '保存的原话快照' : '同作用域动态上下文'}
            </Badge>
            <Badge variant="outline">前 {beforeCount} / 后 {afterCount}</Badge>
            <Badge variant="outline">锚点 #{payload.anchor?.id ?? fact?.source_memory_id ?? '无'}</Badge>
          </>
        ) : null
      }
    />
  )
}

function FactDetail({
  fact,
  now,
  onReview,
  onOpenEvidence,
  busy,
}: {
  fact: ScopedFactItem
  now: number
  onReview: (fact: ScopedFactItem, action: FactReviewAction, target?: FactConflictCandidate) => void
  onOpenEvidence: (fact: ScopedFactItem) => void
  busy: boolean
}) {
  const reviewable = fact.review_actions?.some((action) => canReview(fact, action))
  const [targetId, setTargetId] = useState('')
  const targets = (fact.conflict_candidates ?? []).filter((candidate) => ['active', 'approved'].includes(candidate.status)
    && (candidate.valid_from == null || candidate.valid_from <= now)
    && (candidate.valid_until == null || candidate.valid_until > now))
  const target = targets.find((candidate) => String(candidate.id) === targetId)
  return <div className="flex flex-col gap-5 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="outline">事实 #{fact.id}</Badge>
      <Badge variant="secondary">{fact.status || '未知状态'}</Badge>
      <FactValidityBadge fact={fact} now={now} />
      <Badge variant={fact.evidence_status === 'available' ? 'default' : 'outline'}>{fact.evidence_status === 'available' ? '证据可溯源' : '证据不可用'}</Badge>
      {fact.review_hint ? <Badge variant="outline" className={HINT_TONE[fact.review_hint]}>{HINT_LABEL[fact.review_hint]}</Badge> : null}
    </div>

    <div className="grid gap-3 rounded-lg border bg-muted/10 p-3.5">
      <div><span className="mb-0.5 block text-xs text-muted-foreground">主体</span><span className="break-words font-medium">{fact.subject}</span></div>
      <div><span className="mb-0.5 block text-xs text-muted-foreground">关系</span><Badge variant="outline" className="font-normal">{fact.predicate}</Badge></div>
      <div><span className="mb-0.5 block text-xs text-muted-foreground">客体</span><span className="break-words font-medium">{fact.object}</span></div>
    </div>

    <dl className="grid grid-cols-2 gap-3 rounded-lg border p-3.5">
      <div><dt className="text-xs text-muted-foreground">置信度</dt><dd className="font-mono font-medium">{confidence(fact.confidence)}</dd></div>
      <div><dt className="text-xs text-muted-foreground">来源记忆</dt><dd className="font-mono">{fact.source_memory_id ? `#${fact.source_memory_id}` : '未关联'}</dd></div>
      <div><dt className="text-xs text-muted-foreground">有效期开始</dt><dd><FactValidityTime value={fact.valid_from} emptyLabel="未限定开始" /></dd></div>
      <div><dt className="text-xs text-muted-foreground">有效期截止</dt><dd><FactValidityTime value={fact.valid_until} emptyLabel="未限定截止" /></dd></div>
      <div className="col-span-2"><dt className="text-xs text-muted-foreground">服务端生效判定</dt><dd>{fact.ineffective_reason ? `未生效：${INEFFECTIVE_LABEL[fact.ineffective_reason] ?? fact.ineffective_reason}` : fact.effective ? '已生效' : '未记录'}</dd></div>
      <div className="col-span-2"><dt className="text-xs text-muted-foreground">当前群</dt><dd className="break-all font-mono text-xs">{fact.bot_id} · {fact.session_id}</dd></div>
    </dl>
    <p className="text-xs text-muted-foreground">时间按本机时区显示；到达截止时间即过期。有效期提示不改变服务端审核状态。</p>

    <div>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-medium">本群证据</h3>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 gap-1 text-xs"
          onClick={() => onOpenEvidence(fact)}
        >
          <MessageSquareQuoteIcon className="size-3.5" />
          查看聊天上下文
        </Button>
      </div>
      <EvidenceList evidence={fact.evidence} objectPath="/memories" emptyDescription="没有可验证的本群记忆证据；这不代表事实为假，但无法从此页面完成溯源。" />
    </div>

    {reviewable ? <div className="flex flex-wrap gap-2 rounded-lg border p-3.5">
      {canReview(fact, 'approve') ? <Button type="button" size="sm" disabled={busy} onClick={() => onReview(fact, 'approve')}><CheckIcon data-icon="inline-start" />批准事实</Button> : null}
      {canReview(fact, 'reject') ? <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onReview(fact, 'reject')}><XIcon data-icon="inline-start" />拒绝</Button> : null}
      {canReview(fact, 'coexist') ? <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onReview(fact, 'coexist')}>批准共存</Button> : null}
      {canReview(fact, 'replace') ? <div className="flex w-full flex-wrap gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1">替换的旧事实<select aria-label="替换的旧事实" className="min-w-0 rounded-md border bg-background p-2" value={targetId} disabled={busy} onChange={(event) => setTargetId(event.target.value)}><option value="">请选择旧事实</option>{targets.map((candidate) => <option key={candidate.id} value={candidate.id}>#{candidate.id} {candidate.subject} {candidate.predicate} {candidate.object}</option>)}</select></label>
        <Button type="button" size="sm" disabled={busy || !target} onClick={() => onReview(fact, 'replace', target)}>批准并替换</Button>
      </div> : null}
      {fact.conflict_candidates?.length ? <ul className="w-full text-xs text-muted-foreground" aria-label="冲突事实">{fact.conflict_candidates.map((candidate) => <li key={candidate.id}>#{candidate.id} {candidate.subject} {candidate.predicate} {candidate.object}</li>)}</ul> : null}
      <p className="w-full text-xs text-muted-foreground">批准仍需通过冲突与有效期检查；共存保留旧事实，替换会使选中的旧事实失效。</p>
    </div> : null}

    <details className="rounded-lg border bg-muted/10 p-3 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium text-foreground">技术与安全边界</summary>
      <p className="mt-2 leading-relaxed">事实编号只用于本页查看。证据入口只打开服务端给出的本群记忆，不会跨 Bot、跨群或把已隔离内容补进来。</p>
    </details>
  </div>
}

export function FactsPage() {
  const scope = useGlobalScope()
  if (scope.status !== 'ready') return <QueryState status={scope.status === 'error' ? 'error' : 'loading'} title="事实作用域" error={scope.optionsError} onRetry={scope.reloadOptions} />
  return <FactsScopePage key={JSON.stringify([scope.botId, scope.sessionId, scope.visibility])} useDefaults={!scope.inProvider} />
}

function FactsScopePage({ useDefaults }: { useDefaults: boolean }) {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const search = pagination.searchParams.get('search') ?? ''
  const statusFilter = pagination.searchParams.get('status') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters, enabled: useDefaults })

  const [filterDraft, setFilterDraft] = useState({ search, status: statusFilter })
  const [data, setData] = useState<ScopedFactsResponse | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const [selectedFact, setSelectedFact] = useState<ScopedFactItem | null>(null)
  const [evidenceFact, setEvidenceFact] = useState<ScopedFactItem | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [busyIds, setBusyIds] = useState<number[]>([])
  const [batchBusy, setBatchBusy] = useState(false)
  const pendingReviews = useRef(new Set<number>())
  const mounted = useRef(true)
  const [now, setNow] = useState(() => Date.now() / 1000)

  useEffect(() => {
    mounted.current = true
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 60_000)
    return () => { mounted.current = false; window.clearInterval(timer) }
  }, [])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    // 私聊里得知的事实同样需要审核，私聊会话在本页可选。
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId, { allowPrivate: true })
  }, [botId])

  const visibility: FactVisibility = isPrivateSessionId(sessionId) ? 'private' : 'group'
  useEffect(() => { setFilterDraft({ search, status: statusFilter }) }, [search, statusFilter])
  useEffect(() => {
    if (!botId || !sessionId) { setData(null); setLoading(false); setError(undefined); return }
    let active = true
    setLoading(true)
    setData(null)
    setSelectedIds([])
    setError(undefined)
    listFacts({
      bot_id: botId,
      session_id: sessionId,
      visibility,
      search: search || undefined,
      status: statusFilter || undefined,
      limit: pagination.limit,
      offset: pagination.offset,
    })
      .then((value) => { if (active) { setData(value); setSelectedIds([]) } })
      .catch((reason: unknown) => { if (active) { setData(null); setError(reason) } })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [botId, pagination.limit, pagination.offset, reload, search, sessionId, statusFilter, visibility])

  const scope = useMemo(() => ({ bot_id: botId, session_id: sessionId, visibility }), [botId, sessionId, visibility])
  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    pagination.setFilters({ search: filterDraft.search.trim() || null, status: filterDraft.status || null })
  }
  const resetFilters = () => {
    setFilterDraft({ search: '', status: '' })
    pagination.setFilters({ search: null, status: null })
  }
  const openDetail = (fact: ScopedFactItem) => {
    setSelectedFact(fact)
    setDetailOpen(true)
  }

  const reviewOne = async (fact: ScopedFactItem, action: FactReviewAction, target?: FactConflictCandidate) => {
    if (loading || !canReview(fact, action) || pendingReviews.current.size || !mounted.current) return
    pendingReviews.current.add(fact.id)
    setBusyIds((current) => [...current, fact.id])
    try {
      const result = await reviewFact(fact, action, scope, undefined, target)
      if (!mounted.current) return
      if (!result.ok) throw new Error('审核未完成，请刷新后核对状态')
      if (result.item.status === 'conflict') toast.warning('事实仍有冲突，请查看详情选择共存或替换')
      else if (result.item.ineffective_reason) toast.success(`已处理事实：${INEFFECTIVE_LABEL[result.item.ineffective_reason] ?? result.item.ineffective_reason}`)
      else toast.success(`已${ACTION_LABEL[action]}事实`)
      setDetailOpen(false)
      setSelectedFact(null)
      setReload((value) => value + 1)
    } catch (reason) {
      if (!mounted.current) return
      toast.error(humanizeApiError(reason, `${ACTION_LABEL[action]}事实失败`))
      setDetailOpen(false)
      setSelectedFact(null)
      setReload((value) => value + 1)
    } finally {
      pendingReviews.current.delete(fact.id)
      if (mounted.current) setBusyIds((current) => current.filter((id) => id !== fact.id))
    }
  }

  const facts = data?.items ?? []
  const reviewableSelected = facts.filter((fact) => selectedIds.includes(fact.id) && (canBatchReview(fact, 'approve') || canBatchReview(fact, 'reject')))
  const batchAllowed = (action: FactBatchReviewAction) => !loading && !batchBusy && !busyIds.length && reviewableSelected.length > 0 && reviewableSelected.every((fact) => canBatchReview(fact, action))
  const reviewAvailable = facts.some((fact) => fact.capabilities?.review?.available)
  const toggleSelected = (fact: ScopedFactItem) => {
    setSelectedIds((current) => current.includes(fact.id) ? current.filter((id) => id !== fact.id) : [...current, fact.id])
  }

  const reviewSelected = async (action: FactBatchReviewAction) => {
    if (!batchAllowed(action) || pendingReviews.current.size) return
    reviewableSelected.forEach((fact) => pendingReviews.current.add(fact.id))
    setBatchBusy(true)
    try {
      const result = await batchReviewFacts(reviewableSelected, action, scope)
      if (!mounted.current) return
      const failed = result.failed ?? []
      if (failed.length) toast.warning(`${result.succeeded?.length ?? 0} 条已处理，${failed.length} 条失败`)
      else toast.success(`已${action === 'approve' ? '批准' : '拒绝'} ${result.succeeded?.length ?? 0} 条事实`)
      setSelectedIds([])
      setReload((value) => value + 1)
    } catch (reason) {
      if (!mounted.current) return
      toast.error(humanizeApiError(reason, '批量审核失败'))
      setSelectedIds([])
      setReload((value) => value + 1)
    } finally {
      pendingReviews.current.clear()
      if (mounted.current) setBatchBusy(false)
    }
  }

  const activeCount = data ? facts.filter((fact) => fact.effective).length : '—'
  const evidenceCount = data ? facts.filter((fact) => fact.evidence_status === 'available').length : '—'
  const pendingCount = data ? facts.filter((fact) => REVIEWABLE.has(fact.status)).length : '—'
  const total = data?.page.total_status === 'exact' ? data.page.total ?? '—' : '—'
  const status = !botId || !sessionId ? 'unknown' : loading ? 'loading' : error ? 'error' : !facts.length ? 'empty' : 'success'
  const allPageSelected = reviewableSelected.length > 0 && reviewableSelected.length === facts.filter((f) => canBatchReview(f, 'approve') || canBatchReview(f, 'reject')).length

  return <div className="flex flex-col gap-6" data-page="facts" data-slot="facts-page">
    <HeroHeader
      icon={<NetworkIcon className="size-6" />}
      title="事实关系与知识链"
      description="按当前 Bot 和群审核事实与实体关系；只有批准生效的事实才会进入 Bot 的上下文，证据只接受本群健康记忆。"
      badge={
        reviewAvailable ? (
          <Badge variant="outline" className="border-emerald-500/25 bg-emerald-500/5 text-emerald-600">
            <ShieldCheckIcon className="size-3.5 mr-1" />
            受控生命周期
          </Badge>
        ) : (
          <Badge variant="outline" className="border-amber-500/25 bg-amber-500/5 text-amber-600">
            <LockIcon className="size-3.5 mr-1" />
            只读模式
          </Badge>
        )
      }
      metrics={[
        { label: '匹配总数', value: loading ? '…' : total },
        { label: '本页已生效', value: loading ? '…' : activeCount },
        { label: '本页待审核', value: loading ? '…' : pendingCount, tone: 'text-amber-600' },
        { label: '本页证据引用', value: loading ? '…' : evidenceCount, tone: 'text-emerald-600' },
      ]}
    />

    <ScopeFilterBar
      botId={botId}
      sessionId={sessionId}
      loadBots={loadBots}
      loadSessions={loadSessions}
      onBotChange={(val) => pagination.setFilters({ bot_id: val, session_id: null })}
      onSessionChange={(val) => pagination.setFilters({ session_id: val })}
      searchValue={filterDraft.search}
      onSearchChange={(val) => setFilterDraft((current) => ({ ...current, search: val }))}
      searchPlaceholder="搜索主体、关系或客体…"
      onSubmit={submitSearch}
      onReset={resetFilters}
    >
      <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
        <FieldLabel htmlFor="fact-status-filter">事实状态</FieldLabel>
        <select
          id="fact-status-filter"
          aria-label="事实状态"
          className="h-8 w-full rounded-md border bg-background px-2 text-xs"
          value={filterDraft.status}
          onChange={(event) => setFilterDraft((current) => ({ ...current, status: event.target.value }))}
          disabled={!botId || !sessionId}
        >
          <option value="">全部状态</option>
          <option value="pending">待审核</option>
          <option value="conflict">有冲突待裁决</option>
          <option value="active">已生效</option>
          <option value="rejected">已拒绝</option>
          <option value="superseded">已被取代</option>
        </select>
      </Field>
    </ScopeFilterBar>

    <Card>
      <CardHeader>
        <CardTitle className="text-base">事实清单</CardTitle>
        <CardDescription>浏览当前群聊沉淀的客观事实与实体关系，支持查看聊天气泡证据与生命周期审核。有效期提示按本机时间每分钟更新，审核状态以服务端为准。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <BatchActionBar
          selectedCount={reviewableSelected.length}
          disabled={!reviewAvailable || !reviewableSelected.length}
          onClear={() => setSelectedIds([])}
        >
          <Button
            type="button"
            size="sm"
            disabled={!batchAllowed('approve')}
            onClick={() => reviewSelected('approve')}
          >
            <CheckIcon data-icon="inline-start" />
            批量批准
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!batchAllowed('reject')}
            onClick={() => reviewSelected('reject')}
          >
            <XIcon data-icon="inline-start" />
            批量拒绝
          </Button>
        </BatchActionBar>

        {!reviewAvailable && facts.length ? (
          <p className="text-xs text-amber-600">审核能力当前不可用：服务端未提供事实变更网关，本页只能查看。</p>
        ) : null}

        <QueryState
          status={status}
          error={error}
          title="事实读取失败"
          description={!botId || !sessionId ? '请先选择 Bot 和群，不会跨 Bot 汇总。' : undefined}
          emptyTitle="当前群没有待审或已批准的事实"
          emptyDescription="Bot 在现场对话中提审事实后会进入这里等待审核；批准后才会作为事实进入它的上下文。人物相关的称呼、别名与事实已沉淀在「人物与关系」详情的印象时间线里，不在此页重复展示。"
          onRetry={() => setReload((value) => value + 1)}
        >
          <ResponsiveTable
            label="事实关系清单"
            table={<Table className="w-full table-fixed">
              <TableHeader><TableRow className="h-8 bg-muted/15">
                <TableHead className="w-9 py-1">
                  <input
                    type="checkbox"
                    aria-label="选择当前页全部事实"
                    className="size-3.5"
                    disabled={!reviewAvailable || !facts.some((f) => REVIEWABLE.has(f.status) && f.editable)}
                    checked={allPageSelected}
                    onChange={(e) => {
                      const reviewableFactIds = facts.filter((f) => canBatchReview(f, 'approve') || canBatchReview(f, 'reject')).map((f) => f.id)
                      setSelectedIds(e.target.checked ? reviewableFactIds : [])
                    }}
                  />
                </TableHead>
                <TableHead className="w-14 py-1 text-xs">编号</TableHead>
                <TableHead className="w-1/5 py-1 text-xs">主体</TableHead>
                <TableHead className="w-20 py-1 text-xs">关系</TableHead>
                <TableHead className="w-auto py-1 text-xs">客体</TableHead>
                <TableHead className="w-14 py-1 text-xs">置信度</TableHead>
                <TableHead className="w-20 py-1 text-xs">状态</TableHead>
                <TableHead className="w-24 py-1 text-xs">审核建议</TableHead>
                <TableHead className="w-48 py-1 text-xs text-right">操作</TableHead>
              </TableRow></TableHeader>
              <TableBody>{facts.map((fact) => {
                const reviewable = canBatchReview(fact, 'approve') || canBatchReview(fact, 'reject')
                const busy = batchBusy || busyIds.length > 0
                return <TableRow key={`${fact.bot_id}:${fact.session_id}:${fact.id}`} className="h-9 hover:bg-muted/10">
                  <TableCell className="py-1 align-top">
                    <input type="checkbox" aria-label={`选择事实 ${fact.id}`} className="size-3.5" disabled={!reviewable || !reviewAvailable} checked={selectedIds.includes(fact.id)} onChange={() => toggleSelected(fact)} />
                  </TableCell>
                  <TableCell className="py-1 font-mono text-xs align-top">{fact.id}</TableCell>
                  <TableCell className="py-1 text-xs font-medium align-top whitespace-normal break-words">{fact.subject}</TableCell>
                  <TableCell className="py-1 align-top"><Badge variant="outline" className="px-1.5 text-xs font-normal">{fact.predicate}</Badge></TableCell>
                  <TableCell className="py-1 text-xs align-top whitespace-normal break-words">{fact.object}</TableCell>
                  <TableCell className="py-1 font-mono text-xs align-top">{confidence(fact.confidence)}</TableCell>
                  <TableCell className="py-1 align-top"><div className="flex flex-wrap gap-1"><Badge variant="secondary" className="text-xs">{fact.status || '未知'}</Badge><FactValidityBadge fact={fact} now={now} /></div></TableCell>
                  <TableCell className="py-1 align-top text-xs"><span className={HINT_TONE[fact.review_hint]}>{HINT_LABEL[fact.review_hint] || '—'}</span></TableCell>
                  <TableCell className="py-1 align-top text-right">
                    <div className="flex justify-end items-center gap-1">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-6 px-1.5 text-xs"
                        disabled={!fact.source_memory_id && !fact.evidence?.length}
                        title={fact.source_memory_id ? '还原同作用域聊天证据' : '未关联聊天证据'}
                        onClick={() => setEvidenceFact(fact)}
                      >
                        <MessageSquareQuoteIcon className="size-3" data-icon="inline-start" />
                        证据
                      </Button>
                      {canReview(fact, 'approve') ? <Button type="button" size="icon-sm" className="size-6" aria-label={`批准事实 ${fact.id}`} disabled={busy} onClick={() => reviewOne(fact, 'approve')}><CheckIcon className="size-3" /></Button> : null}
                      {canReview(fact, 'reject') ? <Button type="button" variant="outline" size="icon-sm" className="size-6" aria-label={`拒绝事实 ${fact.id}`} disabled={busy} onClick={() => reviewOne(fact, 'reject')}><XIcon className="size-3" /></Button> : null}
                      <Button type="button" variant="ghost" size="sm" className="h-6 px-1.5 text-xs" aria-label={`查看事实 ${fact.id} 详情`} onClick={() => openDetail(fact)}>详情</Button>
                    </div>
                  </TableCell>
                </TableRow>
              })}</TableBody>
            </Table>}
            cards={facts.map((fact) => {
              const busy = batchBusy || busyIds.length > 0
              return <article key={`${fact.bot_id}:${fact.session_id}:${fact.id}`} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-xs text-muted-foreground">事实 #{fact.id}</span>
                  <div className="flex flex-wrap items-center gap-1">
                    <Badge variant="secondary">{fact.status || '未知'}</Badge>
                    <FactValidityBadge fact={fact} now={now} />
                    <Badge variant={fact.evidence_status === 'available' ? 'default' : 'outline'}>{fact.evidence_status === 'available' ? '可溯源' : '不可用'}</Badge>
                  </div>
                </div>
                <dl className="grid gap-2 text-sm"><div><dt className="text-muted-foreground">主体</dt><dd className="break-words font-medium">{fact.subject}</dd></div><div><dt className="text-muted-foreground">关系</dt><dd><Badge variant="outline">{fact.predicate}</Badge></dd></div><div><dt className="text-muted-foreground">客体</dt><dd className="break-words">{fact.object}</dd></div></dl>
                {fact.review_hint ? <p className={`text-xs ${HINT_TONE[fact.review_hint]}`}>{HINT_LABEL[fact.review_hint]}</p> : null}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="font-mono text-xs text-muted-foreground">置信度 {confidence(fact.confidence)}</span>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!fact.source_memory_id && !fact.evidence?.length}
                      onClick={() => setEvidenceFact(fact)}
                    >
                      <MessageSquareQuoteIcon data-icon="inline-start" />
                      证据
                    </Button>
                    {canReview(fact, 'approve') ? <Button type="button" size="sm" disabled={busy} onClick={() => reviewOne(fact, 'approve')}>批准</Button> : null}
                    {canReview(fact, 'reject') ? <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => reviewOne(fact, 'reject')}>拒绝</Button> : null}
                    <Button type="button" variant="outline" size="sm" onClick={() => openDetail(fact)}>查看详情</Button>
                  </div>
                </div>
              </article>
            })}
          />
        </QueryState>
        {data?.page ? <PaginationControls page={data.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={loading} label="事实分页" /> : null}
      </CardContent>
    </Card>

    <Sheet open={detailOpen} onOpenChange={setDetailOpen}>
      <SheetContent className="w-[min(94vw,34rem)] sm:max-w-xl">
        <SheetHeader className="border-b pr-12"><SheetTitle>事实关系详情</SheetTitle><SheetDescription>查看主体、关系、客体、本群证据，并完成审核。</SheetDescription></SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{selectedFact ? <FactDetail key={`${selectedFact.id}:${selectedFact.revision}`} fact={selectedFact} now={now} busy={loading || batchBusy || busyIds.length > 0} onReview={reviewOne} onOpenEvidence={(f) => setEvidenceFact(f)} /> : null}</div>
      </SheetContent>
    </Sheet>

    <FactEvidenceDialog
      fact={evidenceFact}
      scope={scope}
      onClose={() => setEvidenceFact(null)}
    />
  </div>
}

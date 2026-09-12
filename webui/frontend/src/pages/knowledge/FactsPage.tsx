import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { CheckIcon, EyeIcon, RefreshCwIcon, SearchIcon, XIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  approveFact,
  batchReviewFacts,
  listFacts,
  rejectFact,
  type FactReviewAction,
  type FactReviewHint,
  type ScopedFactItem,
  type ScopedFactsResponse,
} from '@/api/facts'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import { EvidenceList, PaginationControls, QueryState, ResponsiveTable, ScopeSelect, usePaginationSearchParams } from '@/components/shared'
import { useCanonicalScopeDefault } from '@/hooks/use-pagination-search-params'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { humanizeApiError } from '@/lib/reason-label'

const REVIEWABLE = new Set(['pending', 'quarantined', 'conflict'])

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

function SummaryTile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return <div className="min-w-[4.75rem] rounded-lg border bg-muted/20 px-3 py-1.5 text-center text-xs"><div className="text-[10px] text-muted-foreground">{label}</div><div className={`font-semibold ${tone ?? ''}`}>{value}</div></div>
}

function FactDetail({ fact, onReview, busy }: { fact: ScopedFactItem; onReview: (fact: ScopedFactItem, action: FactReviewAction) => void; busy: boolean }) {
  const reviewable = REVIEWABLE.has(fact.status) && fact.editable
  return <div className="flex flex-col gap-5 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="outline">事实 #{fact.id}</Badge>
      <Badge variant="secondary">{fact.status || '未知状态'}</Badge>
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
      <div className="col-span-2"><dt className="text-xs text-muted-foreground">当前群</dt><dd className="break-all font-mono text-xs">{fact.bot_id} · {fact.session_id}</dd></div>
    </dl>

    <div>
      <h3 className="mb-2 font-medium">本群证据</h3>
      <EvidenceList evidence={fact.evidence} objectPath="/memories" emptyDescription="没有可验证的本群记忆证据；这不代表事实为假，但无法从此页面完成溯源。" />
    </div>

    {reviewable ? <div className="flex flex-wrap gap-2 rounded-lg border p-3.5">
      <Button type="button" size="sm" disabled={busy} onClick={() => onReview(fact, 'approve')}><CheckIcon data-icon="inline-start" />批准生效</Button>
      <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onReview(fact, 'reject')}><XIcon data-icon="inline-start" />拒绝</Button>
      <p className="w-full text-xs text-muted-foreground">批准后该事实才会进入 Bot 的上下文；批准前系统会检查它与既有事实是否冲突。</p>
    </div> : null}

    <details className="rounded-lg border bg-muted/10 p-3 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium text-foreground">技术与安全边界</summary>
      <p className="mt-2 leading-relaxed">事实编号只用于本页查看。证据入口只打开服务端给出的本群记忆，不会跨 Bot、跨群或把已隔离内容补进来。</p>
    </details>
  </div>
}

export function FactsPage() {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const search = pagination.searchParams.get('search') ?? ''
  const statusFilter = pagination.searchParams.get('status') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })

  const [filterDraft, setFilterDraft] = useState({ search, status: statusFilter })
  const [data, setData] = useState<ScopedFactsResponse | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const [selectedFact, setSelectedFact] = useState<ScopedFactItem | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [busyIds, setBusyIds] = useState<number[]>([])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])

  useEffect(() => { setFilterDraft({ search, status: statusFilter }) }, [search, statusFilter])
  useEffect(() => {
    if (!botId || !sessionId) { setData(null); setLoading(false); setError(undefined); return }
    let active = true
    setLoading(true)
    setError(undefined)
    listFacts({
      bot_id: botId,
      session_id: sessionId,
      visibility: 'group',
      search: search || undefined,
      status: statusFilter || undefined,
      limit: pagination.limit,
      offset: pagination.offset,
    })
      .then((value) => { if (active) { setData(value); setSelectedIds([]) } })
      .catch((reason: unknown) => { if (active) { setData(null); setError(reason) } })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [botId, pagination.limit, pagination.offset, reload, search, sessionId, statusFilter])

  const scope = useMemo(() => ({ bot_id: botId, session_id: sessionId, visibility: 'group' as const }), [botId, sessionId])
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

  const reviewOne = async (fact: ScopedFactItem, action: FactReviewAction) => {
    setBusyIds((current) => [...current, fact.id])
    try {
      await (action === 'approve' ? approveFact(fact, scope) : rejectFact(fact, scope))
      toast.success(action === 'approve' ? '已批准事实' : '已拒绝事实')
      setDetailOpen(false)
      setReload((value) => value + 1)
    } catch (reason) {
      toast.error(humanizeApiError(reason, action === 'approve' ? '批准事实失败' : '拒绝事实失败'))
    } finally {
      setBusyIds((current) => current.filter((id) => id !== fact.id))
    }
  }

  const facts = data?.items ?? []
  const reviewableSelected = facts.filter((fact) => selectedIds.includes(fact.id) && REVIEWABLE.has(fact.status) && fact.editable)
  const reviewAvailable = facts.some((fact) => fact.capabilities?.review?.available)
  const toggleSelected = (fact: ScopedFactItem) => {
    setSelectedIds((current) => current.includes(fact.id) ? current.filter((id) => id !== fact.id) : [...current, fact.id])
  }

  const reviewSelected = async (action: FactReviewAction) => {
    if (!reviewableSelected.length) return
    try {
      const result = await batchReviewFacts(reviewableSelected, action, scope)
      const failed = result.failed ?? []
      if (failed.length) toast.warning(`${result.succeeded?.length ?? 0} 条已处理，${failed.length} 条失败`)
      else toast.success(`已${action === 'approve' ? '批准' : '拒绝'} ${result.succeeded?.length ?? 0} 条事实`)
      setSelectedIds([])
      setReload((value) => value + 1)
    } catch (reason) {
      toast.error(humanizeApiError(reason, '批量审核失败'))
    }
  }

  const evidenceCount = data ? facts.filter((fact) => fact.evidence_status === 'available').length : '—'
  const pendingCount = data ? facts.filter((fact) => REVIEWABLE.has(fact.status)).length : '—'
  const total = data?.page.total_status === 'exact' ? data.page.total ?? '—' : '—'
  const status = !botId || !sessionId ? 'unknown' : loading ? 'loading' : error ? 'error' : !facts.length ? 'empty' : 'success'

  return <div className="flex flex-col gap-4" data-page="facts">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <header className="max-w-2xl">
        <h1 className="text-xl font-bold tracking-tight">事实关系</h1>
        <p className="text-xs text-muted-foreground">按当前 Bot 和群审核人物/事物关系；只有批准生效的事实才会进入 Bot 的上下文，证据只接受本群健康、未隔离的记忆。</p>
      </header>
      <div className="flex flex-wrap gap-2">
        <SummaryTile label="筛选总数" value={loading ? '…' : total} />
        <SummaryTile label="本页待审" value={loading ? '…' : pendingCount} tone="text-amber-600" />
        <SummaryTile label="本页有证据" value={loading ? '…' : evidenceCount} tone="text-emerald-600" />
      </div>
    </div>

    <Card className="overflow-hidden border-border/60">
      <CardContent className="p-0">
        <div className="flex flex-col gap-3 bg-muted/[0.035] p-3">
          <div className="flex flex-wrap items-end gap-2" data-slot="facts-scope-context">
            <Badge variant="outline" className="mb-0.5 h-7">当前群</Badge>
            <ScopeSelect className="min-w-48 flex-1 xl:max-w-64" value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择 Bot" required onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null })} />
            <ScopeSelect className="min-w-56 flex-[1.4] xl:max-w-80" value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择该 Bot 的群" disabled={!botId} required onValueChange={(value) => pagination.setFilters({ session_id: value })} />
            <span className="pb-1 text-[10px] text-muted-foreground">这里的 Bot 不是 QQ 号</span>
          </div>

          <form className="flex flex-wrap items-center gap-2" onSubmit={submitSearch}>
            <div className="relative min-w-60 flex-1 xl:max-w-xl">
              <SearchIcon className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input aria-label="搜索事实" className="h-8 pl-8 text-xs" value={filterDraft.search} onChange={(event) => setFilterDraft((current) => ({ ...current, search: event.target.value }))} placeholder="搜索主体、关系或客体" disabled={!botId || !sessionId} />
            </div>
            <select aria-label="事实状态" className="h-8 rounded-lg border bg-background px-2 text-xs" value={filterDraft.status} onChange={(event) => setFilterDraft((current) => ({ ...current, status: event.target.value }))} disabled={!botId || !sessionId}>
              <option value="">全部状态</option>
              <option value="pending">待审核</option>
              <option value="conflict">有冲突待裁决</option>
              <option value="active">已生效</option>
              <option value="rejected">已拒绝</option>
              <option value="superseded">已被取代</option>
            </select>
            <Button type="submit" size="sm" className="h-8" disabled={loading || !botId || !sessionId}>搜索</Button>
            <Button type="button" size="sm" className="h-8" variant="ghost" onClick={resetFilters}>清除</Button>
            <Button type="button" size="icon-sm" className="h-8 w-8" variant="outline" disabled={loading || !botId || !sessionId} onClick={() => setReload((value) => value + 1)} aria-label="刷新事实">
              <RefreshCwIcon className={loading ? 'animate-spin' : undefined} aria-hidden="true" />
            </Button>
            <span className="ml-auto text-xs text-muted-foreground">{data?.page ? `当前第 ${Math.floor(pagination.offset / pagination.limit) + 1} 页` : '请先选择 Bot 和群'}</span>
          </form>

          {reviewableSelected.length ? <div data-slot="batch-action-bar" className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3">
            <Badge variant="secondary" className="px-2 py-1 text-xs">已选 {reviewableSelected.length} 条待审</Badge>
            <Button type="button" size="sm" disabled={!reviewAvailable} onClick={() => reviewSelected('approve')}><CheckIcon data-icon="inline-start" />批量批准</Button>
            <Button type="button" size="sm" variant="outline" disabled={!reviewAvailable} onClick={() => reviewSelected('reject')}><XIcon data-icon="inline-start" />批量拒绝</Button>
            <Button type="button" size="sm" variant="ghost" className="ml-auto text-xs" onClick={() => setSelectedIds([])}>取消选择</Button>
          </div> : null}
          {!reviewAvailable && facts.length ? <p className="text-xs text-amber-600">审核能力当前不可用：服务端未提供事实变更网关，本页只能查看。</p> : null}
        </div>

        <Separator />

        <div className="flex flex-col gap-3 p-3">
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
                  <TableHead className="w-9 py-1"><span className="sr-only">选择</span></TableHead>
                  <TableHead className="w-14 py-1 text-[11px]">编号</TableHead>
                  <TableHead className="w-1/5 py-1 text-[11px]">主体</TableHead>
                  <TableHead className="w-20 py-1 text-[11px]">关系</TableHead>
                  <TableHead className="w-auto py-1 text-[11px]">客体</TableHead>
                  <TableHead className="w-14 py-1 text-[11px]">置信度</TableHead>
                  <TableHead className="w-20 py-1 text-[11px]">状态</TableHead>
                  <TableHead className="w-24 py-1 text-[11px]">审核建议</TableHead>
                  <TableHead className="w-28 py-1 text-[11px]">操作</TableHead>
                </TableRow></TableHeader>
                <TableBody>{facts.map((fact) => {
                  const reviewable = REVIEWABLE.has(fact.status) && fact.editable
                  const busy = busyIds.includes(fact.id)
                  return <TableRow key={`${fact.bot_id}:${fact.session_id}:${fact.id}`} className="h-9 hover:bg-muted/10">
                    <TableCell className="py-1 align-top">
                      <input type="checkbox" aria-label={`选择事实 ${fact.id}`} className="size-3.5" disabled={!reviewable || !reviewAvailable} checked={selectedIds.includes(fact.id)} onChange={() => toggleSelected(fact)} />
                    </TableCell>
                    <TableCell className="py-1 font-mono text-[11px] align-top">{fact.id}</TableCell>
                    <TableCell className="py-1 text-xs font-medium align-top whitespace-normal break-words">{fact.subject}</TableCell>
                    <TableCell className="py-1 align-top"><Badge variant="outline" className="px-1.5 text-[10px] font-normal">{fact.predicate}</Badge></TableCell>
                    <TableCell className="py-1 text-xs align-top whitespace-normal break-words">{fact.object}</TableCell>
                    <TableCell className="py-1 font-mono text-[11px] align-top">{confidence(fact.confidence)}</TableCell>
                    <TableCell className="py-1 align-top"><Badge variant="secondary" className="text-[10px]">{fact.status || '未知'}</Badge></TableCell>
                    <TableCell className="py-1 align-top text-xs"><span className={HINT_TONE[fact.review_hint]}>{HINT_LABEL[fact.review_hint] || '—'}</span></TableCell>
                    <TableCell className="py-1 align-top">
                      <div className="flex items-center gap-1">
                        {reviewable ? <>
                          <Button type="button" variant="ghost" size="icon-xs" aria-label={`批准事实 ${fact.id}`} disabled={busy} onClick={() => reviewOne(fact, 'approve')}><CheckIcon aria-hidden="true" /></Button>
                          <Button type="button" variant="ghost" size="icon-xs" aria-label={`拒绝事实 ${fact.id}`} disabled={busy} onClick={() => reviewOne(fact, 'reject')}><XIcon aria-hidden="true" /></Button>
                        </> : null}
                        <Button type="button" variant="ghost" size="icon-xs" aria-label={`查看事实 ${fact.id} 详情`} onClick={() => openDetail(fact)}><EyeIcon aria-hidden="true" /></Button>
                      </div>
                    </TableCell>
                  </TableRow>
                })}</TableBody>
              </Table>}
              cards={facts.map((fact) => {
                const reviewable = REVIEWABLE.has(fact.status) && fact.editable
                const busy = busyIds.includes(fact.id)
                return <article key={`${fact.bot_id}:${fact.session_id}:${fact.id}`} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-mono text-xs text-muted-foreground">事实 #{fact.id}</span>
                    <div className="flex flex-wrap items-center gap-1">
                      <Badge variant="secondary">{fact.status || '未知'}</Badge>
                      <Badge variant={fact.evidence_status === 'available' ? 'default' : 'outline'}>{fact.evidence_status === 'available' ? '可溯源' : '不可用'}</Badge>
                    </div>
                  </div>
                  <dl className="grid gap-2 text-sm"><div><dt className="text-muted-foreground">主体</dt><dd className="break-words font-medium">{fact.subject}</dd></div><div><dt className="text-muted-foreground">关系</dt><dd><Badge variant="outline">{fact.predicate}</Badge></dd></div><div><dt className="text-muted-foreground">客体</dt><dd className="break-words">{fact.object}</dd></div></dl>
                  {fact.review_hint ? <p className={`text-xs ${HINT_TONE[fact.review_hint]}`}>{HINT_LABEL[fact.review_hint]}</p> : null}
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <span className="font-mono text-xs text-muted-foreground">置信度 {confidence(fact.confidence)}</span>
                    <div className="flex gap-2">
                      {reviewable ? <>
                        <Button type="button" size="sm" disabled={busy} onClick={() => reviewOne(fact, 'approve')}>批准</Button>
                        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => reviewOne(fact, 'reject')}>拒绝</Button>
                      </> : null}
                      <Button type="button" variant="outline" size="sm" onClick={() => openDetail(fact)}>查看详情</Button>
                    </div>
                  </div>
                </article>
              })}
            />
          </QueryState>
          {data?.page ? <PaginationControls page={data.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={loading} label="事实分页" /> : null}
        </div>
      </CardContent>
    </Card>

    <Sheet open={detailOpen} onOpenChange={setDetailOpen}>
      <SheetContent className="w-[min(94vw,34rem)] sm:max-w-xl">
        <SheetHeader className="border-b pr-12"><SheetTitle>事实关系详情</SheetTitle><SheetDescription>查看主体、关系、客体、本群证据，并完成审核。</SheetDescription></SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{selectedFact ? <FactDetail fact={selectedFact} busy={busyIds.includes(selectedFact.id)} onReview={reviewOne} /> : null}</div>
      </SheetContent>
    </Sheet>
  </div>
}

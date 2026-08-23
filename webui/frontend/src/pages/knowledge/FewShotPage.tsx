import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { EyeIcon, RefreshCwIcon } from 'lucide-react'

import { getApprovedFewShot, type ApprovedFewShot } from '@/api/knowledge'
import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import { PaginationControls, QueryState, ResponsiveDetail, DeclarativeDataTable, ScopeSelect, usePaginationSearchParams, type PageResponse } from '@/components/shared'
import { useCanonicalScopeDefault } from '@/hooks/use-pagination-search-params'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { InputWithIcon } from '@/components/shared/InputWithIcon'

function score(value: number | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '未评分'
}

function formatTime(value: number | null | undefined): string {
  if (value === undefined || value === null) return '未记录'
  const millis = value < 10_000_000_000 ? value * 1000 : value
  const date = new Date(millis)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN')
}

function FewShotDetail({ item }: { item: ApprovedFewShot }) {
  return <div className="flex flex-col gap-4">
    <div className="flex flex-wrap gap-2"><Badge>approved</Badge><Badge variant="secondary">healthy</Badge><Badge variant="outline">Bot {item.bot_id}</Badge></div>
    <div><h3 className="mb-1 text-sm font-medium text-muted-foreground">正式范例内容</h3><p className="whitespace-pre-wrap rounded-md border p-3 leading-relaxed">{item.content}</p></div>
    <dl className="grid gap-3 sm:grid-cols-2">
      <div><dt className="text-sm font-medium text-muted-foreground">稳定 ID</dt><dd className="font-mono">{item.id}</dd></div>
      <div><dt className="text-sm font-medium text-muted-foreground">评分</dt><dd>{score(item.score)}</dd></div>
      <div><dt className="text-sm font-medium text-muted-foreground">创建时间</dt><dd>{formatTime(item.created_at)}</dd></div>
      <div><dt className="text-sm font-medium text-muted-foreground">批准时间</dt><dd>{formatTime(item.approved_at)}</dd></div>
    </dl>
    <div><h3 className="mb-2 text-sm font-medium text-muted-foreground">风格特征</h3><div className="flex flex-wrap gap-1">{item.traits?.length ? item.traits.map((trait) => <Badge key={trait} variant="outline">{trait}</Badge>) : <span className="text-muted-foreground">未记录 traits</span>}</div></div>
  </div>
}

function SummaryTile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return <div className="min-w-[5rem] rounded-lg border bg-muted/20 px-2.5 py-1.5 text-center"><div className="text-[11px] text-muted-foreground">{label}</div><div className={`text-base font-semibold leading-5 ${tone ?? ''}`}>{value}</div></div>
}

export function FewShotPage() {
  const pagination = usePaginationSearchParams()
  const botId = pagination.searchParams.get('bot_id') ?? ''
  const sessionId = pagination.searchParams.get('session_id') ?? ''
  const search = pagination.searchParams.get('search') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const [searchDraft, setSearchDraft] = useState(search)
  const [data, setData] = useState<PageResponse<ApprovedFewShot> | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['session']).filter((option) => option.description?.startsWith(`${botId} ·`)), [botId])

  useEffect(() => { setSearchDraft(search) }, [search])
  useEffect(() => {
    if (!botId || !sessionId) { setData(null); setLoading(false); setError(undefined); return }
    let active = true
    setLoading(true)
    setError(undefined)
    getApprovedFewShot({ bot_id: botId, session_id: sessionId, visibility: 'group', search: search || undefined, limit: pagination.limit, offset: pagination.offset })
      .then((value) => { if (active) setData(value) })
      .catch((reason: unknown) => { if (active) { setData(null); setError(reason) } })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [botId, pagination.limit, pagination.offset, reload, search, sessionId])

  const submitSearch = (event: FormEvent) => { event.preventDefault(); pagination.setFilters({ search: searchDraft.trim() || null }) }
  const clearSearch = () => { setSearchDraft(''); pagination.setFilters({ search: null }) }
  const pageItems = data?.items ?? []
  const scoredItems = pageItems.filter((item) => typeof item.score === 'number' && Number.isFinite(item.score))
  const averageScore = scoredItems.length ? (scoredItems.reduce((sum, item) => sum + (item.score ?? 0), 0) / scoredItems.length).toFixed(2) : '—'
  const traitCount = data ? new Set(pageItems.flatMap((item) => item.traits ?? [])).size : '—'
  const total = data?.page.total_status === 'exact' ? data.page.total ?? '—' : '—'
  const status = !botId || !sessionId ? 'unknown' : loading ? 'loading' : error ? 'error' : !pageItems.length ? 'empty' : 'success'

  return <div className="flex flex-col gap-4" data-page="few-shot">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <header className="max-w-2xl"><h1 className="text-xl font-bold tracking-tight">FewShot 正式风格范例</h1><p className="text-xs text-muted-foreground">仅展示所选 canonical RuntimeScope 下 approved 且通过健康检查的正式范例。</p></header>
      <div className="flex flex-wrap gap-2 text-xs"><SummaryTile label="筛选总数" value={total} /><SummaryTile label="本页平均分" value={averageScore} tone="text-emerald-600" /><SummaryTile label="本页 traits" value={traitCount} /></div>
    </div>

    <Card className="overflow-hidden border-border/60"><CardContent className="p-0">
      <div className="grid items-center gap-2 border-b bg-muted/10 px-4 py-3 lg:grid-cols-[auto_minmax(10rem,0.75fr)_minmax(12rem,1fr)_minmax(20rem,1.4fr)]">
        <div className="pr-1 text-xs"><div className="font-medium">RuntimeScope</div><div className="text-muted-foreground">Bot + canonical 群会话</div></div>
        <ScopeSelect className="[&_[data-slot=field-label]]:sr-only" value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择真实 Bot" required onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null })} />
        <ScopeSelect className="[&_[data-slot=field-label]]:sr-only" value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择 canonical 群会话" disabled={!botId} required onValueChange={(value) => pagination.setFilters({ session_id: value })} />
        <form className="flex min-w-0 flex-wrap items-center gap-2" onSubmit={submitSearch}>
          <InputWithIcon value={searchDraft} onValueChange={(v) => setSearchDraft(v)} placeholder="搜索范例内容、traits" containerClassName="min-w-48 flex-1" />
          <Button type="submit" size="sm" className="h-8" disabled={loading || !botId || !sessionId}>搜索</Button>
          <Button type="button" size="sm" className="h-8" variant="ghost" onClick={clearSearch}>清除</Button>
          <Button type="button" size="sm" className="h-8" variant="outline" disabled={loading || !botId || !sessionId} onClick={() => setReload((value) => value + 1)}><RefreshCwIcon aria-hidden="true" /><span className="sr-only">刷新</span></Button>
        </form>
      </div>

        <QueryState status={status} error={error} title="FewShot 读取失败" description={!botId || !sessionId ? '请选择真实 Bot 与 canonical 群会话；页面禁止跨群或跨 Bot 汇总。' : '当前 RuntimeScope 与搜索条件下没有 approved / healthy 正式范例。'} onRetry={() => setReload((value) => value + 1)}>
          <DeclarativeDataTable
            label="FewShot 正式范例清单"
            items={pageItems}
            keyExtractor={(row) => String(row.id)}
            columns={[
              { key: 'content', header: '范例内容', isTitle: true, render: (row) => <p className="max-w-2xl truncate leading-relaxed text-sm">{row.content}</p> },
              { key: 'score', header: '评分', render: (row) => <span className="font-mono text-xs">{score(row.score)}</span> },
              { key: 'traits', header: 'Traits', render: (row) => <div className="flex max-w-64 gap-1 overflow-hidden">{row.traits?.length ? row.traits.slice(0, 3).map((trait) => <Badge key={trait} variant="outline" className="max-w-24 truncate">{trait}</Badge>) : <span className="text-xs text-muted-foreground">未记录</span>}</div> },
              { key: 'status', header: '正式状态', render: () => <Badge>approved / healthy</Badge> },
              { key: 'actions', header: null, render: (row) => <ResponsiveDetail title="FewShot 正式范例" description={`Bot ${row.bot_id} 的只读正式范例`} trigger={<Button type="button" variant="ghost" size="icon-sm" aria-label="查看 FewShot 正式范例详情"><EyeIcon aria-hidden="true" /></Button>}><FewShotDetail item={row} /></ResponsiveDetail> },
            ]}
            renderCardActions={(row) => <ResponsiveDetail title="FewShot 正式范例" description={`Bot ${row.bot_id} 的只读正式范例`} trigger={<Button type="button" variant="outline" size="sm">查看详情</Button>}><FewShotDetail item={row} /></ResponsiveDetail>}
          />
        </QueryState>
      {data?.page ? <div className="border-t px-4 py-3"><PaginationControls page={data.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={loading} label="FewShot 分页" /></div> : null}
    </CardContent></Card>

    <details className="rounded-lg border border-dashed text-sm">
      <summary className="cursor-pointer list-none px-4 py-2.5 font-medium marker:hidden">正式数据边界与健康规则</summary>
      <p className="border-t px-4 py-3 text-xs leading-relaxed text-muted-foreground">本页不展示候选、拒绝项或审核写操作。approved 之外还要求内容通过身份污染与不安全风格检查；总数、分页和搜索始终限定在完整 canonical RuntimeScope。</p>
    </details>
  </div>
}

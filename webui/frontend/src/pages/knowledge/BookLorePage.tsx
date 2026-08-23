import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { EyeIcon, RefreshCwIcon } from 'lucide-react'

import {
  getBookLoreItems,
  getBookLoreSummary,
  type BookLoreItem,
  type BookLorePage,
  type BookLoreResource,
  type BookLoreSummary,
} from '@/api/knowledge'
import { DeclarativeDataTable, InputWithIcon, PaginationControls, QueryState, ResponsiveDetail } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { usePaginationSearchParams } from '@/hooks/use-pagination-search-params'

const RESOURCES: Array<{ value: BookLoreResource; label: string; help: string }> = [
  { value: 'entities', label: '实体', help: '人物、地点与设定对象' },
  { value: 'communities', label: '社区', help: '聚合后的世界观主题' },
  { value: 'relations', label: '关系', help: '实体之间的语义关系' },
  { value: 'notes', label: '笔记', help: '导入来源中的原始知识片段' },
]

function displayText(value: unknown, fallback = '未记录'): string {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'bigint') return String(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.length ? value.map((item) => displayText(item, '')).filter(Boolean).join('、') : fallback
  if (typeof value === 'object') return `结构化内容（${Object.keys(value).length} 项）`
  return String(value)
}

function itemTitle(item: BookLoreItem): string {
  return displayText(item.title ?? item.name ?? item.source_title ?? item.id)
}

function itemSummary(item: BookLoreItem): string {
  return displayText(item.summary ?? item.description ?? item.content ?? item.original, '无摘要')
}

function governance(item: BookLoreItem) {
  const quarantined = item.quarantine === true || item.quarantine === 1
  return (
    <div className="flex flex-wrap gap-1">
      <Badge variant="outline">{displayText(item.resolution, '解析状态未知')}</Badge>
      <Badge variant={quarantined ? 'destructive' : 'secondary'}>
        {item.quarantine === null || item.quarantine === undefined ? '隔离状态未知' : quarantined ? '已隔离' : '可用'}
      </Badge>
    </div>
  )
}

function ReadableValue({ value }: { value: unknown }): ReactNode {
  if (value === undefined || value === null || value === '') return <span className="text-muted-foreground">未记录</span>
  if (Array.isArray(value)) {
    return value.length ? <div className="flex flex-wrap gap-1">{value.map((item, index) => <Badge key={index} variant="outline">{displayText(item)}</Badge>)}</div> : <span className="text-muted-foreground">未记录</span>
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([key]) => !/(path|file|directory)/i.test(key)).slice(0, 20)
    return entries.length ? (
      <dl className="grid gap-2 rounded-md border p-3 sm:grid-cols-2">
        {entries.map(([key, nested]) => <div key={key}><dt className="text-xs text-muted-foreground">{key}</dt><dd className="break-words">{displayText(nested)}</dd></div>)}
      </dl>
    ) : <span className="text-muted-foreground">无可展示字段</span>
  }
  return <span className="whitespace-pre-wrap break-words">{displayText(value)}</span>
}

function ItemDetail({ item, resource }: { item: BookLoreItem; resource: BookLoreResource }) {
  const relationFields = resource === 'relations'
    ? [['起始实体', item.source], ['关系', item.relation], ['目标实体', item.target]] as const
    : []
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">{governance(item)}<Badge variant="outline">{resource}</Badge></div>
      <dl className="grid gap-3 sm:grid-cols-2">
        <div><dt className="text-sm font-medium text-muted-foreground">稳定 ID</dt><dd className="break-all font-mono">{String(item.id)}</dd></div>
        <div><dt className="text-sm font-medium text-muted-foreground">标题</dt><dd>{itemTitle(item)}</dd></div>
        {relationFields.map(([label, value]) => <div key={label}><dt className="text-sm font-medium text-muted-foreground">{label}</dt><dd><ReadableValue value={value} /></dd></div>)}
      </dl>
      <div><h3 className="mb-1 text-sm font-medium text-muted-foreground">原文 / 主要内容</h3><div className="rounded-md border p-3"><ReadableValue value={item.original ?? item.content ?? item.summary ?? item.description} /></div></div>
      <div><h3 className="mb-1 text-sm font-medium text-muted-foreground">本地化内容</h3><div className="rounded-md border p-3"><ReadableValue value={item.localized} /></div></div>
      <div><h3 className="mb-1 text-sm font-medium text-muted-foreground">翻译内容</h3><div className="rounded-md border p-3"><ReadableValue value={item.translation} /></div></div>
    </div>
  )
}

function SummaryTile({ label, value, help }: { label: string; value: number | undefined; help: string }) {
  return <div className="min-w-[4.75rem] rounded-lg border bg-muted/20 px-2.5 py-1.5 text-center"><div className="text-[11px] text-muted-foreground">{label}</div><div className="text-base font-semibold leading-5">{value ?? '—'}</div><div className="sr-only">{help}</div></div>
}

export function BookLorePage() {
  const pagination = usePaginationSearchParams()
  const requestedTab = pagination.searchParams.get('tab') as BookLoreResource | null
  const resource = RESOURCES.some((item) => item.value === requestedTab) ? requestedTab! : 'entities'
  const search = pagination.searchParams.get('search') ?? ''
  const [searchDraft, setSearchDraft] = useState(search)
  const [summary, setSummary] = useState<BookLoreSummary | null>(null)
  const [payload, setPayload] = useState<BookLorePage | null>(null)
  const [summaryError, setSummaryError] = useState<unknown>()
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)

  useEffect(() => { setSearchDraft(search) }, [search])

  useEffect(() => {
    let active = true
    setSummary(null)
    setSummaryError(undefined)
    getBookLoreSummary()
      .then((value) => {
        if (!value?.scope || !value?.counts) throw new Error('BookLore 摘要缺少 catalog scope 或分类统计。')
        if (active) setSummary(value)
      })
      .catch((reason: unknown) => { if (active) setSummaryError(reason) })
    return () => { active = false }
  }, [reload])

  useEffect(() => {
    if (!summary) { setPayload(null); setLoading(Boolean(!summaryError)); return }
    let active = true
    setLoading(true)
    setError(undefined)
    getBookLoreItems(resource, { ...summary.scope, limit: pagination.limit, offset: pagination.offset, search })
      .then((value) => { if (active) setPayload(value) })
      .catch((reason: unknown) => { if (active) { setPayload(null); setError(reason) } })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [pagination.limit, pagination.offset, resource, search, summary, summaryError])

  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    pagination.setFilters({ search: searchDraft.trim() || null })
  }

  const status = !summary && summaryError ? 'error' : loading ? 'loading' : error ? 'error' : !payload?.items.length ? 'empty' : 'success'

  return (
    <div className="flex flex-col gap-4" data-page="book-lore">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <header className="max-w-2xl">
          <h1 className="text-xl font-bold tracking-tight">BookLore 世界观知识库</h1>
          <p className="text-xs text-muted-foreground">只读浏览实体、社区、关系与笔记。书设是独立 Catalog，直接查询，不经学习投影。</p>
        </header>
        <div className="flex flex-wrap gap-2 text-xs">
          {RESOURCES.map((item) => <SummaryTile key={item.value} label={item.label} value={summary?.counts[item.value]} help={item.help} />)}
        </div>
      </div>

      <Card className="overflow-hidden border-border/60">
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
            <form className="flex min-w-[17rem] flex-1 flex-wrap items-center gap-2" onSubmit={submitSearch}>
              <InputWithIcon value={searchDraft} onValueChange={(v) => setSearchDraft(v)} placeholder="搜索标题、摘要或内容" aria-label="搜索 BookLore" />
              <Button type="submit" size="sm" className="h-8" disabled={loading}>搜索</Button>
              <Button type="button" size="sm" className="h-8" variant="ghost" onClick={() => { setSearchDraft(''); pagination.setFilters({ search: null }) }}>清除</Button>
              <Button type="button" size="sm" className="h-8" variant="outline" disabled={loading} onClick={() => setReload((value) => value + 1)}><RefreshCwIcon aria-hidden="true" /><span className="sr-only">重新读取</span></Button>
            </form>
            <Badge variant="outline">Catalog · 只读</Badge>
          </div>

          <QueryState status={summaryError ? 'error' : summary ? 'success' : 'loading'} error={summaryError} title="BookLore 摘要读取失败" onRetry={() => setReload((value) => value + 1)}>
            {summary ? <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-y bg-muted/15 px-4 py-2 text-xs"><span className="font-medium">Catalog Scope</span><span className="text-muted-foreground">{summary.scope.catalog_id} · {summary.scope.corpus_id} · {summary.scope.version}</span><span className="ml-auto text-muted-foreground">搜索与分页仅作用于当前分类</span></div> : null}
          </QueryState>

          <Tabs value={resource} onValueChange={(tab) => pagination.setFilters({ tab })} className="w-full">
            <div className="px-4 pt-3"><TabsList className="h-8 border bg-muted/40 p-0.5">{RESOURCES.map((item) => <TabsTrigger key={item.value} value={item.value} className="h-7 text-xs">{item.label}<span className="ml-1 text-[10px] text-muted-foreground">{summary?.counts[item.value] ?? '—'}</span></TabsTrigger>)}</TabsList></div>
            {RESOURCES.map((item) => (
              <TabsContent key={item.value} value={item.value} className="mt-3">
                <QueryState status={status} error={error ?? summaryError} title={`${item.label}读取失败`} description={!summary && summaryError ? '无法确认服务端 catalog scope，因此没有使用硬编码默认值继续查询。' : undefined} onRetry={() => setReload((value) => value + 1)}>
                  <DeclarativeDataTable
                    label={`${item.label}知识目录`}
                    items={payload?.items ?? []}
                    keyExtractor={(entry) => String(entry.id)}
                    columns={[
                      { key: 'title', header: '标题', isTitle: true, render: (entry) => itemTitle(entry) },
                      { key: 'summary', header: '摘要 / 内容', render: (entry) => <span className="max-w-2xl truncate text-muted-foreground">{itemSummary(entry)}</span> },
                      { key: 'governance', header: '治理状态', render: (entry) => governance(entry) },
                      { key: 'actions', header: null, render: (entry) => <ResponsiveDetail title={itemTitle(entry)} description={`${item.label}的只读内容与治理状态`} trigger={<Button type="button" variant="ghost" size="icon-sm" aria-label={`查看 ${itemTitle(entry)} 详情`}><EyeIcon aria-hidden="true" /></Button>}><ItemDetail item={entry} resource={item.value} /></ResponsiveDetail> },
                    ]}
                    renderCardSubtitle={(entry) => <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">{itemSummary(entry)}</p>}
                    renderCardActions={(entry) => <ResponsiveDetail title={itemTitle(entry)} description={`${item.label}的只读内容与治理状态`} trigger={<Button type="button" variant="outline" size="sm">查看详情</Button>}><ItemDetail item={entry} resource={item.value} /></ResponsiveDetail>}
                  />
                </QueryState>
              </TabsContent>
            ))}
          </Tabs>
          {payload?.page ? <div className="border-t px-4 py-3"><PaginationControls page={payload.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={loading} label="BookLore 分页" /></div> : null}
        </CardContent>
      </Card>

      <details className="rounded-lg border border-dashed text-sm">
        <summary className="cursor-pointer list-none px-4 py-2.5 font-medium marker:hidden">数据边界与治理说明</summary>
        <p className="border-t px-4 py-3 text-xs leading-relaxed text-muted-foreground">BookLore Catalog 是独立只读知识源，直接供查询与注入使用。隔离项不会被当作可用知识，“解析状态未知”也不等同于解析成功。</p>
      </details>
    </div>
  )
}

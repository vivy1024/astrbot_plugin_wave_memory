import { useEffect, useRef, useState } from 'react'

import { isRequestCancelled } from '@/api/client'
import { getTagQuality, getTags, type TagListPayload, type TagQualityPayload } from '@/api/tags'
import { DeclarativeDataTable, InputWithIcon, PaginationControls, QueryState, type PageSize } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ScopedTagGovernancePanel } from '@/components/tag/ScopedTagGovernancePanel'
import { RefreshCwIcon, ShieldCheckIcon, TagsIcon } from 'lucide-react'

function formatPercent(value: number): string {
  return `${(Math.max(0, Math.min(1, value || 0)) * 100).toFixed(1).replace(/\.0$/, '')}%`
}

const countFormatter = new Intl.NumberFormat('zh-CN')

function formatCount(value: number): string {
  return countFormatter.format(value)
}

function formatConfidence(value: number): string {
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : '未知'
}

function Metric({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return <div className="min-w-0 rounded-lg border bg-card p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 text-2xl font-semibold tracking-tight">{value}</p><p className="mt-1 text-xs text-muted-foreground">{detail}</p></div>
}

const RAG_MODE_LABELS = { semantic: '语义 RAG', static: '静态词表', unavailable: '不可用' } as const
const INDEX_HEALTH_LABELS = { ready: '代次已验证', invalid: '清单无效', unavailable: '索引不可用' } as const
const REASON_LABELS: Record<string, string> = {
  provider_not_configured: '标签提取模型未配置',
  tag_extractor_unavailable: '标签提取器未启动',
  embedding_unavailable: '向量服务不可用',
  tag_index_unavailable: '标签向量索引不可用',
  tag_index_empty: '标签向量索引为空',
  manifest_invalid: '索引清单验证失败',
  manifest_unavailable: '索引尚未生成版本清单',
}

function reasonLabel(value?: string | null): string {
  return value ? REASON_LABELS[value] ?? value : '当前无降级原因'
}

type TagRow = TagListPayload['items'][number]

const TAG_COLUMNS: import('@/components/shared').DataColumn<TagRow>[] = [
  {
    key: 'name',
    header: '标签',
    isTitle: true,
    render: (row) => <span className="font-medium">{row.name}</span>,
  },
  {
    key: 'type',
    header: '类型',
    render: (row) => <Badge variant="outline">{row.type || '未分类'}</Badge>,
  },
  {
    key: 'frequency',
    header: '频率',
    width: '24 text-right',
    render: (row) => <span className="tabular-nums">{row.frequency}</span>,
  },
  {
    key: 'confidence',
    header: '置信度',
    width: '24 text-right',
    render: (row) => <span className="tabular-nums">{formatConfidence(row.confidence)}</span>,
  },
  {
    key: 'capability',
    header: '能力',
    render: () => <Badge variant="secondary">只读</Badge>,
  },
]

function TagsTable({ items, total, limit, offset, loading, onOffset, onLimit }: {
  items: TagRow[]
  total: number
  limit: PageSize
  offset: number
  loading: boolean
  onOffset: (v: number) => void
  onLimit: (v: PageSize) => void
}) {
  return (
    <>
      <DeclarativeDataTable
        label="标签只读目录"
        items={items}
        keyExtractor={(row) => String(row.id)}
        columns={TAG_COLUMNS}
      />
      <PaginationControls
        page={{ limit, offset, total, total_status: 'exact', reason_code: null, page: Math.floor(offset / limit) + 1, page_count: Math.ceil(total / limit) || 1, has_more: offset + limit < total }}
        disabled={loading}
        onOffsetChange={onOffset}
        onLimitChange={onLimit}
        label="标签分页"
      />
    </>
  )
}

export function TagsPage() {
  const [quality, setQuality] = useState<TagQualityPayload | null>(null)
  const [data, setData] = useState<TagListPayload | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [type, setType] = useState('')
  const [sort, setSort] = useState<'frequency' | 'recent'>('frequency')
  const [limit, setLimit] = useState<PageSize>(25)
  const [offset, setOffset] = useState(0)
  const requestRef = useRef<AbortController | null>(null)

  useEffect(() => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    setError(undefined)
    Promise.all([
      getTags({ limit, offset, type, search, sort, signal: controller.signal }),
      getTagQuality(controller.signal),
    ]).then(([tags, nextQuality]) => {
      if (controller.signal.aborted || requestRef.current !== controller) return
      setData(tags)
      setQuality(nextQuality)
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || requestRef.current !== controller || isRequestCancelled(reason)) return
      setData(null)
      setQuality(null)
      setError(reason)
    }).finally(() => {
      if (!controller.signal.aborted && requestRef.current === controller) setLoading(false)
    })
    return () => controller.abort()
  }, [limit, offset, reload, search, sort, type])

  const tagTypes = data?.available_types ?? []
  const status = loading ? 'loading' : error ? 'error' : !data?.items.length ? 'empty' : 'success'
  const total = data?.total ?? 0

  return <div className="flex flex-col gap-5" data-page="tags">
    <ScopedTagGovernancePanel />
    <div className="flex flex-wrap items-start justify-between gap-4">
      <header className="max-w-2xl"><div className="flex items-center gap-2"><TagsIcon className="size-5 text-primary" aria-hidden="true" /><h1 className="text-xl font-bold tracking-tight">标签总览</h1></div><p className="mt-1 text-xs text-muted-foreground">上方治理面板按当前群操作。下面的覆盖率与列表是全局诊断，不是当前群的可写目录。</p></header>
      <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => setReload((value) => value + 1)}><RefreshCwIcon aria-hidden="true" />刷新</Button>
    </div>

    <Alert><ShieldCheckIcon aria-hidden="true" /><AlertTitle>只读安全边界</AlertTitle><AlertDescription>本页不会用裸编号去重命名、改类型或删除标签。需要写入的能力必须通过当前群的正式命令授权。</AlertDescription></Alert>

    <section aria-label="标签质量概览" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="标签总数" value={quality ? formatCount(quality.total_tags) : '—'} detail="当前正式标签记录" />
      <Metric label="记忆覆盖率" value={quality ? formatPercent(quality.coverage) : '—'} detail={quality ? `${formatCount(quality.tagged_memories)} / ${formatCount(quality.total_memories)} 条真实记忆` : '等待真实质量接口'} />
      <Metric label="可提取未标注" value={quality ? formatCount(quality.extractable_untagged_memories ?? 0) : '—'} detail={`短文本跳过 ${quality ? formatCount(quality.skipped_short_untagged_memories ?? 0) : '—'} 条`} />
      <Metric label="孤立引用" value={quality ? formatCount(quality.orphan_memory_tag_refs ?? 0) : '—'} detail="仅诊断，不在本页直接清理" />
    </section>

    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">标签提取与索引状态</CardTitle>
        <CardDescription>展示当前提取能力、索引代次和检索降级路径，不暴露模型编号或物理路径。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 p-4 sm:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">提取能力</span><Badge variant={quality?.runtime.capabilities.extract.available ? 'secondary' : 'outline'}>{quality?.runtime.capabilities.extract.available ? '可用' : '不可用'}</Badge></div>
          <p className="text-sm">{quality?.runtime.capabilities.extract.available ? '标签提取器已启动' : reasonLabel(quality?.runtime.capabilities.extract.reason_code)}</p>
        </div>
        <div className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">标签检索</span><Badge variant={quality?.runtime.rag.mode === 'semantic' ? 'secondary' : 'outline'}>{quality ? RAG_MODE_LABELS[quality.runtime.rag.mode] : '状态未知'}</Badge></div>
          <p className="text-sm">{quality?.runtime.rag.mode === 'semantic' ? '使用消息向量检索已有标签，并同时保留高频静态参考。' : reasonLabel(quality?.runtime.rag.fallback_reason)}</p>
        </div>
        <div className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">标签向量索引</span><Badge variant={quality?.runtime.index.health === 'ready' ? 'secondary' : 'outline'}>{quality ? INDEX_HEALTH_LABELS[quality.runtime.index.health] : '状态未知'}</Badge></div>
          <p className="text-sm tabular-nums">{quality ? `${formatCount(quality.runtime.index.count)} 个向量${quality.runtime.index.generation ? ` · generation ${formatCount(quality.runtime.index.generation)}` : ''}` : '等待运行时状态'}</p>
          {quality?.runtime.index.reason_code ? <p className="text-xs text-muted-foreground">{reasonLabel(quality.runtime.index.reason_code)}</p> : null}
        </div>
      </CardContent>
    </Card>

    <Card className="border-border/60">
      <CardHeader className="gap-3 border-b pb-3">
        <div><CardTitle className="text-sm">标签目录</CardTitle><CardDescription>服务端分页的只读结果；搜索和排序会重新读取真实数据。</CardDescription></div>
        <form className="flex flex-wrap items-center gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setSearch(searchDraft.trim()) }}>
          <InputWithIcon value={searchDraft} onValueChange={(v) => { setSearchDraft(v); }} placeholder="例如：共同记忆…" aria-label="搜索标签" />
          <select aria-label="标签类型" className="h-8 rounded-md border bg-background px-2 text-xs text-foreground" value={type} onChange={(event) => { setOffset(0); setType(event.target.value) }}><option value="">全部类型</option>{tagTypes.map((value) => <option key={value} value={value}>{value}</option>)}</select>
          <select aria-label="排序方式" className="h-8 rounded-md border bg-background px-2 text-xs" value={sort} onChange={(event) => { setOffset(0); setSort(event.target.value === 'recent' ? 'recent' : 'frequency') }}><option value="frequency">按频率</option><option value="recent">按最近创建</option></select>
          <Button type="submit" size="sm">查询</Button>
        </form>
      </CardHeader>
      <CardContent className="p-4">
        <QueryState status={status} error={error} title="标签数据读取失败" onRetry={() => setReload((value) => value + 1)} description={status === 'empty' ? '当前筛选条件下没有标签；未使用演示数据填充。' : undefined}>
          {data?.items.length ? <TagsTable items={data.items} total={total} limit={limit} offset={offset} loading={loading} onOffset={setOffset} onLimit={(v) => { setOffset(0); setLimit(v) }} /> : null}
        </QueryState>
      </CardContent>
    </Card>
  </div>
}

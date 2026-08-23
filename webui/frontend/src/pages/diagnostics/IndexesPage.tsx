import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangleIcon, EyeIcon, RefreshCwIcon, ShieldCheckIcon } from 'lucide-react'

import { isRequestCancelled } from '@/api/client'
import { getIndexDiagnostics, type DiagnosticCheck, type DiagnosticHealth, type IndexDiagnostics } from '@/api/diagnostics'
import { DeclarativeDataTable, InputWithIcon, QueryState, ResponsiveDetail } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const CHECK_LABELS: Record<string, string> = {
  fts: '全文检索索引',
  memory_vectors: '记忆向量存储',
  outbox_consumer_lag: '派生消费进度',
  job_runs: '维护任务运行记录',
  derived_projection: '派生投影',
  memory_manifest: '记忆索引清单',
  tag_manifest: '标签索引清单',
  memory_index_shadow: '记忆运行时索引',
  tag_index_shadow: '标签运行时索引',
  book_lore_source: 'BookLore 数据源',
}

const HEALTH_LABELS: Record<DiagnosticHealth, string> = {
  healthy: '健康',
  empty: '真实为空',
  not_configured: '未配置',
  probe_error: '检查失败',
  drift: '存在漂移',
  repairing: '修复进行中',
}

const EVIDENCE_LABELS: Record<string, string> = {
  reason: '原因代码',
  scope: '检查范围',
  kind: '索引类型',
  count: '记录数',
  memory_count: '记忆数',
  fts_count: '全文索引数',
  missing_rows: '缺失记录',
  orphan_rows: '孤立记录',
  missing_triggers: '缺失触发器',
  canonical_vector_count: '规范向量数',
  runtime_count: '运行时记录数',
  matched_count: '匹配记录数',
  mismatched_count: '不匹配记录数',
  orphan_count: '孤立记录数',
  total_lag: '待消费数',
  active_count: '活动任务数',
  failed_count: '失败任务数',
  lagged_count: '滞后投影数',
  repairing_count: '修复中投影数',
  latest_generation: '最新代次',
  generation: '清单代次',
  committed_watermark: '数据库水位',
  watermark_relation: '水位关系',
  watermark_delta: '水位差',
  checksum_verified: '校验和已验证',
  missing_count: '运行时缺失数',
  sample_size: '抽样数',
  sample_hits: '抽样命中数',
  sample_recall: '抽样召回率',
  size_bytes: '数据源大小（字节）',
  missing_tables: '缺失数据表',
  tables: '数据表计数',
}

function checkLabel(name: string): string { return CHECK_LABELS[name] ?? name }

function formatCheckedAt(value: unknown): string {
  const date = new Date(String(value ?? ''))
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '时间不可用'
}

function healthVariant(health: DiagnosticHealth): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (health === 'healthy') return 'default'
  if (health === 'drift' || health === 'probe_error') return 'destructive'
  if (health === 'repairing') return 'secondary'
  return 'outline'
}

function sourceLabel(source: string): string {
  if (source.startsWith('runtime:')) return '运行时只读探针'
  if (source.startsWith('file:')) return '索引清单只读探针'
  if (source.startsWith('sqlite:')) return 'SQLite 只读探针'
  return 'WaveMemory 只读诊断'
}

function impactText(check: DiagnosticCheck): string {
  if (check.health === 'healthy') return '当前检查未发现对读取结果的已知影响。'
  if (check.health === 'empty') return '数据源真实为空；相关召回可能没有结果，但不等同于故障。'
  if (check.health === 'not_configured') return '该能力未配置；依赖它的检索或投影不会提供结果。'
  if (check.health === 'probe_error') return '无法确认健康度；在重新检查成功前，不应把索引视为可靠。'
  if (check.health === 'repairing') return '后台正在追赶或修复，完成前读取结果可能短暂滞后。'
  if (check.name === 'fts') return '关键词检索可能漏掉新记忆，或返回已删除的旧记录。'
  if (check.name.includes('memory')) return '语义记忆召回可能漏项、包含孤立项，或与数据库水位不一致。'
  if (check.name.includes('tag')) return '标签语义匹配可能漏项，影响分类与关联召回。'
  if (check.name === 'book_lore_source') return 'BookLore 表结构或计数不完整，世界观知识读取可能缺失。'
  return '派生读取可能落后于已提交写入，相关页面暂时看不到最新状态。'
}

function formatEvidenceValue(key: string, value: unknown): string {
  if (value === undefined || value === null || value === '') return '未记录'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return key === 'sample_recall' ? `${(value * 100).toFixed(0)}%` : String(value)
  if (typeof value === 'string') {
    if (/(^[A-Za-z]:[\\/]|^\/|\\|\.db\b|\.bin\b)/i.test(value)) return '内部位置已隐藏'
    return value
  }
  if (Array.isArray(value)) return value.length ? `${value.length} 项` : '无'
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([nestedKey]) => !/(path|file|directory|error)/i.test(nestedKey))
    return entries.length ? entries.slice(0, 6).map(([nestedKey, nestedValue]) => `${nestedKey}: ${formatEvidenceValue(nestedKey, nestedValue)}`).join('；') : '无可展示字段'
  }
  return String(value)
}

function safeEvidenceEntries(check: DiagnosticCheck): Array<[string, unknown]> {
  return Object.entries(check.evidence).filter(([key]) => !/(path|file|directory|error|samples|recent_runs|consumers|ids|blob_sizes|triggers)/i.test(key))
}

function primaryMetric(check: DiagnosticCheck): string {
  const entries = safeEvidenceEntries(check)
  const preferred = ['missing_count', 'missing_rows', 'mismatched_count', 'orphan_count', 'total_lag', 'lagged_count', 'failed_count', 'count', 'runtime_count']
  const match = preferred.map((key) => entries.find(([entryKey]) => entryKey === key)).find(Boolean)
  return match ? `${EVIDENCE_LABELS[match[0]] ?? match[0]} ${formatEvidenceValue(match[0], match[1])}` : impactText(check)
}

function CheckDetail({ check }: { check: DiagnosticCheck }) {
  const evidence = safeEvidenceEntries(check)
  return <div className="flex flex-col gap-4">
    <div className="flex flex-wrap gap-2"><Badge variant={healthVariant(check.health)}>{HEALTH_LABELS[check.health]}</Badge><Badge variant="outline">{sourceLabel(check.source)}</Badge></div>
    <div className="rounded-md border bg-muted/10 p-3"><h3 className="text-sm font-medium">影响</h3><p className="mt-1 text-sm text-muted-foreground">{impactText(check)}</p></div>
    <dl className="grid gap-3 sm:grid-cols-2">{evidence.length ? evidence.map(([key, value]) => <div key={key} className="rounded-md border p-3"><dt className="text-xs font-medium text-muted-foreground">{EVIDENCE_LABELS[key] ?? key}</dt><dd className="mt-1 break-words text-sm">{formatEvidenceValue(key, value)}</dd></div>) : <div className="text-sm text-muted-foreground">该检查没有可安全展示的结构化证据。</div>}</dl>
    <div className="text-xs text-muted-foreground">检查时间：{formatCheckedAt(check.checked_at)}。内部物理路径与原始诊断对象不会在界面中展开。</div>
  </div>
}

function SummaryTile({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return <div className="min-w-0 px-3 py-1.5 text-center"><div className="truncate text-[10px] text-muted-foreground">{label}</div><div className={`text-lg font-semibold leading-5 ${tone ?? ''}`}>{value}</div></div>
}

function HealthStatus({ loading, error, data, issueCount }: { loading: boolean; error: unknown; data: IndexDiagnostics | null; issueCount: number }) {
  if (loading) return <Alert className="py-2"><RefreshCwIcon className="animate-spin" aria-hidden="true" /><AlertTitle>正在读取索引诊断</AlertTitle><AlertDescription>检查完成前，健康度保持未知。</AlertDescription></Alert>
  if (error || !data) return <Alert className="py-2"><AlertTriangleIcon aria-hidden="true" /><AlertTitle>索引健康状态未知</AlertTitle><AlertDescription>诊断接口不可用，当前不能证明索引健康；请重试只读检查。</AlertDescription></Alert>
  if (issueCount > 0) return <Alert variant="destructive" className="py-2"><AlertTriangleIcon aria-hidden="true" /><AlertTitle>检测到索引漂移或检查失败</AlertTitle><AlertDescription><span>先在健康矩阵定位异常；修复仅通过 Maintenance durable job 执行。</span><details className="mt-1"><summary className="cursor-pointer text-xs font-medium">查看影响与安全边界</summary><p className="mt-1 text-xs">漂移可能造成关键词或语义召回漏项、孤立结果及派生页面滞后；检查失败表示当前无法证明索引健康。本页不提供直接 rebuild，任务进入 running 也不代表修复完成。</p></details></AlertDescription></Alert>
  return <Alert className="py-2"><ShieldCheckIcon aria-hidden="true" /><AlertTitle>{data.health === 'repairing' ? '后台修复进行中' : '未发现阻断性索引漂移'}</AlertTitle><AlertDescription>“真实为空”与“未配置”保留原始语义，不会被显示为健康或故障。</AlertDescription></Alert>
}

export function IndexesPage() {
  const [data, setData] = useState<IndexDiagnostics | null>(null)
  const [error, setError] = useState<unknown>()
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [search, setSearch] = useState('')
  const [healthFilter, setHealthFilter] = useState('')
  const requestRef = useRef<AbortController | null>(null)

  useEffect(() => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    setError(undefined)
    getIndexDiagnostics(controller.signal)
      .then((value) => { if (!controller.signal.aborted && requestRef.current === controller) setData(value) })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted && requestRef.current === controller && !isRequestCancelled(reason)) {
          setData(null)
          setError(reason)
        }
      })
      .finally(() => { if (!controller.signal.aborted && requestRef.current === controller) setLoading(false) })
    return () => controller.abort()
  }, [reload])

  const checks = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return (data?.checks ?? []).filter((check) => (!healthFilter || check.health === healthFilter) && (!normalized || `${checkLabel(check.name)} ${check.name}`.toLocaleLowerCase().includes(normalized)))
  }, [data?.checks, healthFilter, search])
  const counts = data?.evidence?.health_counts ?? {}
  const driftCount = counts.drift ?? 0
  const issueCount = driftCount + (counts.probe_error ?? 0)
  const status = loading ? 'loading' : error ? 'error' : !data?.checks?.length ? 'empty' : 'success'

  return <div className="flex flex-col gap-5" data-page="indexes">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <header className="max-w-2xl"><h1 className="text-xl font-bold tracking-tight">索引与派生数据诊断</h1><p className="text-xs text-muted-foreground">以只读探针检查 FTS、向量、索引清单、运行时 shadow、派生消费进度与 BookLore 数据源。</p></header>
      <div className="grid min-w-[20rem] grid-cols-4 divide-x overflow-hidden rounded-lg border bg-muted/15"><SummaryTile label="检查项" value={data ? data.evidence?.probe_count ?? 0 : '—'} /><SummaryTile label="健康" value={data ? counts.healthy ?? 0 : '—'} tone={data ? 'text-emerald-600' : undefined} /><SummaryTile label="漂移" value={data ? driftCount : '—'} tone={driftCount ? 'text-destructive' : ''} /><SummaryTile label="检查失败" value={data ? counts.probe_error ?? 0 : '—'} tone={(counts.probe_error ?? 0) ? 'text-destructive' : ''} /></div>
    </div>

    <HealthStatus loading={loading} error={error} data={data} issueCount={issueCount} />

    <Card className="border-border/60">
      <CardHeader className="gap-3 border-b pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><CardTitle className="text-sm">索引健康矩阵</CardTitle><CardDescription>优先展示各只读探针的当前状态与关键证据。</CardDescription></div>
          <Badge variant="outline" className="text-[10px]">{data ? `检查于 ${formatCheckedAt(data.checked_at)}` : '状态未确认'}</Badge>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <InputWithIcon value={search} onValueChange={setSearch} placeholder="搜索检查名称" aria-label="搜索诊断项" />
          <select aria-label="健康状态" className="h-8 rounded-md border bg-background px-2 text-xs" value={healthFilter} onChange={(event) => setHealthFilter(event.target.value)}>
            <option value="">全部状态</option>
            {Object.entries(HEALTH_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => setReload((value) => value + 1)}><RefreshCwIcon aria-hidden="true" />重新检查</Button>
          <Button asChild size="sm"><Link to="/maintenance?source=diagnostics&panel=indexes">进入 Maintenance 修复任务</Link></Button>
        </div>
      </CardHeader>
      <CardContent className="p-4">
        <QueryState status={status} error={error} title="索引诊断读取失败" onRetry={() => setReload((value) => value + 1)}>
          {checks.length ? (
            <DeclarativeDataTable
              label="索引诊断检查项"
              items={checks}
              keyExtractor={(row) => row.name}
              columns={[
                { key: 'name', header: '检查对象', isTitle: true, render: (row) => <div><div className="font-medium">{checkLabel(row.name)}</div><div className="font-mono text-xs text-muted-foreground">{row.name}</div></div> },
                { key: 'health', header: '健康状态', render: (row) => <Badge variant={healthVariant(row.health)}>{HEALTH_LABELS[row.health]}</Badge> },
                { key: 'evidence', header: '关键证据 / 影响', render: (row) => <span className="max-w-xl text-sm text-muted-foreground">{primaryMetric(row)}</span> },
                { key: 'source', header: '来源', render: (row) => sourceLabel(row.source) },
                { key: 'actions', header: null, render: (row) => <ResponsiveDetail title={checkLabel(row.name)} description="只读健康证据与用户可见影响" trigger={<Button type="button" variant="ghost" size="icon-sm" aria-label={`查看 ${checkLabel(row.name)} 详情`}><EyeIcon aria-hidden="true" /></Button>}><CheckDetail check={row} /></ResponsiveDetail> },
              ]}
            />
          ) : null}
        </QueryState>
      </CardContent>
    </Card>
  </div>
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertCircleIcon,
  ArrowRightIcon,
  CheckCircle2Icon,
  DatabaseIcon,
  Loader2Icon,
  PauseIcon,
  PlayIcon,
  RefreshCwIcon,
  SearchCheckIcon,
  TagsIcon,
} from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import { humanizeApiError } from '@/lib/reason-label'
import {
  getImportSources,
  preflightImport,
  startImport,
  waitForImportJob,
  type ImportPreflightPayload,
  type ImportSourceItem,
} from '@/api/import'
import {
  getMaintenanceCheckpoint,
  getMaintenanceJob,
  getMaintenanceLogs,
  type MaintenanceCheckpoint,
  type MaintenanceJob,
  type MaintenanceLog,
} from '@/api/maintenance'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'

const activeStatuses = new Set(['pending', 'queued', 'running'])
const maintenancePath = '/' + 'maintenance'
const wizardSteps = ['配置检查', '数据源发现', '导入预览', '执行导入', 'Tag 提取', '结果复核']

function statusLabel(status?: string): string {
  return ({
    pending: '等待调度',
    queued: '排队中',
    running: '执行中',
    succeeded: '已成功',
    failed: '已失败',
    cancelled: '已取消',
  } as Record<string, string>)[status ?? ''] ?? '状态未知'
}

function statusVariant(status?: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'succeeded') return 'default'
  if (status === 'failed') return 'destructive'
  if (activeStatuses.has(status ?? '')) return 'secondary'
  return 'outline'
}

function formatTime(value?: number | null): string {
  return value && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function numberFrom(records: Array<Record<string, unknown> | null | undefined>, keys: string[]): number | null {
  for (const record of records) {
    if (!record) continue
    for (const key of keys) {
      const value = Number(record[key])
      if (Number.isFinite(value)) return value
    }
  }
  return null
}

function jobProgress(job: MaintenanceJob | null) {
  const records = [job?.progress, job?.cursor, job?.result]
  const processed = numberFrom(records, ['processed', 'imported', 'current']) ?? 0
  const total = numberFrom(records, ['total', 'total_count', 'selected']) ?? 0
  const tagged = numberFrom(records, ['tagged', 'tagged_count']) ?? 0
  const skipped = numberFrom(records, ['skipped', 'duplicates']) ?? 0
  const errors = numberFrom(records, ['errors', 'failed']) ?? 0
  const explicit = numberFrom(records, ['progress', 'ratio', 'percent'])
  const ratio = explicit === null ? (total > 0 ? processed / total : job?.status === 'succeeded' ? 1 : 0) : explicit > 1 ? explicit / 100 : explicit
  return { processed, total, tagged, skipped, errors, ratio: Math.min(1, Math.max(0, ratio)) }
}

function logSummary(log: MaintenanceLog): string {
  const data = log.data && typeof log.data === 'object' ? log.data as Record<string, unknown> : null
  if (data && typeof data.message === 'string') return data.message
  if (data && typeof data.status === 'string') return `状态：${statusLabel(data.status)}`
  return ({ scheduled: '任务已进入后台队列', checkpoint: '后台已更新导入进度', result: '后台已写入最终结果', failure: '后台任务执行失败' } as Record<string, string>)[log.event] ?? log.event
}

export function ImportPage() {
  const [params, setParams] = useSearchParams()
  const jobId = params.get('job_id') ?? ''
  const [sources, setSources] = useState<ImportSourceItem[]>([])
  const [status, setStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [sourceId, setSourceId] = useState('')
  const [limit, setLimit] = useState(2000)
  const [extractTags, setExtractTags] = useState(true)
  const [tagBatchSize, setTagBatchSize] = useState(20)
  const [preflight, setPreflight] = useState<ImportPreflightPayload | null>(null)
  const [job, setJob] = useState<MaintenanceJob | null>(null)
  const [logs, setLogs] = useState<MaintenanceLog[]>([])
  const [checkpoint, setCheckpoint] = useState<MaintenanceCheckpoint | null>(null)
  const [jobError, setJobError] = useState<unknown>()
  const [auxiliaryErrors, setAuxiliaryErrors] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [tracking, setTracking] = useState(false)
  const [pollVersion, setPollVersion] = useState(0)
  const pollingRef = useRef<AbortController | null>(null)
  const announcedRef = useRef<string>('')

  const selectedSource = sources.find((source) => source.id === sourceId) ?? null
  const progress = jobProgress(job)

  const loadSources = useCallback(async () => {
    setStatus('loading')
    setError(undefined)
    try {
      const result = await getImportSources()
      setSources(result.sources ?? [])
      setStatus(result.sources.length ? 'success' : 'empty')
    } catch (reason) {
      setError(reason)
      setStatus('error')
    }
  }, [])

  useEffect(() => { void loadSources() }, [loadSources])

  useEffect(() => {
    pollingRef.current?.abort()
    setJob(null)
    setLogs([])
    setCheckpoint(null)
    setJobError(undefined)
    setAuxiliaryErrors([])
    if (!jobId) {
      setTracking(false)
      return
    }
    const controller = new AbortController()
    pollingRef.current = controller
    setTracking(true)

    let auxiliaryInFlight = false
    const updateAuxiliary = async () => {
      if (auxiliaryInFlight || controller.signal.aborted) return
      auxiliaryInFlight = true
      try {
        const [logsResult, checkpointResult] = await Promise.allSettled([
          getMaintenanceLogs(jobId, 100, 0, controller.signal),
          getMaintenanceCheckpoint(jobId, controller.signal),
        ])
        if (controller.signal.aborted) return

        const failures: string[] = []
        if (logsResult.status === 'fulfilled') setLogs(logsResult.value.items)
        else {
          setLogs([])
          failures.push(`日志：${humanizeApiError(logsResult.reason, '未知错误')}`)
        }
        if (checkpointResult.status === 'fulfilled') setCheckpoint(checkpointResult.value)
        else {
          setCheckpoint(null)
          failures.push(`检查点：${humanizeApiError(checkpointResult.reason, '未知错误')}`)
        }
        setAuxiliaryErrors(failures)
      } finally {
        auxiliaryInFlight = false
      }
    }

    const resume = async () => {
      try {
        const initial = (await getMaintenanceJob(jobId, controller.signal)).item
        if (controller.signal.aborted) return
        setJob(initial)
        void updateAuxiliary()
        if (!activeStatuses.has(initial.status)) return initial
        return await waitForImportJob(jobId, (next) => {
          setJob(next)
          void updateAuxiliary()
        }, controller.signal)
      } catch (reason) {
        if (!controller.signal.aborted && !isRequestCancelled(reason)) setJobError(reason)
        return null
      } finally {
        if (pollingRef.current === controller) setTracking(false)
      }
    }

    void resume().then((completed) => {
      if (!completed || controller.signal.aborted || announcedRef.current === `${completed.run_id}:${completed.status}`) return
      announcedRef.current = `${completed.run_id}:${completed.status}`
      if (completed.status === 'succeeded') toast.success('导入任务已完成，可进行结果复核')
      else if (completed.status === 'failed') toast.error(completed.error_message || '导入任务失败')
    })

    return () => controller.abort()
  }, [jobId, pollVersion])

  function resetPreflight() {
    setPreflight(null)
  }

  async function inspect() {
    if (!sourceId) return
    setBusy(true)
    try {
      const result = await preflightImport(sourceId, {
        limit,
        extract_tags: extractTags,
        tag_batch_size: tagBatchSize,
        tag_write_policy: 'missing_only',
      })
      setPreflight(result)
      toast.success('真实预检已完成，请核对结构化结果后再创建任务')
    } catch (reason) {
      toast.error(humanizeApiError(reason, '导入预检失败'))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!preflight) return
    setBusy(true)
    try {
      const accepted = await startImport(sourceId, preflight.preflight_token)
      setJob({
        run_id: accepted.job_id,
        request_id: accepted.request_id,
        status: accepted.status,
        kind: 'maintenance.import.run',
        operation: accepted.operation,
      })
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('job_id', accepted.job_id)
        return next
      })
      toast.info('导入已受理；job_id 已写入 URL，刷新页面可恢复跟踪。')
    } catch (reason) {
      toast.error(humanizeApiError(reason, '导入任务创建失败'))
    } finally {
      setBusy(false)
    }
  }

  function stopTracking() {
    pollingRef.current?.abort()
    setTracking(false)
    toast.info('仅停止当前页面轮询，服务端任务会继续执行。')
  }

  function clearJob() {
    pollingRef.current?.abort()
    setJob(null)
    setLogs([])
    setCheckpoint(null)
    setJobError(undefined)
    setAuxiliaryErrors([])
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.delete('job_id')
      return next
    })
  }

  const totalSourceCount = sources.reduce((sum, source) => sum + Number(source.count ?? 0), 0)
  const importedEstimate = sources.reduce((sum, source) => sum + Math.round(Number(source.count ?? 0) * Number(source.imported_pct ?? 0) / 100), 0)
  const remainingEstimate = sources.reduce((sum, source) => sum + Number(source.remaining ?? 0), 0)
  const duplicateEstimate = Math.max(totalSourceCount - importedEstimate - remainingEstimate, 0)
  const preflightDuplicateEstimate = preflight ? Math.max(preflight.preview.total_count - preflight.preview.estimated_imported - preflight.preview.estimated_remaining, 0) : 0
  const completedSteps = [
    Boolean(sourceId),
    status === 'success',
    Boolean(preflight),
    Boolean(job),
    Boolean(job && (!extractTags || job.status === 'succeeded')),
    job?.status === 'succeeded',
  ]

  return <div data-slot="import-page" className="flex flex-col gap-6">
    <Card>
      <CardHeader><CardTitle>智能导入向导</CardTitle><CardDescription>发现真实外部来源，先进行结构化 preflight，再创建可恢复的 durable import job；HTTP 202/queued 不代表完成。</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-3 md:grid-cols-6">{wizardSteps.map((step, index) => <div key={step} className={`rounded-lg border p-3 ${completedSteps[index] ? 'border-primary/40 bg-primary/5' : 'bg-muted/20'}`}><div className="flex items-center justify-between gap-2"><Badge variant={completedSteps[index] ? 'default' : 'outline'}>{index + 1}</Badge>{completedSteps[index] ? <CheckCircle2Icon className="size-4 text-primary" /> : null}</div><p className="mt-3 text-sm font-medium">{step}</p></div>)}</div>
        <Alert><AlertCircleIcon /><AlertTitle>安全执行边界</AlertTitle><AlertDescription>本页不调用旧 SSE 导入或任意 rebuild；preflight token 只由当前预检产生并原样提交，导入进度统一从 durable job 查询。</AlertDescription></Alert>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle className="text-base">1. 数据源与导入策略</CardTitle><CardDescription>选择 discovery 返回且具备 adapter 的真实来源；修改任何选项后需重新预检。</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <QueryState status={status} error={error} onRetry={() => void loadSources()} title="未发现可导入来源" description="当前 discovery 没有返回具备 adapter 的外部记忆源。">
            <Field className="lg:col-span-2"><FieldLabel>导入来源</FieldLabel><Select value={sourceId} onValueChange={(value) => { setSourceId(value); resetPreflight() }}><SelectTrigger><SelectValue placeholder="选择真实来源" /></SelectTrigger><SelectContent><SelectGroup>{sources.map((source) => <SelectItem key={source.id} value={source.id} disabled={!source.has_adapter}>{source.name} · {source.count.toLocaleString('zh-CN')} 条{source.has_adapter ? '' : '（无 adapter）'}</SelectItem>)}</SelectGroup></SelectContent></Select><FieldDescription>{selectedSource?.description ?? '来源必须来自当前 discovery 结果。'}</FieldDescription></Field>
          </QueryState>
          <Field><FieldLabel>最大导入条数</FieldLabel><Input type="number" min={1} max={50000} value={limit} disabled={busy || tracking} onChange={(event) => { setLimit(Math.max(1, Math.min(50000, Number(event.target.value) || 1))); resetPreflight() }} /></Field>
          <Field><FieldLabel>同步提取标签</FieldLabel><div className="flex h-10 items-center justify-between rounded-md border px-3"><span className="text-sm">{extractTags ? '开启' : '关闭'}</span><Switch checked={extractTags} disabled={busy || tracking} onCheckedChange={(value) => { setExtractTags(value); resetPreflight() }} /></div></Field>
          <Field><FieldLabel>标签批大小</FieldLabel><Input type="number" min={1} max={50} value={tagBatchSize} disabled={!extractTags || busy || tracking} onChange={(event) => { setTagBatchSize(Math.max(1, Math.min(50, Number(event.target.value) || 1))); resetPreflight() }} /><FieldDescription>固定只补缺，不覆盖已有标签。</FieldDescription></Field>
          <div className="flex items-end"><Button disabled={!sourceId || busy || tracking} onClick={() => void inspect()}>{busy && !preflight ? <Loader2Icon className="animate-spin" /> : <SearchCheckIcon />}运行真实预检</Button></div>
        </div>

        <div className="grid gap-3 md:grid-cols-4">
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">发现来源</p><p className="mt-2 text-xl font-semibold">{sources.length}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">来源总条数</p><p className="mt-2 text-xl font-semibold">{totalSourceCount.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">已导入估计</p><p className="mt-2 text-xl font-semibold">{importedEstimate.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">重复估计</p><p className="mt-2 text-xl font-semibold">{duplicateEstimate.toLocaleString('zh-CN')}</p></div>
        </div>
      </CardContent>
    </Card>

    {preflight ? <Card>
      <CardHeader><CardTitle className="text-base">2. 结构化预检</CardTitle><CardDescription>预检于 {formatTime(preflight.checked_at)} 完成 · 来源状态：{preflight.source_status === 'available' ? '可用' : '未知'}。请核对后再执行。</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid gap-3 md:grid-cols-3 lg:grid-cols-4">
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">数据源</p><p className="mt-2 font-semibold">{preflight.source.name}</p><p className="mt-1 text-xs text-muted-foreground">{preflight.source.description}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">总条数</p><p className="mt-2 text-xl font-semibold">{preflight.preview.total_count.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">本次上限</p><p className="mt-2 text-xl font-semibold">{preflight.preview.limit.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">预计待导入</p><p className="mt-2 text-xl font-semibold">{preflight.preview.estimated_remaining.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">预计已导入</p><p className="mt-2 text-xl font-semibold">{preflight.preview.estimated_imported.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">重复估计</p><p className="mt-2 text-xl font-semibold">{preflightDuplicateEstimate.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">向量处理</p><p className="mt-2 font-semibold">{preflight.preview.re_embed ? '会重新生成向量' : '保留来源向量策略'}</p></div>
          <div className="rounded-lg border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">标签处理</p><p className="mt-2 font-semibold">{preflight.preview.extract_tags ? `同步提取 · 每批 ${tagBatchSize}` : '本次不提取'}</p></div>
        </div>
        <div className="flex flex-wrap items-center gap-3"><Button disabled={busy || tracking || Boolean(jobId && activeStatuses.has(job?.status ?? ''))} onClick={() => void start()}>{busy ? <Loader2Icon className="animate-spin" /> : <PlayIcon />}确认创建 Durable Job</Button><p className="text-xs text-muted-foreground">token 不展示、不编辑，且仅用于这次来源与参数完全一致的导入。</p></div>
      </CardContent>
    </Card> : null}

    {job ? <Card>
      <CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="text-base">3. 外部记忆导入任务状态</CardTitle><CardDescription>刷新页面会从 URL 中的 job_id 恢复此任务；服务端执行不依赖页面保持打开。</CardDescription></div><Badge variant={statusVariant(job.status)}>{statusLabel(job.status)}</Badge></div></CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="flex justify-between gap-3 text-sm"><span>导入进度</span><span>{Math.round(progress.ratio * 100)}% · 已处理 {progress.processed.toLocaleString('zh-CN')}{progress.total ? ` / ${progress.total.toLocaleString('zh-CN')}` : ''}</span></div>
        <div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.round(progress.ratio * 100)}%` }} /></div>
        <div className="grid gap-3 md:grid-cols-4">
          <div className="rounded-lg border bg-muted/20 p-3"><p className="text-xs text-muted-foreground">已处理</p><p className="mt-1 text-lg font-semibold">{progress.processed.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-3"><p className="text-xs text-muted-foreground">已打标签</p><p className="mt-1 text-lg font-semibold">{progress.tagged.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-3"><p className="text-xs text-muted-foreground">跳过 / 重复</p><p className="mt-1 text-lg font-semibold">{progress.skipped.toLocaleString('zh-CN')}</p></div>
          <div className="rounded-lg border bg-muted/20 p-3"><p className="text-xs text-muted-foreground">失败</p><p className="mt-1 text-lg font-semibold">{progress.errors.toLocaleString('zh-CN')}</p></div>
        </div>
        {auxiliaryErrors.length ? <Alert><AlertCircleIcon /><AlertTitle>部分任务附属信息不可用</AlertTitle><AlertDescription>{auxiliaryErrors.join('；')}。核心 job 状态仍可继续跟踪。</AlertDescription></Alert> : null}
        <div><h3 className="mb-2 text-sm font-semibold">进度与日志</h3><div className="max-h-56 overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs">{logs.length ? logs.map((log, index) => <div key={`${log.at}-${log.event}-${index}`} className={log.level === 'error' ? 'text-destructive' : 'text-muted-foreground'}>[{formatTime(log.at)}] {logSummary(log)}</div>) : <p className="text-muted-foreground">{auxiliaryErrors.length ? '日志或 checkpoint 当前不可用。' : '等待后台任务日志与 checkpoint...'}</p>}</div></div>
        {job.error_message ? <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>导入失败</AlertTitle><AlertDescription>{job.error_message}</AlertDescription></Alert> : null}
        <div className="flex flex-wrap items-center justify-between gap-3"><details className="rounded-md border p-3 text-xs text-muted-foreground"><summary className="cursor-pointer font-medium text-foreground">技术详情</summary><div className="mt-2 font-mono">job_id: {job.run_id}<br />request_id: {job.request_id}<br />checkpoint: {typeof checkpoint?.checkpoint?.phase === 'string' ? checkpoint.checkpoint.phase : '未记录'}<br />error_code: {job.error_code ?? '无'}</div></details><div className="flex flex-wrap gap-2">{tracking ? <Button variant="outline" onClick={stopTracking}><PauseIcon />停止页面轮询</Button> : activeStatuses.has(job.status) ? <Button variant="outline" onClick={() => setPollVersion((value) => value + 1)}><RefreshCwIcon />恢复跟踪</Button> : null}<Button variant="ghost" onClick={clearJob}>关闭任务卡片</Button></div></div>
      </CardContent>
    </Card> : jobId ? <Card><CardContent className="py-6">{jobError ? <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>无法恢复 URL 中的导入任务</AlertTitle><AlertDescription className="flex flex-col gap-3"><span>{humanizeApiError(jobError, 'job_id 无效、已删除或当前账号不可访问。')}</span><span className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setPollVersion((value) => value + 1)}><RefreshCwIcon />重试</Button><Button type="button" size="sm" variant="ghost" onClick={clearJob}>清除无效任务引用</Button></span></AlertDescription></Alert> : <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在通过 URL 中的 job_id 恢复导入任务...</div>}</CardContent></Card> : null}

    <Card>
      <CardHeader><CardTitle className="text-base">4. 结果复核</CardTitle><CardDescription>导入完成后分别检查新数据、标签审计和系统覆盖率。</CardDescription></CardHeader>
      <CardContent className="grid gap-3 md:grid-cols-3">
        <Button asChild variant="outline" size="lg"><Link to="/memories"><DatabaseIcon />查看新导入记忆<ArrowRightIcon /></Link></Button>
        <Button asChild variant="outline" size="lg"><Link to={`${maintenancePath}?tab=workbench`}><TagsIcon />复核标签与审计<ArrowRightIcon /></Link></Button>
        <Button asChild variant="outline" size="lg"><Link to="/dashboard"><RefreshCwIcon />查看覆盖率变化<ArrowRightIcon /></Link></Button>
      </CardContent>
    </Card>
  </div>
}

export default ImportPage

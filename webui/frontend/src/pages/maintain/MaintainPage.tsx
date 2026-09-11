import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  HistoryIcon,
  Loader2Icon,
  PlayIcon,
  SquareIcon,
} from 'lucide-react'
import { toast } from 'sonner'

import {
  cancelMaintenanceJob,
  getMaintenanceLogs,
  startTagBackfill,
  waitForMaintenanceJob,
  type MaintenanceJob,
  type MaintenanceLog,
} from '@/api/maintenance'
import {
  getTagQuality,
  type TagExecutionOptions,
  type TagQualityPayload,
} from '@/api/tags'
import { getSystemStatus, type SystemPayload } from '@/api/system'
import { TagExtractionConfigPanel } from '@/components/tag/TagExtractionConfigPanel'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { humanizeApiError } from '@/lib/reason-label'

const activeJobStatuses = new Set(['pending', 'queued', 'running'])

function valueFrom(records: Array<Record<string, unknown> | null | undefined>, keys: string[]): number | null {
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
  const processed = valueFrom(records, ['processed', 'processed_count', 'imported', 'current']) ?? 0
  const total = valueFrom(records, ['total', 'total_count', 'total_scanned', 'selected']) ?? 0
  const tagged = valueFrom(records, ['tagged', 'written']) ?? 0
  const errors = valueFrom(records, ['errors', 'failed']) ?? 0
  const explicit = valueFrom(records, ['progress', 'ratio', 'percent'])
  const ratio = explicit === null
    ? (total > 0 ? processed / total : 0)
    : explicit > 1 ? explicit / 100 : explicit
  return { processed, total, tagged, errors, ratio: Math.min(1, Math.max(0, ratio)) }
}

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

function formatLog(log: MaintenanceLog): string {
  const time = log.at ? new Date(log.at * 1000).toLocaleTimeString('zh-CN') : '--:--:--'
  const data = log.data && typeof log.data === 'object' ? log.data as Record<string, unknown> : null
  const message = data && typeof data.message === 'string'
    ? data.message
    : data && typeof data.status === 'string'
      ? `状态：${statusLabel(data.status)}`
      : ''
  return `[${time}] ${log.event}${message ? ` · ${message}` : ''}`
}

export function MaintainPage() {
  const [, setParams] = useSearchParams()
  const [sys, setSys] = useState<SystemPayload | null>(null)
  const [quality, setQuality] = useState<TagQualityPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState('extract')
  const [submitting, setSubmitting] = useState(false)
  const [job, setJob] = useState<MaintenanceJob | null>(null)
  const [jobLogs, setJobLogs] = useState<MaintenanceLog[]>([])
  const [jobType, setJobType] = useState<'extract' | null>(null)
  const pollingRef = useRef<AbortController | null>(null)

  const [tagBatchSize, setTagBatchSize] = useState(20)
  const [skipShortMinLength, setSkipShortMinLength] = useState(10)

  const running = submitting || Boolean(job && activeJobStatuses.has(job.status))
  const progress = jobProgress(job)

  const handleTagOptionsChange = useCallback((options: Required<TagExecutionOptions>) => {
    setTagBatchSize(options.tag_batch_size)
    setSkipShortMinLength(options.skip_short_min_length)
  }, [])

  const loadData = useCallback(async () => {
    setError('')
    try {
      const [sysPayload, qualityPayload] = await Promise.all([
        getSystemStatus(),
        getTagQuality(),
      ])
      setSys(sysPayload)
      setQuality(qualityPayload)
    } catch (reason) {
      setError(humanizeApiError(reason, '标签与维护数据加载失败'))
    }
  }, [])

  useEffect(() => {
    void loadData().finally(() => setLoading(false))
    return () => pollingRef.current?.abort()
  }, [loadData])

  const exposeJobInUrl = useCallback((jobId: string, openHistory = false) => {
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.set('job_id', jobId)
      next.set('tab', openHistory ? 'jobs' : 'workbench')
      return next
    })
  }, [setParams])

  const followJob = useCallback(async (acceptedJob: MaintenanceJob) => {
    pollingRef.current?.abort()
    const controller = new AbortController()
    pollingRef.current = controller
    setJob(acceptedJob)
    try {
      const completed = await waitForMaintenanceJob(acceptedJob.run_id, (next) => {
        setJob(next)
        void getMaintenanceLogs(next.run_id, 100, 0).then((page) => setJobLogs(page.items)).catch(() => undefined)
      }, controller.signal)
      if (completed.status === 'succeeded') toast.success('维护任务已成功完成')
      else if (completed.status === 'failed') toast.error(completed.error_message || '维护任务执行失败')
      else toast.info(`维护任务终态：${statusLabel(completed.status)}`)
      await loadData()
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
        toast.error(humanizeApiError(reason, '任务状态跟踪失败'))
      }
    } finally {
      if (pollingRef.current === controller) pollingRef.current = null
    }
  }, [loadData])

  async function handleStartExtract() {
    if (running) return
    setSubmitting(true)
    setJobType('extract')
    setJob(null)
    setJobLogs([])
    try {
      const accepted = await startTagBackfill({ tag_batch_size: tagBatchSize, skip_short_min_length: skipShortMinLength })
      const initial: MaintenanceJob = {
        run_id: accepted.job_id,
        request_id: accepted.request_id,
        status: accepted.status,
        kind: 'maintenance.tag_backfill.run',
        operation: accepted.operation,
      }
      exposeJobInUrl(accepted.job_id)
      toast.info('标签提取任务已进入 durable 队列，可离开页面后从任务历史恢复查看。')
      void followJob(initial)
    } catch (reason) {
      toast.error(humanizeApiError(reason, '标签提取任务创建失败'))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleCancelJob() {
    if (!job || !activeJobStatuses.has(job.status)) return
    try {
      const result = await cancelMaintenanceJob(job.run_id)
      setJob(result.item)
      toast.info('取消请求已提交；请以任务最终状态为准。')
    } catch (reason) {
      toast.error(humanizeApiError(reason, '取消请求失败'))
    }
  }

  if (loading) {
    return <div className="flex flex-col gap-6"><Skeleton className="h-24 w-full" /><Skeleton className="h-96 w-full" /></div>
  }

  if (error) {
    return <Alert variant="destructive"><AlertCircleIcon /><AlertTitle>维护数据加载失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>
  }

  const untaggedCount = Math.max(0, Number(quality?.untagged_memories ?? ((sys?.memories?.total ?? 0) - (sys?.memories?.with_tags ?? 0))))
  const extractableCount = Math.max(0, Number(quality?.extractable_untagged_memories ?? untaggedCount))
  const skippedShortCount = Math.max(0, Number(quality?.skipped_short_untagged_memories ?? 0))
  const orphanRefs = Math.max(0, Number(quality?.orphan_memory_tag_refs ?? 0))
  const coverage = quality ? `${(quality.coverage * 100).toFixed(1)}%` : '未记录'

  return <div data-slot="maintain-workbench" className="flex flex-col gap-6">
    <div className="grid gap-4 md:grid-cols-3">
      <Card><CardHeader className="pb-2">        <CardDescription>系统总标签数</CardDescription><CardTitle className="font-mono text-2xl">{Number(quality?.total_tags ?? 0).toLocaleString('zh-CN')}</CardTitle></CardHeader></Card>
      <Card><CardHeader className="pb-2"><CardDescription>记忆标签覆盖率</CardDescription><CardTitle className="font-mono text-2xl">{coverage}</CardTitle></CardHeader></Card>
      <Card><CardHeader className="pb-2"><CardDescription>可提取无标签记忆</CardDescription><CardTitle className="font-mono text-2xl">{extractableCount.toLocaleString('zh-CN')}</CardTitle><CardDescription>短文本跳过 {skippedShortCount.toLocaleString('zh-CN')}{orphanRefs ? ` · 孤儿关联 ${orphanRefs.toLocaleString('zh-CN')}` : ''}</CardDescription></CardHeader></Card>
    </div>

    <Card>
      <CardHeader><CardTitle>标签维护工作台</CardTitle><CardDescription>标签补提取和质量审计只创建当前 durable job，不执行任意 token rebuild；建议处理沿用后端作用域与写入门禁。</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-5">
        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList><TabsTrigger value="extract">批量标签提取</TabsTrigger><TabsTrigger value="audit">质量审计与建议审核</TabsTrigger></TabsList>
          <TabsContent value="extract" className="mt-5 flex flex-col gap-4">
            <TagExtractionConfigPanel
              title="维护中心标签提取配置"
              description="每次只提交一个有界 durable batch；后端固定 missing_only，避免改写已有标签。"
              onOptionsChange={handleTagOptionsChange}
              disabled={running}
              showSkipShort
            />
            <div className="rounded-xl border bg-muted/10 p-5">
              <h3 className="text-sm font-semibold">无标签记忆补提取</h3>
              <p className="mt-1 text-xs text-muted-foreground">当前可提取 {extractableCount.toLocaleString('zh-CN')} 条。任务进入后台队列后可刷新或离开页面，进度不会丢失。</p>
              <div className="mt-5 grid max-w-2xl gap-4 md:grid-cols-2">
                <Field><FieldLabel>本批处理上限</FieldLabel><Input type="number" min={1} max={50} value={tagBatchSize} disabled={running} onChange={(event) => setTagBatchSize(Math.max(1, Math.min(50, Number(event.target.value) || 1)))} /></Field>
                <Field><FieldLabel>跳过短文本阈值</FieldLabel><Input type="number" min={0} max={1000} value={skipShortMinLength} disabled={running} onChange={(event) => setSkipShortMinLength(Math.max(0, Number(event.target.value) || 0))} /></Field>
              </div>
              <Button className="mt-4" size="sm" disabled={running || extractableCount === 0} onClick={() => void handleStartExtract()}>{running && jobType === 'extract' ? <Loader2Icon className="animate-spin" /> : <PlayIcon />}提交标签提取任务</Button>
            </div>
          </TabsContent>

          <TabsContent value="audit" className="mt-5 flex flex-col gap-5">
            <div className="rounded-xl border bg-muted/10 p-5">
              <h3 className="text-sm font-semibold">标签治理与审核</h3>
              <p className="mt-1 text-xs text-muted-foreground">合并、重分类、增加别名与停用的治理建议统一在「标签」页的「当前群标签治理」面板发起、预检和批准；那里按当前 Bot 和群操作，并要求先预检再批准。这里不再维护旧的后台审计队列与只读建议列表。</p>
              <div className="mt-5 flex flex-wrap items-center gap-2">
                <Button asChild size="sm" variant="outline"><Link to="/tags">在标签工作台审核</Link></Button>
                <span className="text-xs text-muted-foreground">进入后选择 Bot 与群即可创建并审批建议。</span>
              </div>
            </div>
          </TabsContent>
        </Tabs>

        {(submitting || job) ? <div className="flex flex-col gap-3 border-t pt-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm font-medium">{running ? <Loader2Icon className="size-4 animate-spin text-primary" /> : job?.status === 'succeeded' ? <CheckCircle2Icon className="size-4 text-primary" /> : <AlertCircleIcon className="size-4 text-destructive" />}<span>{jobType === 'extract' ? '标签提取' : '质量审计'} · {submitting ? '正在创建任务' : statusLabel(job?.status)}</span></div>
            {job ? <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => exposeJobInUrl(job.run_id, true)}><HistoryIcon />在任务历史查看</Button>{activeJobStatuses.has(job.status) ? <Button size="sm" variant="destructive" onClick={() => void handleCancelJob()}><SquareIcon />请求取消</Button> : null}</div> : null}
          </div>
          {job ? <><div className="flex justify-between gap-3 text-xs text-muted-foreground"><span>已处理 {progress.processed}{progress.total ? ` / ${progress.total}` : ''}</span><span>{Math.round(progress.ratio * 100)}%{jobType === 'extract' ? ` · 写入 ${progress.tagged} · 失败 ${progress.errors}` : ''}</span></div><div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.round(progress.ratio * 100)}%` }} /></div></> : null}
          <ScrollArea className="h-40 rounded-lg border bg-muted/40 p-3 font-mono text-xs text-muted-foreground">{jobLogs.length ? jobLogs.map((log, index) => <div key={`${log.at}-${log.event}-${index}`}>{formatLog(log)}</div>) : <div>等待后台任务日志与 checkpoint...</div>}</ScrollArea>
          {job ? <details className="rounded-md border p-3 text-xs text-muted-foreground"><summary className="cursor-pointer font-medium text-foreground">技术详情</summary><dl className="mt-2 grid gap-1 font-mono"><div>job_id: {job.run_id}</div><div>request_id: {job.request_id}</div>{job.error_code ? <div>error_code: {job.error_code}</div> : null}{job.error_message ? <div>error_message: {job.error_message}</div> : null}</dl></details> : null}
        </div> : null}
      </CardContent>
    </Card>

  </div>
}

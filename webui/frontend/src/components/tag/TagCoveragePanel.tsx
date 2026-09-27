import { useCallback, useEffect, useState } from 'react'
import { ChevronDownIcon, ChevronRightIcon, PlayIcon, RefreshCwIcon, RotateCcwIcon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import {
  getTagCoverage,
  requeueTagCategory,
  wakeTagWorker,
  type TagCoverageCategory,
  type TagCoverageReport,
  type TagRequeueCategory,
} from '@/api/tags'
import { QueryState } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const ORDER: TagCoverageCategory[] = ['tagged', 'pending', 'lost', 'failed', 'skipped', 'too_short', 'not_eligible']

const COLORS: Record<TagCoverageCategory, string> = {
  tagged: 'bg-emerald-500',
  pending: 'bg-sky-500',
  lost: 'bg-amber-500',
  failed: 'bg-rose-500',
  skipped: 'bg-slate-400',
  too_short: 'bg-slate-300 dark:bg-slate-600',
  not_eligible: 'bg-slate-200 dark:bg-slate-700',
}

/** 每类是什么意思、要不要处理 */
const EXPLAIN: Record<TagCoverageCategory, string> = {
  tagged: '正式标签或旧标签任一存在。',
  pending: 'TagWorker 会处理，还没轮到。',
  lost: '提取状态是「完成」但标签已不在（历史清理删掉了关联）。重新提取即可恢复，会消耗 LLM 调用。',
  failed: '提取出错。失败 5 次后不再自动重试，可以手动重试。',
  skipped: '模型判定没有可用标签（多是「@某人 收到」这类）。一般不用处理；换了提取模型后可以重试。',
  too_short: '不足最小长度，按规则不提取。',
  not_eligible: 'TagWorker 不处理的记忆：私聊，或缺少 Bot 归属的旧行。',
}

const NEEDS_WORK: TagCoverageCategory[] = ['pending', 'lost', 'failed']

const number = new Intl.NumberFormat('zh-CN')

function pct(value: number, digits = 1): string {
  return `${(Math.max(0, Math.min(1, value || 0)) * 100).toFixed(digits).replace(/\.0$/, '')}%`
}

function ago(ts?: number | null): string {
  if (!ts) return '尚未运行'
  const seconds = Math.max(0, Date.now() / 1000 - ts)
  if (seconds < 90) return `${Math.round(seconds)} 秒前`
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟前`
  return `${Math.round(seconds / 3600)} 小时前`
}

function eta(hours: number | null): string {
  if (hours === null || hours === undefined) return '无法估算'
  if (hours < 1) return `约 ${Math.max(1, Math.round(hours * 60))} 分钟`
  if (hours < 48) return `约 ${hours.toFixed(1).replace(/\.0$/, '')} 小时`
  return `约 ${Math.round(hours / 24)} 天`
}

export function TagCoveragePanel() {
  const [report, setReport] = useState<TagCoverageReport | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [open, setOpen] = useState<TagCoverageCategory | null>(null)
  const [confirming, setConfirming] = useState<TagRequeueCategory | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (refresh = false, signal?: AbortSignal) => {
    try {
      setReport(await getTagCoverage({ refresh, signal }))
      setStatus('success')
    } catch (reason) {
      if (isRequestCancelled(reason)) return
      setError(reason)
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(false, controller.signal)
    return () => controller.abort()
  }, [load])

  async function requeue(category: TagRequeueCategory) {
    setBusy(true)
    try {
      const result = await requeueTagCategory(category)
      toast.success(`已重新排队 ${number.format(result.requeued)} 条，TagWorker 已开始处理`)
      setConfirming(null)
      await load(true)
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  async function wake() {
    setBusy(true)
    try {
      await wakeTagWorker()
      toast.success('已叫醒 TagWorker，稍后刷新查看进度')
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  const total = report?.active_memories ?? 0
  const worker = report?.worker ?? {}
  return (
    <Card className="border-border/60" data-panel="tag-coverage">
      <CardHeader className="gap-2 border-b pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-sm">标签覆盖</CardTitle>
            <CardDescription>只算活跃记忆（不含噪声、归档、隔离）。「应打已打」去掉了按规则不提取、模型判定无标签和不在提取范围的部分。</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={busy || !worker.running} onClick={() => void wake()}><PlayIcon aria-hidden="true" />立即处理一批</Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void load(true)}><RefreshCwIcon aria-hidden="true" />重新统计</Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 p-4">
        <QueryState status={status} error={error} title="覆盖率统计失败" onRetry={() => void load(true)}>
          {report ? (
            <>
              <section aria-label="覆盖率概览" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <Stat label="应打已打" value={pct(report.effective_coverage)} detail={`${number.format(report.counts.tagged)} / ${number.format(report.counts.tagged + report.counts.pending + report.counts.lost + report.counts.failed)} 条该有标签的记忆`} />
                <Stat label="整体覆盖" value={pct(report.coverage)} detail={`${number.format(report.counts.tagged)} / ${number.format(total)} 条活跃记忆`} />
                <Stat label="待处理" value={number.format(report.backlog)} detail={`按当前速度${eta(report.backlog_eta_hours)}处理完`} />
                <Stat label="近 24 小时处理" value={number.format(report.throughput.last_24h ?? 0)} detail={`近 7 天 ${number.format(report.throughput.last_7d ?? 0)} 条`} />
              </section>

              <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label={ORDER.map((c) => `${report.labels[c]} ${report.counts[c]}`).join('，')}>
                {ORDER.map((category) => {
                  const width = total ? (report.counts[category] / total) * 100 : 0
                  return width > 0 ? <div key={category} className={COLORS[category]} style={{ width: `${width}%` }} title={`${report.labels[category]}：${number.format(report.counts[category])}`} /> : null
                })}
              </div>

              <ul className="flex flex-col divide-y rounded-lg border" aria-label="覆盖率分类">
                {ORDER.map((category) => {
                  const count = report.counts[category]
                  const samples = report.samples[category] ?? []
                  const expanded = open === category
                  const retryable = report.requeueable.includes(category as TagRequeueCategory) && count > 0
                  return (
                    <li key={category} className="flex flex-col gap-2 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`size-2.5 shrink-0 rounded-full ${COLORS[category]}`} aria-hidden="true" />
                        <button
                          type="button"
                          className="flex items-center gap-1 text-sm font-medium disabled:cursor-default"
                          disabled={!samples.length}
                          aria-expanded={expanded}
                          onClick={() => setOpen(expanded ? null : category)}
                        >
                          {samples.length ? (expanded ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />) : null}
                          {report.labels[category]}
                        </button>
                        <span className="font-mono text-sm tabular-nums">{number.format(count)}</span>
                        <span className="text-xs text-muted-foreground">{total ? pct(count / total) : '0%'}</span>
                        {NEEDS_WORK.includes(category) && count > 0 ? <Badge variant="outline">需要处理</Badge> : null}
                        {category === 'failed' && report.failed_exhausted > 0 ? <Badge variant="secondary">{number.format(report.failed_exhausted)} 条已停止自动重试</Badge> : null}
                        <div className="ml-auto flex gap-2">
                          {retryable && confirming !== category ? (
                            <Button type="button" size="sm" variant={category === 'lost' ? 'default' : 'outline'} disabled={busy} onClick={() => setConfirming(category as TagRequeueCategory)}>
                              <RotateCcwIcon aria-hidden="true" />{category === 'lost' ? '重新提取' : '重试'}
                            </Button>
                          ) : null}
                          {confirming === category ? (
                            <>
                              <span className="self-center text-xs text-muted-foreground">将重新提取 {number.format(Math.min(count, 5000))} 条（消耗 LLM 调用）</span>
                              <Button type="button" size="sm" disabled={busy} onClick={() => void requeue(category as TagRequeueCategory)}>确认</Button>
                              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>取消</Button>
                            </>
                          ) : null}
                        </div>
                      </div>
                      <p className="pl-4 text-xs text-muted-foreground">{EXPLAIN[category]}{category === 'too_short' ? `（少于 ${report.min_content_length} 字）` : ''}</p>
                      {expanded ? (
                        <ul className="ml-4 flex flex-col gap-1 rounded-md bg-muted/40 p-2 text-xs" aria-label={`${report.labels[category]}样例`}>
                          {samples.map((sample) => (
                            <li key={sample.id} className="flex gap-2">
                              <span className="shrink-0 font-mono text-muted-foreground">#{sample.id}</span>
                              <span className="shrink-0 text-muted-foreground">{sample.bot_id || '旧行'}{sample.visibility === 'private' ? ' · 私聊' : ''}</span>
                              <span className="min-w-0 truncate">{sample.preview || '（空）'}</span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  )
                })}
              </ul>

              <div className="grid gap-3 lg:grid-cols-2">
                <div className="rounded-lg border p-3 text-xs">
                  <p className="mb-2 text-sm font-medium">TagWorker</p>
                  <p>
                    <Badge variant={worker.running ? 'secondary' : 'outline'}>{worker.running ? '运行中' : '未运行'}</Badge>
                    <span className="ml-2 text-muted-foreground">
                      每 {Math.round((worker.interval_seconds ?? 0) / 60)} 分钟最多 {worker.batch_size ?? '—'} 条（约 {number.format(report.worker_capacity_per_hour)} 条/小时）；上一轮 {ago(worker.last_cycle_at)}，取到 {worker.last_cycle_count ?? 0} 条
                    </span>
                  </p>
                  {!worker.running ? <p className="mt-2 text-muted-foreground">检查「标签提取」开关与 LLM Provider；也可以在「服务与扩展」页启动。</p> : null}
                </div>
                <div className="rounded-lg border p-3 text-xs">
                  <p className="mb-2 text-sm font-medium">按 Bot</p>
                  <table className="w-full tabular-nums">
                    <thead className="text-muted-foreground"><tr><th className="text-left font-normal">Bot</th><th className="text-right font-normal">已标</th><th className="text-right font-normal">待处理</th><th className="text-right font-normal">应打已打</th></tr></thead>
                    <tbody>
                      {Object.entries(report.by_bot).map(([bot, counts]) => {
                        const needs = counts.tagged + counts.pending + counts.lost + counts.failed
                        return (
                          <tr key={bot}>
                            <td className="font-mono">{bot}</td>
                            <td className="text-right">{number.format(counts.tagged)}</td>
                            <td className="text-right">{number.format(counts.pending + counts.lost + counts.failed)}</td>
                            <td className="text-right">{needs ? pct(counts.tagged / needs) : '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
              <p className="text-right text-xs text-muted-foreground">统计用时 {report.elapsed_ms} ms · {ago(report.generated_at)}（缓存 1 分钟）</p>
            </>
          ) : null}
        </QueryState>
      </CardContent>
    </Card>
  )
}

function Stat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="min-w-0 rounded-lg border bg-card p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 text-2xl font-semibold tracking-tight tabular-nums">{value}</p><p className="mt-1 text-xs text-muted-foreground">{detail}</p></div>
}

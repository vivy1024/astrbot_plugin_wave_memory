import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { RefreshCwIcon } from 'lucide-react'

import { useGlobalScope } from '@/app/global-scope'
import {
  isReplyLearningScope, listExpressionCandidates, listReplyFeedback, replyLearningErrorMessage, reviewExpression,
  type ExpressionCandidate, type ExpressionReviewAction, type ExpressionSituation, type ExpressionStatus, type ReplyLearningScope, type ReplyOutcome,
} from '@/api/replyLearning'
import type { ApiError } from '@/api/client'
import { PaginationControls, QueryState, type PageSize } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const SITUATIONS: Record<ExpressionSituation, string> = {
  acknowledge: '回应与致谢', clarify: '澄清疑问', encourage: '鼓励安慰',
  celebrate: '庆祝祝贺', boundary: '表达边界', humor: '轻松幽默',
}
const OUTCOMES: Record<ReplyOutcome, string> = { accepted: '明确承接', corrected: '被纠正', rejected: '被拒绝', unknown: '未知' }
type Notice = { text: string; error?: boolean }

function formatTime(seconds: number): string {
  const date = new Date(seconds * 1000)
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN')
}

// The caller memoizes load with scope and pagination; revision requests a refresh.
// A changed request hides the previous result in the very first render.
function useLearningPage<T>(load: (signal: AbortSignal) => Promise<T>, revision: number) {
  const [state, setState] = useState<{ load: typeof load; revision: number; data?: T; error?: string; loading: boolean }>({ load, revision, loading: true })
  useEffect(() => {
    const controller = new AbortController()
    setState({ load, revision, loading: true })
    void load(controller.signal).then(
      (data) => { if (!controller.signal.aborted) setState({ load, revision, data, loading: false }) },
      (reason) => { if (!controller.signal.aborted) setState({ load, revision, error: replyLearningErrorMessage(reason, '读取失败，请稍后重试。'), loading: false }) },
    )
    return () => controller.abort()
  }, [load, revision])
  return state.load === load && state.revision === revision ? state : { loading: true, data: undefined, error: undefined }
}

function newReviewKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function ExpressionCard({ item, scope, onChanged }: { item: ExpressionCandidate; scope: ReplyLearningScope; onChanged: (notice: Notice) => void }) {
  const checkboxId = useId()
  const [styleOnly, setStyleOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submitting = useRef(false)
  const mounted = useRef(true)
  const keys = useRef<Partial<Record<ExpressionReviewAction, string>>>({})
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const length = [...item.content].length
  const validLength = length > 0 && length <= 20
  const reviewable = item.status !== 'rejected' && Number.isSafeInteger(item.revision) && item.revision > 0
    && item.bot_id === scope.bot_id && item.session_id === scope.session_id && item.visibility === scope.visibility

  async function review(action: ExpressionReviewAction) {
    if (submitting.current || !reviewable || (action === 'approve' && (item.status !== 'pending' || !styleOnly || !validLength || !item.evidence_available))) return
    submitting.current = true
    setBusy(true)
    setError('')
    try {
      // Preserve the key when retrying an ambiguous network failure on this revision.
      keys.current[action] ??= newReviewKey()
      const result = await reviewExpression(item, scope, action, { idempotencyKey: keys.current[action], styleOnly: action === 'approve' })
      if (!mounted.current) return
      if (!result.ok) throw new Error('审核未完成，请刷新列表核对状态。')
      const labels = { approved: '已批准', rejected: '已拒绝', pending: '待审核' }
      onChanged({ text: `表达 #${item.id} 当前为${labels[result.item.status]}，已刷新列表。${result.item.status === 'approved' ? '表达学习开启且来源证据有效时，可按适用情景召回。' : ''}` })
    } catch (reason) {
      if (!mounted.current) return
      const text = replyLearningErrorMessage(reason, '审核失败，请重试或刷新列表核对状态。')
      const status = (reason as ApiError)?.status
      if (status === 409 || status === 404 || status === 422) onChanged({ text, error: true })
      else setError(text)
    } finally {
      submitting.current = false
      if (mounted.current) setBusy(false)
    }
  }

  return <article className="flex min-w-0 flex-col gap-3 rounded-lg border p-4" aria-label={`表达 ${item.id}`}>
    <div className="flex flex-wrap items-center justify-between gap-2"><Badge variant="secondary">{SITUATIONS[item.situation] ?? '情景未记录'}</Badge><span className="text-xs text-muted-foreground">{length}/20 字</span></div>
    <p className="whitespace-pre-wrap break-words text-base font-medium">{item.content}</p>
    <dl className="grid gap-1 text-xs text-muted-foreground">
      <div><dt className="inline">来源：</dt><dd className="inline break-all">群友 {item.speaker_id} · 记忆 #{item.source_memory_id}</dd></div>
      <div><dt className="inline">出现：</dt><dd className="inline">{item.occurrence_count} 次 · {item.speaker_count} 位群友说过</dd></div>
      <div><dt className="inline">收录时间：</dt><dd className="inline">{formatTime(item.created_at)}</dd></div>
    </dl>
    {!item.evidence_available ? <p className="text-xs text-amber-600">来源证据不可用，不能批准或召回；仍可拒绝或撤销批准。</p> : null}
    {!validLength ? <p className="text-xs text-amber-600">表达长度不符合 1–20 字要求，暂不能批准。</p> : null}
    {item.status === 'pending' ? <label htmlFor={checkboxId} className="flex items-start gap-2 text-xs leading-relaxed">
      <input id={checkboxId} type="checkbox" className="mt-0.5" checked={styleOnly} onChange={(event) => setStyleOnly(event.target.checked)} disabled={busy || !reviewable || !validLength || !item.evidence_available} />
      确认只学习表达方式，不学习说话人的身份或经历
    </label> : <Badge variant="outline">{item.status === 'approved' ? '已批准' : '已拒绝'}</Badge>}
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    <div className="flex gap-2">
      {item.status === 'pending' ? <Button size="sm" disabled={busy || !reviewable || !styleOnly || !validLength || !item.evidence_available} onClick={() => void review('approve')}>批准表达</Button> : null}
      {item.status !== 'rejected' ? <Button size="sm" variant="outline" disabled={busy || !reviewable} onClick={() => void review('reject')}>{item.status === 'approved' ? '撤销批准' : '拒绝表达'}</Button> : null}
      {busy ? <span role="status" className="self-center text-xs text-muted-foreground">提交中…</span> : null}
    </div>
  </article>
}

function Expressions({ scope }: { scope: ReplyLearningScope }) {
  const [status, setStatus] = useState<ExpressionStatus>('pending')
  // 默认列出全部待审：门槛只是「先看哪些」，勾选后才隐藏未达标的候选。
  // 服务端已按出现次数倒序，达标的那几条本来就在第一页最前面。
  const [qualifiedOnly, setQualifiedOnly] = useState(false)
  const [limit, setLimit] = useState<PageSize>(25)
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const [notice, setNotice] = useState<Notice | null>(null)
  const { bot_id, session_id, visibility } = scope
  const load = useCallback((signal: AbortSignal) => listExpressionCandidates({ bot_id, session_id, visibility, status, qualified: qualifiedOnly, limit, offset }, signal), [bot_id, session_id, visibility, status, qualifiedOnly, limit, offset])
  const { data, error, loading } = useLearningPage(load, reload)
  const refresh = () => setReload((value) => value + 1)

  return <Card aria-label="群友表达审核">
    <CardHeader className="flex flex-wrap items-start justify-between gap-3">
      <div className="space-y-1"><CardTitle>群友表达审核</CardTitle><CardDescription>本群群友的简短表达，每条不超过 20 字；批准后才可在适用情景中学习使用。</CardDescription></div>
      <Button size="sm" variant="outline" disabled={loading} onClick={refresh}><RefreshCwIcon aria-hidden="true" />刷新表达</Button>
    </CardHeader>
    <CardContent className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm">表达状态<select aria-label="表达状态" className="rounded-md border bg-background p-2" value={status} onChange={(event) => { setStatus(event.target.value as ExpressionStatus); setOffset(0); setNotice(null) }}><option value="pending">待审核</option><option value="approved">已批准</option><option value="rejected">已拒绝</option></select></label>
        <label className="flex items-center gap-2 text-sm">
          <input aria-label="只看达标的表达" type="checkbox" checked={qualifiedOnly} onChange={(event) => { setQualifiedOnly(event.target.checked); setOffset(0); setNotice(null) }} />
          只看出现 ≥2 次或跨 ≥2 人的表达
        </label>
      </div>
      {data ? <p className="text-sm text-muted-foreground">{data.enabled ? '表达学习已开启。已批准且来源证据有效的表达可按适用情景召回；批准不保证每次使用。' : '表达学习已关闭。已有候选仍可审核，启用后才会学习使用。'}</p> : null}
      {notice ? <p role={notice.error ? 'alert' : 'status'} className={notice.error ? 'text-sm text-destructive' : 'text-sm text-muted-foreground'}>{notice.text}</p> : null}
      <QueryState status={loading ? 'loading' : error ? 'error' : data?.items.length ? 'success' : 'empty'} error={error} title="表达读取失败" emptyTitle={qualifiedOnly && status === 'pending' ? '暂无达标的待审表达' : `当前群暂无${status === 'pending' ? '待审' : status === 'approved' ? '已批准' : '已拒绝'}表达`} emptyDescription={qualifiedOnly && status === 'pending' ? '出现两次以上或跨两位群友说过的表达，才会进入这里。取消勾选可查看全部待审候选。' : status === 'pending' ? '有符合要求的群友表达被收录后，会在这里等待人工审核。' : '可切换表达状态查看其他记录。'} onRetry={refresh}>
        <div className="grid gap-3 lg:grid-cols-2">{data?.items.map((item) => <ExpressionCard key={`${item.id}:${item.revision}`} item={item} scope={scope} onChanged={(next) => { setNotice(next); setOffset(0); refresh() }} />)}</div>
      </QueryState>
      {data ? <PaginationControls page={data.page} label="待审表达分页" onOffsetChange={setOffset} onLimitChange={(value) => { setLimit(value); setOffset(0) }} /> : null}
    </CardContent>
  </Card>
}


function Feedback({ scope }: { scope: ReplyLearningScope }) {
  const [limit, setLimit] = useState<PageSize>(25)
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const { bot_id, session_id, visibility } = scope
  const load = useCallback((signal: AbortSignal) => listReplyFeedback({ bot_id, session_id, visibility, limit, offset }, signal), [bot_id, session_id, visibility, limit, offset])
  const { data, error, loading } = useLearningPage(load, reload)
  const refresh = () => setReload((value) => value + 1)

  return <Card aria-label="近期回复反馈">
    <CardHeader className="flex flex-wrap items-start justify-between gap-3">
      <div className="space-y-1"><CardTitle>近期回复反馈</CardTitle><CardDescription>无回应、沉默、延迟或含糊反应均为未知，不扣分，也不推断为被讨厌。</CardDescription></div>
      <Button size="sm" variant="outline" disabled={loading} onClick={refresh}><RefreshCwIcon aria-hidden="true" />刷新回复反馈</Button>
    </CardHeader>
    <CardContent className="flex flex-col gap-4">
      {data ? <p className="text-sm text-muted-foreground">{data.enabled ? '回复反馈采集已开启；无明确回应仍记为未知。' : '回复反馈采集已关闭，已有记录仍可查看。'}</p> : null}
      <QueryState status={loading ? 'loading' : error ? 'error' : data?.items.length ? 'success' : 'empty'} error={error} title="回复反馈读取失败" emptyTitle="当前群暂无回复反馈" emptyDescription="没有反馈记录不代表回复效果不好。" onRetry={refresh}>
        <div className="flex flex-col gap-3">{data?.items.map((item) => <article key={item.id} className="space-y-2 rounded-lg border p-3" aria-label={`回复反馈 ${item.id}`}>
          <div className="flex flex-wrap items-center gap-2"><Badge variant={item.outcome === 'unknown' ? 'outline' : 'secondary'}>{OUTCOMES[item.outcome] ?? '未知'}</Badge><span className="text-sm">Bot 回复 #{item.reply_memory_id}</span>{item.situation ? <Badge variant="outline">{SITUATIONS[item.situation] ?? '情景未记录'}</Badge> : null}</div>
          {item.source_quote ? <blockquote className="whitespace-pre-wrap break-words border-l-2 pl-3 text-sm">{item.source_quote}</blockquote> : <p className="text-sm text-muted-foreground">没有可引用的回应原话，效果未知。</p>}
          <p className="break-all text-xs text-muted-foreground">{item.response_memory_id ? `回应记忆 #${item.response_memory_id} · 群友 ${item.speaker_id}` : '未关联回应消息'} · {formatTime(item.created_at)}</p>
          {!item.evidence_available ? <p className="text-xs text-amber-600">来源证据目前不可用，此记录不代表已确认的回复效果。</p> : null}
        </article>)}</div>
      </QueryState>
      {data ? <PaginationControls page={data.page} label="回复反馈分页" onOffsetChange={setOffset} onLimitChange={(value) => { setLimit(value); setOffset(0) }} /> : null}
    </CardContent>
  </Card>
}

export function ReplyLearningPanel() {
  const global = useGlobalScope()
  const scope = { bot_id: global.botId, session_id: global.sessionId, visibility: global.visibility }
  if (global.status !== 'ready') return <QueryState status={global.status === 'error' ? 'error' : 'loading'} title="表达学习作用域" error={global.optionsError} onRetry={global.reloadOptions} />
  if (!isReplyLearningScope(scope)) return <QueryState status="unknown" title="表达学习与回复反馈" description="请选择有效的 Bot 和群后查看表达学习与回复反馈。" />
  const key = JSON.stringify([scope.bot_id, scope.session_id, scope.visibility])
  return <div className="flex flex-col gap-4" key={key}><Expressions scope={scope} /><Feedback scope={scope} /></div>
}

import { useEffect, useState } from 'react'
import { ActivityIcon, ExternalLinkIcon, Loader2Icon, MessagesSquareIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import { isRequestCancelled } from '@/api/client'
import { listMemoryTraces, type MemoryTraceUsage } from '@/api/injection'
import { getMemoryContext, type MemoryContextMessage, type MemoryItem } from '@/api/memories'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { sanitizeDisplayName } from '@/lib/display-name'
import { humanizeApiError } from '@/lib/reason-label'
import { observatoryTraceHref } from '@/lib/trace-links'
import { cn } from '@/lib/utils'

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function formatScore(value: unknown): string {
  const score = Number(value)
  return value === null || value === undefined || !Number.isFinite(score) ? '—' : score.toFixed(3)
}

type LoadState<T> = { status: 'loading' } | { status: 'ready'; data: T } | { status: 'error'; message: string }

/** 「最近被这些回复用到」：列出最近把这条记忆作为命中条目注入的 trace，点击回到观测台详情。 */
export function MemoryTraceUsageSection({ memoryId, botId, limit = 10 }: { memoryId: number; botId: string; limit?: number }) {
  const [state, setState] = useState<LoadState<MemoryTraceUsage[]>>({ status: 'loading' })

  useEffect(() => {
    if (!botId) {
      setState({ status: 'error', message: '缺少 Bot，无法按 Bot 查询注入记录' })
      return
    }
    const controller = new AbortController()
    setState({ status: 'loading' })
    listMemoryTraces(memoryId, botId, limit, controller.signal)
      .then((payload) => setState({ status: 'ready', data: payload.items ?? [] }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted || isRequestCancelled(reason)) return
        setState({ status: 'error', message: humanizeApiError(reason, '注入记录加载失败') })
      })
    return () => controller.abort()
  }, [botId, limit, memoryId])

  return (
    <Card data-slot="memory-trace-usage" className="border-border/60">
      <CardHeader className="py-3">
        <CardTitle className="flex items-center gap-2 text-sm"><ActivityIcon className="size-4" aria-hidden="true" />最近被这些回复用到</CardTitle>
        <CardDescription>来自注入观测台：只统计 memory / fts5 通道真正注入的条目（不含被过滤的），只看当前 Bot。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 pt-0">
        {state.status === 'loading' ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="animate-spin" />正在查询注入记录</div>
        ) : state.status === 'error' ? (
          <p className="text-xs text-destructive">{state.message}</p>
        ) : state.data.length ? (
          <ul className="flex flex-col gap-2" aria-label="用到这条记忆的回复">
            {state.data.map((usage) => (
              <li key={usage.trace_id}>
                <Link
                  to={observatoryTraceHref(usage.trace_id, usage.bot_profile_id || botId)}
                  data-slot="memory-trace-link"
                  className="block rounded-lg border bg-background/50 p-2.5 text-xs transition-colors hover:border-primary/40 hover:bg-primary/5"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-muted-foreground">
                    <span>{formatTime(usage.timestamp)}</span>
                    <span className="flex items-center gap-1 text-primary">查看注入详情<ExternalLinkIcon className="size-3" aria-hidden="true" /></span>
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Badge variant="outline" className="font-mono">群 {usage.group_id || '私聊'}</Badge>
                    {(usage.channels?.length ? usage.channels : [{ channel: usage.channel, score: usage.score }]).map((hit) => (
                      <Badge key={hit.channel} variant="secondary" className="font-mono">{hit.channel} · 得分 {formatScore(hit.score)}{'rank' in hit && hit.rank ? ` · 第 ${hit.rank} 条` : ''}</Badge>
                    ))}
                  </div>
                  <p className="mt-1.5 line-clamp-2 leading-relaxed">{usage.sender_name ? <span className="text-muted-foreground">{sanitizeDisplayName(usage.sender_name)}：</span> : null}{usage.message_preview || '（未记录消息预览）'}</p>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="py-3 text-center text-xs text-muted-foreground">观测台保留期内没有回复用到这条记忆。</p>
        )}
      </CardContent>
    </Card>
  )
}

/** 同 Bot、同群里这条记忆前后的消息，用来还原它当时的对话。 */
export function MemoryContextSection({ memory, before = 5, after = 5 }: { memory: Pick<MemoryItem, 'id' | 'mutation_url'>; before?: number; after?: number }) {
  const [state, setState] = useState<LoadState<MemoryContextMessage[]>>({ status: 'loading' })
  const mutationUrl = memory.mutation_url

  useEffect(() => {
    if (!mutationUrl) {
      setState({ status: 'error', message: '缺少服务端签发的记忆引用，无法读取上下文' })
      return
    }
    const controller = new AbortController()
    setState({ status: 'loading' })
    getMemoryContext({ mutation_url: mutationUrl }, before, after, controller.signal)
      .then((payload) => setState({ status: 'ready', data: payload.messages ?? [] }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted || isRequestCancelled(reason)) return
        setState({ status: 'error', message: humanizeApiError(reason, '上下文加载失败') })
      })
    return () => controller.abort()
  }, [after, before, mutationUrl])

  return (
    <Card data-slot="memory-context" className="border-border/60">
      <CardHeader className="py-3">
        <CardTitle className="flex items-center gap-2 text-sm"><MessagesSquareIcon className="size-4" aria-hidden="true" />对话上下文</CardTitle>
        <CardDescription>同一 Bot、同一群里紧挨着这条记忆的前后消息，只读展示。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5 pt-0">
        {state.status === 'loading' ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="animate-spin" />正在读取上下文</div>
        ) : state.status === 'error' ? (
          <p className="text-xs text-destructive">{state.message}</p>
        ) : state.data.length ? state.data.map((message) => (
          <div
            key={message.id}
            data-role={message.role}
            className={cn('rounded-md border px-2.5 py-1.5 text-xs', message.role === 'anchor' ? 'border-primary/40 bg-primary/10' : 'bg-background/50')}
          >
            <div className="flex justify-between gap-2 font-mono text-muted-foreground">
              <span>{sanitizeDisplayName(message.sender_name) || message.sender_id || '未记录'}{message.role === 'anchor' ? ' · 本条' : ''}</span>
              <span>#{message.id} · {formatTime(message.timestamp)}</span>
            </div>
            <p className={cn('mt-0.5 whitespace-pre-wrap break-words leading-relaxed', message.role === 'anchor' ? '' : 'line-clamp-3')} title={message.role === 'anchor' ? undefined : message.content}>{message.content}</p>
          </div>
        )) : <p className="py-3 text-center text-xs text-muted-foreground">没有可展示的上下文。</p>}
      </CardContent>
    </Card>
  )
}

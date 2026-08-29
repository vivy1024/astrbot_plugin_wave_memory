import { AlertTriangleIcon, ArrowRightIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import type { TraceDetailPayload } from '@/api/injection'
import { ObjectDeepLink, TracePayloadViewer, type ObjectRefDescriptor } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function asRecords(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.map((item) => asRecord(item)).filter((item): item is Record<string, unknown> => item !== null)
    : []
}

function textValue(value: unknown, fallback = '未记录'): string {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.length ? value.map((item) => typeof item === 'object' ? compactObject(item) : String(item)).join('、') : fallback
  if (typeof value === 'object') return compactObject(value)
  return String(value)
}

function compactObject(value: unknown): string {
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function channelName(channel: Record<string, unknown>): string {
  return textValue(channel.channel ?? channel.name ?? channel.key, 'unknown')
}

function channelStatusLabel(status: unknown): string {
  const value = String(status ?? '')
  const labels: Record<string, string> = {
    ok: '正常',
    error: '错误',
    timeout: '超时',
    empty: '无命中',
    disabled: '已关闭',
    skipped: '已跳过',
    filtered: '已过滤',
  }
  return labels[value] ?? (value || '未知')
}

function channelVariant(status: unknown): 'secondary' | 'destructive' | 'outline' {
  const value = String(status ?? '')
  if (value.includes('error') || value.includes('timeout')) return 'destructive'
  if (value === 'ok') return 'secondary'
  return 'outline'
}

function detailHits(detail: TraceDetailPayload): Array<Record<string, unknown>> {
  const direct = asRecords(detail.hits)
  const nested = asRecords(detail.channels).flatMap((channel) => asRecords(channel.hit_items).map((item) => ({ ...item, channel_name: channelName(channel) })))
  return nested.length ? nested : direct
}

function detailFiltered(detail: TraceDetailPayload): Array<Record<string, unknown>> {
  const direct = asRecords(detail.filtered ?? detail.filtered_items)
  const nested = asRecords(detail.channels).flatMap((channel) => asRecords(channel.filtered_items).map((item) => ({ ...item, channel_name: channelName(channel) })))
  return [...direct, ...nested]
}

function normalizeMessages(value: unknown): string[] {
  if (value === undefined || value === null || value === '') return []
  if (Array.isArray(value)) return value.flatMap(normalizeMessages)
  const record = asRecord(value)
  if (record) return [textValue(record.message ?? record.error ?? record.code ?? record)]
  return [String(value)]
}

function objectLinks(detail: TraceDetailPayload): Array<{ label: string; path: string; ref: ObjectRefDescriptor }> {
  return asRecords(detail.object_refs).flatMap((item) => {
    const nestedRef = asRecord(item.object_ref)
    const ref = typeof item.ref === 'string' ? item.ref : typeof nestedRef?.ref === 'string' ? nestedRef.ref : ''
    const path = typeof item.path === 'string' ? item.path : ''
    if (!ref || !path) return []
    return [{
      label: textValue(item.label ?? item.kind, '打开命中对象'),
      path,
      ref: {
        ref,
        kind: textValue(item.kind ?? nestedRef?.kind, ''),
        scope_key: typeof item.scope_key === 'string' ? item.scope_key : typeof nestedRef?.scope_key === 'string' ? nestedRef.scope_key : undefined,
        version: typeof item.version === 'number' ? item.version : typeof nestedRef?.version === 'number' ? nestedRef.version : undefined,
      },
    }]
  })
}

function DetailGrid({ value, preferredKeys, omitKeys = [] }: { value: unknown; preferredKeys?: string[]; omitKeys?: string[] }) {
  const record = asRecord(value)
  if (!record || Object.keys(record).length === 0) return <p className="text-sm text-muted-foreground">未记录。</p>
  const visibleKeys = Object.keys(record).filter((key) => !omitKeys.includes(key))
  const keys = preferredKeys?.length
    ? [...preferredKeys.filter((key) => key in record && !omitKeys.includes(key)), ...visibleKeys.filter((key) => !preferredKeys.includes(key))]
    : visibleKeys
  return (
    <dl className="grid gap-3 text-sm sm:grid-cols-2">
      {keys.map((key) => (
        <div key={key} className="min-w-0 rounded-md border bg-muted/20 p-3">
          <dt className="text-xs font-medium text-muted-foreground">{key}</dt>
          <dd className="mt-1 whitespace-pre-wrap break-words">{textValue(record[key])}</dd>
        </div>
      ))}
    </dl>
  )
}

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="gap-1">
        <CardTitle className="text-sm font-semibold">{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function ItemCards({ items, kind }: { items: Array<Record<string, unknown>>; kind: 'hit' | 'filtered' }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">暂无{kind === 'hit' ? '命中' : '过滤'}项。</p>
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {items.map((item, index) => {
        const channel = textValue(item.channel_name ?? item.channel ?? item.source_channel ?? item.type, '未记录通道')
        const title = textValue(item.title ?? item.name ?? item.word ?? item.content ?? item.preview, `${kind === 'hit' ? '命中' : '过滤'}项 ${index + 1}`)
        const reason = item.reason ?? item.filter_reason ?? item.skip_reason
        return (
          <div key={`${channel}-${String(item.id ?? index)}-${index}`} className="min-w-0 rounded-lg border bg-card p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{channel}</Badge>
              {reason ? <Badge variant="outline">{textValue(reason)}</Badge> : null}
            </div>
            <p className="mt-2 whitespace-pre-wrap break-words text-sm">{title}</p>
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
              {['id', 'score', 'tokens', 'source', 'scope', 'matched_by'].filter((key) => item[key] !== undefined).map((key) => <div key={key}><dt className="inline text-muted-foreground">{key}：</dt><dd className="inline break-words">{textValue(item[key])}</dd></div>)}
            </dl>
          </div>
        )
      })}
    </div>
  )
}

export function TraceDetailSheet({
  open,
  onOpenChange,
  detail,
  loading,
  error,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  detail?: TraceDetailPayload | null
  loading: boolean
  error: string
}) {
  const channels = detail ? asRecords(detail.channels) : []
  const hits = detail ? detailHits(detail) : []
  const filtered = detail ? detailFiltered(detail) : []
  const links = detail ? objectLinks(detail) : []
  const warnings = detail ? normalizeMessages(detail.warnings) : []
  const errors = detail ? [detail.error, detail.errors, ...channels.filter((channel) => channel.error).map((channel) => `${channelName(channel)}: ${textValue(channel.error)}`)].flatMap(normalizeMessages) : []
  const finalText = textValue(detail?.final_text ?? detail?.final_injection_text, '')
  const rawPayload = typeof detail?.raw_payload === 'string' ? detail.raw_payload : undefined

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent data-slot="trace-detail-sheet" className="flex w-full flex-col gap-0 pr-0 sm:max-w-4xl sm:pr-2">
        <SheetHeader className="shrink-0 border-b pb-4 pr-6">
          <SheetTitle>注入详情</SheetTitle>
          <SheetDescription>{detail?.trace_id ? `记录编号：${detail.trace_id}` : '按需读取请求、预算、通道、命中、过滤、错误与最终文本。'}</SheetDescription>
        </SheetHeader>
        <ScrollArea className="flex-1 pr-6">
          <div className="flex flex-col gap-4 py-6">
            {loading ? (
              <><Skeleton className="h-28 w-full" /><Skeleton className="h-64 w-full" /><Skeleton className="h-48 w-full" /></>
            ) : error ? (
              <Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>详情加载失败</AlertTitle><AlertDescription>{error}。引用不存在、不属于当前群或版本失效时，不会改用裸编号或默认 Bot 猜测。</AlertDescription></Alert>
            ) : detail ? (
              <>
                <Alert><AlertTitle>观测与解释边界</AlertTitle><AlertDescription>以下分区用来解释这次回复用了哪些通道；对象跳转只打开服务端给出的本群链接。完整载荷仅作为末尾的辅助核对内容。</AlertDescription></Alert>

                <Section title="请求上下文" description="只展示这次注入实际记录的请求与当前群；缺失字段保持未记录。">
                  <DetailGrid value={detail.request ?? detail.context} preferredKeys={['bot_profile_id', 'bot_id', 'session_id', 'sender_id', 'scope', 'chat_type', 'message']} omitKeys={['group_id']} />
                </Section>

                <Section title="预算" description="Token、字符、通道预算和截断信息均来自详情载荷。">
                  <DetailGrid value={detail.budget} preferredKeys={['total_tokens', 'token_budget', 'used_tokens', 'remaining_tokens', 'max_chars', 'truncated']} />
                </Section>

                <Section title="通道瀑布" description="按通道查看状态、耗时、Token、命中/过滤数量和错误。">
                  {channels.length ? <div className="flex flex-col gap-3">{channels.map((channel, index) => {
                    const name = channelName(channel)
                    const status = channel.status
                    return <div key={`${name}-${index}`} className="rounded-lg border bg-card p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><span className="font-semibold">{name}</span><Badge variant={channelVariant(status)}>{channelStatusLabel(status)}</Badge></div><span className="text-xs text-muted-foreground">{textValue(channel.latency_ms ?? channel.duration_ms)} ms · {textValue(channel.tokens ?? channel.token_count)} tokens</span></div><dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3"><div><dt className="text-muted-foreground">命中</dt><dd>{textValue(channel.hit_count ?? channel.hits_count ?? asRecords(channel.hit_items).length, '0')}</dd></div><div><dt className="text-muted-foreground">过滤</dt><dd>{textValue(channel.filtered_count ?? asRecords(channel.filtered_items).length, '0')}</dd></div><div><dt className="text-muted-foreground">错误</dt><dd className={channel.error ? 'text-destructive' : ''}>{textValue(channel.error, '无')}</dd></div></dl></div>
                  })}</div> : <p className="text-sm text-muted-foreground">暂无通道明细。</p>}
                </Section>

                <Section title="命中项" description="展示可解释字段；正式对象入口另由服务端给出的跳转链接提供。"><ItemCards items={hits} kind="hit" /></Section>
                {links.length ? <Section title="正式对象入口" description="只跟随服务端给出的跳转链接，不会用裸编号拼路由。"><div className="flex flex-wrap gap-2">{links.map((item) => <ObjectDeepLink key={`${item.path}:${item.ref.ref}`} to={item.path} objectRef={item.ref}>{item.label}</ObjectDeepLink>)}</div></Section> : null}
                <Section title="过滤项" description="展示过滤通道、原因和可用预览，不补造缺失原因。"><ItemCards items={filtered} kind="filtered" /></Section>

                <Section title="最终注入文本">
                  {finalText ? <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-muted/30 p-3 text-sm leading-relaxed">{finalText}</pre> : <p className="text-sm text-muted-foreground">暂无最终文本。</p>}
                </Section>

                <Section title="错误与警告">
                  {!errors.length && !warnings.length ? <p className="text-sm text-muted-foreground">未记录错误或警告。</p> : <div className="grid gap-3 md:grid-cols-2"><div><p className="mb-2 text-sm font-medium">错误</p>{errors.length ? <ul className="list-disc space-y-1 pl-5 text-sm text-destructive">{errors.map((message, index) => <li key={`${message}-${index}`}>{message}</li>)}</ul> : <p className="text-sm text-muted-foreground">无</p>}</div><div><p className="mb-2 text-sm font-medium">警告</p>{warnings.length ? <ul className="list-disc space-y-1 pl-5 text-sm">{warnings.map((message, index) => <li key={`${message}-${index}`}>{message}</li>)}</ul> : <p className="text-sm text-muted-foreground">无</p>}</div></div>}
                </Section>

                <Section title="后续处理" description="通道参数在通道配置中管理。">
                  <div className="flex flex-wrap gap-2"><Button asChild variant="outline" size="sm"><Link to="/channels">调整通道配置<ArrowRightIcon data-icon="inline-end" aria-hidden="true" /></Link></Button></div>
                </Section>

                <Section title="完整载荷（辅助核对）" description="结构化分区是主要阅读入口；复制与下载仍保留服务端完整内容。">
                  <TracePayloadViewer payload={detail} rawPayload={rawPayload} downloadName={`trace-${detail.trace_id ?? 'unknown'}.json`} maxHeightClassName="h-80" />
                </Section>
              </>
            ) : <p className="py-6 text-center text-sm text-muted-foreground">请选择一条注入记录。</p>}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}

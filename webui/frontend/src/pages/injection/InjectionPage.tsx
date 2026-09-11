import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCwIcon, SearchIcon } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'

import { isRequestCancelled } from '@/api/client'
import { humanizeApiError, humanizeReason } from '@/lib/reason-label'
import { getInjectionTrace, listInjectionTraces, type InjectionTraceSummary, type TraceDetailPayload, type TraceFilters } from '@/api/injection'
import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import { PaginationControls, QueryState, ScopeSelect, DeclarativeDataTable } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import { TraceDetailSheet } from '@/pages/injection/TraceDetailSheet'

function formatTime(value: unknown): string {
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toLocaleString('zh-CN') : '未记录'
}

function textValue(value: unknown, fallback = '未记录'): string {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

function traceBot(trace: InjectionTraceSummary): string {
  return textValue(trace.bot_profile_id ?? trace.bot_id)
}

function traceSession(trace: InjectionTraceSummary): { primary: string; secondary?: string } {
  if (trace.session) {
    return {
      primary: trace.session.label || trace.session.id,
      secondary: `${trace.session.kind} · ${trace.session.id}`,
    }
  }
  if (trace.session_id) return { primary: String(trace.session_id), secondary: '仅记录 session_id' }
  return { primary: '未记录', secondary: '服务端未返回结构化 session' }
}

function tracePreview(trace: InjectionTraceSummary): string {
  return textValue(trace.preview ?? trace.final_text_preview ?? trace.message_preview)
}

function channelRecords(trace: InjectionTraceSummary): Array<Record<string, unknown>> {
  const raw = trace.channels ?? trace.channel_results ?? trace.channel_summaries ?? []
  return Array.isArray(raw)
    ? raw.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : []
}

function channelCount(trace: InjectionTraceSummary, kind: 'hit' | 'skipped' | 'error'): string {
  const explicitKeys = kind === 'hit'
    ? ['hit_channel_count', 'hit_channels_count', 'matched_channel_count']
    : kind === 'skipped'
      ? ['skipped_channel_count', 'skipped_channels_count', 'filtered_channel_count']
      : ['error_channel_count', 'error_channels_count']
  const explicit = explicitKeys.map((key) => trace[key]).find((value) => value !== undefined && value !== null)
  if (explicit !== undefined) return String(explicit)

  const count = channelRecords(trace).filter((channel) => {
    const status = String(channel.status ?? '')
    if (kind === 'hit') return status === 'ok' || Number(channel.hit_count ?? channel.hits_count ?? 0) > 0
    if (kind === 'skipped') return ['empty', 'disabled', 'skipped', 'filtered'].includes(status)
    return status.includes('error') || status.includes('timeout') || Boolean(channel.error)
  }).length
  if (count > 0) return String(count)
  return kind === 'error' && trace.has_error ? '1' : '0'
}

function primaryTokenChannel(trace: InjectionTraceSummary): string {
  const explicit = trace.primary_token_channel ?? trace.max_token_channel ?? trace.top_token_channel
  if (explicit !== undefined && explicit !== null && explicit !== '') return String(explicit)
  const top = channelRecords(trace).reduce<Record<string, unknown> | null>((current, channel) => {
    const currentTokens = Number(current?.tokens ?? current?.token_count ?? 0)
    const nextTokens = Number(channel.tokens ?? channel.token_count ?? 0)
    return nextTokens > currentTokens ? channel : current
  }, null)
  return top ? textValue(top.channel ?? top.name ?? top.key) : '未记录'
}

function traceLatency(trace: InjectionTraceSummary): string {
  const value = Number(trace.latency_ms ?? trace.total_latency_ms ?? trace.total_ms)
  return Number.isFinite(value) && value >= 0 ? `${Math.round(value)} ms` : '未记录'
}

function statusLabel(status: unknown): string {
  const value = String(status ?? '')
  if (value === 'ok') return '正常'
  if (value === 'error') return '错误'
  if (value === 'timeout') return '超时'
  if (value === 'skipped') return '已跳过'
  return value || '未知'
}

function epochToInput(value: string | null): string {
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const date = new Date(seconds * 1000)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function inputToEpoch(value: string): string {
  if (!value) return ''
  const milliseconds = new Date(value).getTime()
  return Number.isFinite(milliseconds) ? String(Math.floor(milliseconds / 1000)) : ''
}

type TraceFilterDraft = {
  channel: string
  sender_id: string
  status: string
  has_error: string
  scope: string
  from_ts: string
  to_ts: string
  config_revision: string
}

const privateFilterKeys: Array<keyof TraceFilterDraft> = ['channel', 'sender_id', 'status', 'has_error', 'scope', 'from_ts', 'to_ts', 'config_revision']

function draftFromSearchParams(params: URLSearchParams): TraceFilterDraft {
  return {
    channel: params.get('channel') ?? '',
    sender_id: params.get('sender_id') ?? '',
    status: params.get('status') ?? '',
    has_error: params.get('has_error') ?? '',
    scope: params.get('scope') ?? '',
    from_ts: params.get('from_ts') ?? '',
    to_ts: params.get('to_ts') ?? '',
    config_revision: params.get('config_revision') ?? '',
  }
}

const emptyDraft: TraceFilterDraft = draftFromSearchParams(new URLSearchParams())

function draftFromSerialized(value: string): TraceFilterDraft {
  const values = value.split('\u0000')
  return Object.fromEntries(privateFilterKeys.map((key, index) => [key, values[index] ?? ''])) as unknown as TraceFilterDraft
}

export function InjectionPage() {
  const pagination = usePaginationSearchParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const [payload, setPayload] = useState<Awaited<ReturnType<typeof listInjectionTraces>> | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [detail, setDetail] = useState<TraceDetailPayload | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [filterDraft, setFilterDraft] = useState<TraceFilterDraft>(() => draftFromSearchParams(searchParams))
  const traceRequestRef = useRef<{ id: number; controller: AbortController } | null>(null)

  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const channel = searchParams.get('channel') ?? ''
  const selectedTraceId = searchParams.get('trace_id') ?? ''

  const filters = useMemo<TraceFilters>(() => ({
    bot_id: botId || undefined,
    session_id: sessionId || undefined,
    sender_id: searchParams.get('sender_id') || undefined,
    channel: channel || undefined,
    status: searchParams.get('status') || undefined,
    has_error: searchParams.get('has_error') || undefined,
    scope: searchParams.get('scope') || undefined,
    config_revision: searchParams.get('config_revision') || undefined,
    from_ts: searchParams.get('from_ts') || undefined,
    to_ts: searchParams.get('to_ts') || undefined,
    limit: pagination.limit,
    offset: pagination.offset,
  }), [botId, channel, pagination.limit, pagination.offset, searchParams, sessionId])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    const options = scopeOptionsFor(await getScopeOptions(), ['session'])
    return botId ? options.filter((option) => option.description?.startsWith(`${botId} ·`)) : options
  }, [botId])
  const loadChannels = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['channel']), [])
  const committedDraftKey = privateFilterKeys.map((key) => searchParams.get(key) ?? '').join('\u0000')

  useEffect(() => {
    setFilterDraft(draftFromSerialized(committedDraftKey))
  }, [committedDraftKey])

  const load = useCallback(async () => {
    traceRequestRef.current?.controller.abort()
    const controller = new AbortController()
    const request = { id: (traceRequestRef.current?.id ?? 0) + 1, controller }
    traceRequestRef.current = request
    setStatus('loading')
    setError(undefined)
    try {
      const next = await listInjectionTraces(filters, controller.signal)
      if (traceRequestRef.current !== request || controller.signal.aborted) return
      setPayload(next)
      setStatus(next.items.length ? 'success' : 'empty')
    } catch (reason) {
      if (traceRequestRef.current !== request || controller.signal.aborted || isRequestCancelled(reason)) return
      setPayload(null)
      setError(reason)
      setStatus('error')
    }
  }, [filters])

  useEffect(() => {
    void load()
    return () => traceRequestRef.current?.controller.abort()
  }, [load])

  useEffect(() => {
    if (!selectedTraceId) {
      setDetail(null)
      setDetailError('')
      return
    }
    let active = true
    const controller = new AbortController()
    setDetail(null)
    setDetailLoading(true)
    setDetailError('')
    getInjectionTrace(selectedTraceId, controller.signal)
      .then((value) => { if (active) setDetail(value) })
      .catch((reason: unknown) => { if (active && !isRequestCancelled(reason)) setDetailError(humanizeApiError(reason, '注入详情加载失败')) })
      .finally(() => { if (active) setDetailLoading(false) })
    return () => { active = false; controller.abort() }
  }, [selectedTraceId])

  function selectTrace(traceId: string | null) {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      if (traceId) next.set('trace_id', traceId)
      else next.delete('trace_id')
      return next
    })
  }

  function submitFilters() {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      privateFilterKeys.forEach((key) => {
        const value = filterDraft[key].trim()
        if (value) next.set(key, value)
        else next.delete(key)
      })
      next.delete('trace_id')
      next.delete('offset')
      return next
    })
  }

  function resetFilters() {
    setFilterDraft(emptyDraft)
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      privateFilterKeys.forEach((key) => next.delete(key))
      next.delete('trace_id')
      next.delete('offset')
      return next
    })
  }

  return (
    <div data-slot="observatory-page" className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>注入观测台</CardTitle>
          <CardDescription>筛选一次回复用了哪些记忆通道。筛选保存在网址里；Bot、群和通道均来自服务端真实选项。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <FieldGroup className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <ScopeSelect value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择 Bot" onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null })} />
            <ScopeSelect value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择群" disabled={!botId} onValueChange={(value) => pagination.setFilters({ session_id: value })} />
            <ScopeSelect value={filterDraft.channel || undefined} loadOptions={loadChannels} label="通道" placeholder="选择已注册通道" onValueChange={(value) => setFilterDraft((current) => ({ ...current, channel: value }))} />
            <Field><FieldLabel htmlFor="trace-sender">发送者 ID</FieldLabel><Input id="trace-sender" value={filterDraft.sender_id} onChange={(event) => setFilterDraft((current) => ({ ...current, sender_id: event.target.value }))} /></Field>
            <Field>
              <FieldLabel>状态</FieldLabel>
              <Select value={filterDraft.status || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, status: value === 'all' ? '' : value }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectGroup><SelectItem value="all">全部状态</SelectItem><SelectItem value="ok">正常</SelectItem><SelectItem value="error">错误</SelectItem><SelectItem value="timeout">超时</SelectItem><SelectItem value="skipped">已跳过</SelectItem></SelectGroup></SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel>错误</FieldLabel>
              <Select value={filterDraft.has_error || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, has_error: value === 'all' ? '' : value }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectGroup><SelectItem value="all">全部</SelectItem><SelectItem value="true">有错误</SelectItem><SelectItem value="false">无错误</SelectItem></SelectGroup></SelectContent>
              </Select>
            </Field>
            <Field><FieldLabel htmlFor="trace-scope">群 / 聊天类型</FieldLabel><Input id="trace-scope" value={filterDraft.scope} onChange={(event) => setFilterDraft((current) => ({ ...current, scope: event.target.value }))} placeholder="按实际群或聊天类型筛选" /></Field>
            <Field><FieldLabel htmlFor="trace-from">开始时间</FieldLabel><Input id="trace-from" type="datetime-local" value={epochToInput(filterDraft.from_ts || null)} onChange={(event) => setFilterDraft((current) => ({ ...current, from_ts: inputToEpoch(event.target.value) }))} /></Field>
            <Field><FieldLabel htmlFor="trace-to">结束时间</FieldLabel><Input id="trace-to" type="datetime-local" value={epochToInput(filterDraft.to_ts || null)} onChange={(event) => setFilterDraft((current) => ({ ...current, to_ts: inputToEpoch(event.target.value) }))} /></Field>
            <Field><FieldLabel htmlFor="trace-revision">配置版本</FieldLabel><Input id="trace-revision" value={filterDraft.config_revision} onChange={(event) => setFilterDraft((current) => ({ ...current, config_revision: event.target.value }))} placeholder="例如 cfg-…" /></Field>
          </FieldGroup>
          <div className="flex flex-wrap gap-2">
            <Button type="button" onClick={submitFilters}><SearchIcon data-icon="inline-start" aria-hidden="true" />查询</Button>
            <Button type="button" variant="outline" disabled={status === 'loading'} onClick={() => void load()}><RefreshCwIcon data-icon="inline-start" aria-hidden="true" />刷新</Button>
            <Button type="button" variant="ghost" onClick={resetFilters}>清除筛选</Button>
          </div>
        </CardContent>
      </Card>

      <Alert>
        <AlertTitle>如何阅读与验证这次注入</AlertTitle>
        <AlertDescription>先核对 Bot 和群，再检查配置版本、通道状态、预算、命中、过滤原因、错误和最终文本。</AlertDescription>
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle>注入摘要</CardTitle>
          <CardDescription>{payload?.page.total_status === 'exact' ? `当前筛选共 ${payload.page.total ?? 0} 条` : `总数暂不可用：${humanizeReason(payload?.page.reason_code, '等待查询')}`}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <QueryState status={status} error={error} onRetry={() => void load()}>
            <DeclarativeDataTable
              label="注入摘要清单"
              items={payload?.items ?? []}
              keyExtractor={(row) => String(row.trace_id ?? '')}
              columns={[
                { key: 'trace', header: '记录 / 时间', isTitle: true, render: (row) => <span className="flex min-w-40 flex-col"><Button type="button" variant="link" className="h-auto justify-start p-0 font-mono text-xs" onClick={(event) => { event.stopPropagation(); selectTrace(String(row.trace_id ?? '')) }}>{row.trace_id || '未记录编号'}</Button><span className="text-xs text-muted-foreground">{formatTime(row.timestamp ?? row.created_at)}</span></span> },
                { key: 'bot', header: 'Bot', render: (row) => <span className="font-mono text-xs">{traceBot(row)}</span> },
                { key: 'session', header: '会话', render: (row) => { const s = traceSession(row); return <span className="flex min-w-44 flex-col"><span>{s.primary}</span>{s.secondary ? <span className="text-xs text-muted-foreground">{s.secondary}</span> : null}</span> } },
                { key: 'status', header: '模式 / 状态', render: (row) => { const st = String(row.status ?? (row.has_error ? 'error' : 'unknown')); return <span className="flex flex-col gap-1"><span>{textValue(row.mode)}</span><Badge className="w-fit" variant={st === 'ok' ? 'secondary' : st === 'unknown' ? 'outline' : 'destructive'}>{statusLabel(st)}</Badge></span> } },
                { key: 'preview', header: '预览', render: (row) => <span className="max-w-md truncate" title={tracePreview(row)}>{tracePreview(row)}</span> },
                { key: 'channels', header: '命中 / 跳过 / 错误', render: (row) => <span className="font-mono text-xs">{channelCount(row, 'hit')} / {channelCount(row, 'skipped')} / {channelCount(row, 'error')}</span> },
                { key: 'token_channel', header: '主 Token 通道', render: (row) => <span className="font-mono text-xs">{primaryTokenChannel(row)}</span> },
                { key: 'tokens_lat', header: 'Token / 耗时', render: (row) => <span>{textValue(row.total_tokens ?? row.tokens)} / {traceLatency(row)}</span> },
                { key: 'revision', header: '配置版本', render: (row) => <span className="font-mono text-xs">{textValue(row.config_revision)}</span> },
              ]}
              onRowClick={(row) => row.trace_id && selectTrace(String(row.trace_id))}
              renderCardActions={(row) => row.trace_id ? <Button type="button" variant="outline" size="sm" onClick={() => selectTrace(String(row.trace_id))}>查看注入详情</Button> : null}
            />
          </QueryState>
          {payload ? <PaginationControls page={payload.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} disabled={status === 'loading'} /> : null}
        </CardContent>
      </Card>

      <TraceDetailSheet open={Boolean(selectedTraceId)} onOpenChange={(open) => { if (!open) selectTrace(null) }} detail={detail} loading={detailLoading} error={detailError} />
    </div>
  )
}

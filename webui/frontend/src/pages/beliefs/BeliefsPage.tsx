import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  ArchiveIcon,
  BrainCircuitIcon,
  CheckIcon,
  EyeIcon,
  Loader2Icon,
  MessageSquareTextIcon,
  PlusIcon,
  ShieldCheckIcon,
  Trash2Icon,
} from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { humanizeApiError, humanizeReason } from '@/lib/reason-label'

import {
  approveBelief,
  archiveBelief,
  batchTransitionBeliefs,
  getBeliefEvidence,
  listBeliefs,
  type BeliefEvidencePayload,
  type BeliefItem,
  type BeliefType,
  type ScopedSelection,
} from '@/api/beliefs'
import { fetchJson } from '@/api/client'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import {
  BatchActionBar,
  EvidenceList,
  ObjectDeepLink,
  PaginationControls,
  QueryState,
  QualityDecisionBadge,
  ResponsiveDetail,
  ResponsiveTable,
  ScopeFilterBar,
  type ObjectRefState,
} from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'

const TYPE_LABELS: Record<BeliefType, string> = {
  self_identity: '自我身份',
  person_judgment: '人物判断',
  world_view: '世界观',
  preference: '偏好',
}

const STATUS_LABELS: Record<BeliefItem['status'], string> = {
  pending: '待审核',
  active: '已生效',
  archived: '已归档',
  quarantined: '已隔离',
}

const COMPONENT_LABELS: Record<string, string> = {
  evidence: '证据质量',
  frequency: '出现频率',
  recency: '近期程度',
  consistency: '一致性',
  source: '来源独立性',
  span: '时间跨度',
  confidence: '综合支持强度',
}

const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  memory: '记忆证据',
  episode: '情节证据',
  relationship_event: '关系事件',
  message: '消息记录',
}

const DEEP_LINK_LABELS: Record<Exclude<ObjectRefState, 'ready'>, string> = {
  'not-found': '对象不存在或引用无效；不会用裸编号回退定位。',
  'scope-mismatch': '这条信念不属于当前 Bot 和群。',
  'version-stale': '对象版本已更新，请从最新列表重新打开。',
}

function deepLinkFailureState(reason: unknown): ObjectRefState {
  const payload = reason instanceof Error && 'payload' in reason ? (reason as Error & { payload?: unknown }).payload : undefined
  const code = typeof payload === 'object' && payload !== null && 'error' in payload
    ? (payload as { error?: { code?: unknown } }).error?.code
    : undefined
  if (code === 'scope_mismatch') return 'scope-mismatch'
  if (code === 'version_stale') return 'version-stale'
  return 'not-found'
}

function statusClass(status: BeliefItem['status']) {
  if (status === 'active') return 'border-primary/20 bg-primary/10 text-primary'
  if (status === 'pending') return 'border-border bg-secondary text-secondary-foreground'
  if (status === 'quarantined') return 'border-destructive/20 bg-destructive/10 text-destructive'
  return 'border-border bg-muted text-muted-foreground'
}

function typeClass(type: BeliefType) {
  if (type === 'self_identity') return 'border-primary/20 bg-primary/10 text-primary'
  if (type === 'person_judgment') return 'border-border bg-muted text-foreground'
  if (type === 'world_view') return 'border-border bg-accent text-accent-foreground'
  return 'border-border bg-secondary text-secondary-foreground'
}

function confidenceText(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '未评估'
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
}

function gatingLabel(decision: BeliefItem['gating']['decision'] | undefined) {
  if (decision === 'direct') return '关系门禁：自动放行'
  if (decision === 'quarantine') return '关系门禁：已隔离'
  if (decision === 'pending') return '关系门禁：待审核'
  return '关系门禁：未评估'
}

function formatTime(seconds?: number | null) {
  return seconds && Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString('zh-CN') : '未记录'
}

function recordText(record: Record<string, unknown>, keys: string[], fallback = '未记录') {
  for (const key of keys) {
    const value = record[key]
    if (value !== undefined && value !== null && String(value).trim()) return String(value)
  }
  return fallback
}

function ConfidenceComponents({ item }: { item: BeliefItem }) {
  const components = Object.entries(item.confidence_components ?? {})
  if (!components.length) return <p className="text-xs text-muted-foreground">服务端未返回置信分量。</p>
  return <div className="flex flex-wrap gap-2">{components.map(([key, raw]) => {
    const value = Math.max(0, Math.min(1, Number(raw) || 0))
    return <Badge key={key} variant="outline" className="gap-1 font-normal text-xs">{COMPONENT_LABELS[key] ?? key}: <span className="font-semibold">{Math.round(value * 100)}%</span></Badge>
  })}</div>
}

function ConfidenceEvidenceSummary({ item }: { item: BeliefItem }) {
  const summary = item.confidence_evidence
  if (!summary) return null
  const count = (key: string) => Math.max(0, Math.trunc(Number(summary[key]) || 0))
  const spanSeconds = Math.max(0, Number(summary.span_seconds) || 0)
  const spanText = spanSeconds >= 86_400 ? `${Math.round(spanSeconds / 86_400)} 天` : spanSeconds >= 3_600 ? `${Math.round(spanSeconds / 3_600)} 小时` : '同一时间段'
  return <div className="flex flex-wrap gap-2 text-xs text-muted-foreground"><Badge variant="secondary">支持经历 {count('support_windows')} 段</Badge><Badge variant="outline">反证经历 {count('challenge_windows')} 段</Badge><Badge variant="outline">支持消息 {count('support_messages')} 条</Badge><Badge variant="outline">独立来源 {count('distinct_sources')} 个</Badge><Badge variant="outline">跨度 {spanText}</Badge></div>
}

function EvidenceCards({ item }: { item: BeliefItem }) {
  if (!item.evidence.length) return <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">当前没有可用证据，系统不会将它直接晋升为有效信念。</div>
  return <div className="grid gap-3 sm:grid-cols-2">{item.evidence.slice(0, 4).map((evidence) => <div key={`${evidence.type}:${evidence.id}`} className="rounded-lg border bg-card p-3"><div className="flex items-center justify-between gap-2"><span className="font-medium">{EVIDENCE_TYPE_LABELS[evidence.type] ?? '关联证据'}</span><Badge variant={evidence.availability === 'available' ? 'secondary' : 'outline'}>{evidence.availability === 'available' ? '可用' : evidence.availability === 'quarantined' ? '已隔离' : '待核验'}</Badge></div><p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{evidence.summary || '已保留可追溯引用，可在技术详情中核对完整来源。'}</p></div>)}</div>
}

function BeliefDetails({ item, mutating, onTransition }: { item: BeliefItem; mutating: boolean; onTransition: (action: 'approve' | 'archive') => void }) {
  return <div className="flex flex-col gap-6">
    <section className="flex flex-col gap-2"><div className="flex flex-wrap gap-2"><Badge className={typeClass(item.type)}>{TYPE_LABELS[item.type]}</Badge><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge><Badge variant={item.gating?.decision === 'quarantine' ? 'destructive' : 'outline'}>{gatingLabel(item.gating?.decision)}</Badge></div><p className="text-base leading-7 text-foreground">{item.content}</p>{item.anchor_sentence ? <blockquote className="rounded-r-lg border-l-2 border-primary bg-primary/5 px-4 py-3 text-muted-foreground">“{item.anchor_sentence}”</blockquote> : null}</section>
    <section className="flex flex-col gap-3"><div className="flex items-center justify-between gap-3"><h3 className="font-medium">证据支持分量</h3><Badge variant="outline">综合 {confidenceText(item.confidence)}</Badge></div><ConfidenceComponents item={item} /><ConfidenceEvidenceSummary item={item} /></section>
    <section className="flex flex-col gap-2 rounded-lg border bg-muted/20 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">关系门禁</h3><span className="text-xs text-muted-foreground">策略版本 {item.gating?.policy_version ?? '未记录'}</span></div><div className="flex flex-wrap gap-2 text-xs"><Badge variant="outline">信任度 {item.gating?.trust == null ? '未知' : Math.round(item.gating.trust)}</Badge><Badge variant="outline">敌意 {item.gating?.hostility == null ? '未知' : Math.round(item.gating.hostility)}</Badge><Badge variant="outline">参与者 {item.gating?.subjects?.length ?? 0}</Badge>{item.gating?.review_required ? <Badge variant="secondary">需要人工复核</Badge> : <Badge variant="secondary">跳过人工复核</Badge>}</div><p className="text-sm text-muted-foreground">高信任仅表示可以跳过人工复核；进入激活状态仍必须满足证据支持窗口、置信度、标签链和证据要求。{item.gating?.reason_code ? ` 当前原因：${humanizeReason(item.gating.reason_code, '未提供原因')}` : ''}</p></section>
    <section className="flex flex-col gap-3"><div className="flex items-center justify-between gap-3"><h3 className="font-medium">证据卡</h3><span className="text-sm text-muted-foreground">{item.evidence.length} 条</span></div><EvidenceCards item={item} /></section>
    {item.quarantine_reason ? <Alert variant="destructive"><AlertTitle>当前处于隔离状态</AlertTitle><AlertDescription>{item.quarantine_reason}</AlertDescription></Alert> : null}
    <div className="flex flex-wrap gap-2 border-t pt-4"><Button disabled={mutating || !item.actions.approve.available} onClick={() => onTransition('approve')}><CheckIcon data-icon="inline-start" />通过并激活</Button><Button variant="outline" disabled={mutating || !item.actions.archive.available} onClick={() => onTransition('archive')}><ArchiveIcon data-icon="inline-start" />归档</Button>{!item.actions.approve.available ? <span className="self-center text-sm text-muted-foreground">无法通过：{humanizeReason(item.actions.approve.reason_code, '当前状态不允许')}</span> : null}</div>
    <details className="rounded-lg border bg-muted/20 p-3"><summary className="cursor-pointer font-medium">技术字段与完整证据引用</summary><div className="mt-4 flex flex-col gap-4"><dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">信念键</dt><dd className="break-all font-mono">{item.belief_key}</dd></div><div><dt className="text-muted-foreground">修订版本</dt><dd className="font-mono">{item.revision}</dd></div><div><dt className="text-muted-foreground">置信策略</dt><dd className="break-all font-mono">{item.confidence_policy_version ?? '未记录'}</dd></div><div><dt className="text-muted-foreground">当前群</dt><dd className="break-all font-mono">{item.bot_id} · {item.session_id}</dd></div></dl><EvidenceList evidence={item.evidence} />{item.object_ref ? <ObjectDeepLink to="/beliefs" objectRef={item.object_ref}>复制可复现跳转链接</ObjectDeepLink> : null}<details className="rounded-md border bg-background p-3"><summary className="cursor-pointer text-sm">查看置信分量 JSON</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item.confidence_components ?? {}, null, 2)}</pre></details></div></details>
  </div>
}

function BeliefEvidenceDialog({ item, scope, onOpenChange }: { item: BeliefItem | null; scope: ScopedSelection | null; onOpenChange: (open: boolean) => void }) {
  // 序号守卫：切换选中的信念时旧响应可能后到，避免详情串到另一条信念上。
  const detailRequest = useRef(0)
  const [payload, setPayload] = useState<BeliefEvidencePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [before, setBefore] = useState(15)
  const [after, setAfter] = useState(15)
  const [tab, setTab] = useState<'relationship_event' | 'episode' | 'memory'>('memory')

  const load = useCallback(async () => {
    if (!item || !scope) return
    // 切换选中的信念时旧响应可能后到，用序号丢弃，避免详情串到另一条信念上。
    const request = ++detailRequest.current
    setLoading(true)
    setError('')
    try {
      const next = await getBeliefEvidence(item, scope, before, after)
      if (request !== detailRequest.current) return
      setPayload(next)
      setTab(next.relationship_events.length ? 'relationship_event' : next.episodes.length ? 'episode' : 'memory')
    } catch (reason) {
      if (request !== detailRequest.current) return
      setPayload(null)
      setError(humanizeApiError(reason, '信念证据读取失败'))
    } finally {
      if (request === detailRequest.current) setLoading(false)
    }
  }, [after, before, item, scope])

  useEffect(() => {
    if (!item) {
      setPayload(null)
      setError('')
      return
    }
    void load()
  }, [item, load])

  const messages = payload?.messages ?? []
  const supportAnchors = payload?.support_anchors ?? []
  const challengeAnchors = payload?.challenge_anchors ?? []
  return <Dialog open={Boolean(item)} onOpenChange={onOpenChange}><DialogContent className="flex max-h-[86vh] flex-col sm:max-w-4xl max-w-[95vw] overflow-hidden"><DialogHeader><DialogTitle className="flex flex-wrap items-center gap-2"><span>信念形成多阶证据链</span>{item ? <Badge variant="outline">#{item.id}</Badge> : null}</DialogTitle><DialogDescription>查看支撑该信念的事实、关系变动与关联记忆证据。</DialogDescription></DialogHeader>
    <div className="flex-1 overflow-y-auto pr-1">
    {loading ? <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在还原同作用域证据链</div> : null}
    {!loading && error ? <Alert variant="destructive"><AlertTitle>证据读取失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
    {!loading && payload ? <Tabs value={tab} onValueChange={(value) => setTab(value as typeof tab)} className="flex min-h-0 flex-1 flex-col"><div className="flex flex-wrap items-end justify-between gap-3 border-b pb-3"><div className="flex items-end gap-2"><Field className="w-20 gap-1"><FieldLabel htmlFor="belief-evidence-before">前文</FieldLabel><Input id="belief-evidence-before" type="number" min={0} max={50} value={before} onChange={(event) => setBefore(Math.max(0, Math.min(50, Number(event.target.value) || 0)))} /></Field><Field className="w-20 gap-1"><FieldLabel htmlFor="belief-evidence-after">后文</FieldLabel><Input id="belief-evidence-after" type="number" min={0} max={50} value={after} onChange={(event) => setAfter(Math.max(0, Math.min(50, Number(event.target.value) || 0)))} /></Field><Button type="button" size="sm" onClick={() => void load()}>刷新</Button></div><TabsList><TabsTrigger value="relationship_event" disabled={!payload.relationship_events.length}>关系变化</TabsTrigger><TabsTrigger value="episode" disabled={!payload.episodes.length}>自省独白</TabsTrigger><TabsTrigger value="memory" disabled={!messages.length}>聊天气泡</TabsTrigger></TabsList></div>
      <TabsContent value="relationship_event" className="min-h-0 flex-1 pt-4"><ScrollArea className="max-h-[55vh]"><div className="flex flex-col gap-3 pr-3">{payload.relationship_events.map((event, index) => <article key={recordText(event, ['id'], String(index))} className="rounded-lg border bg-card p-4"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{recordText(event, ['dimension', 'event_type'], '关系变化')}</span><Badge variant="outline">{recordText(event, ['delta'], '—')}</Badge></div><p className="mt-2 text-sm text-muted-foreground">{recordText(event, ['reason', 'summary'])}</p></article>)}</div></ScrollArea></TabsContent>
      <TabsContent value="episode" className="min-h-0 flex-1 pt-4"><ScrollArea className="max-h-[55vh]"><div className="flex flex-col gap-3 pr-3">{payload.episodes.map((episode, index) => <article key={recordText(episode, ['id'], String(index))} className="rounded-lg border bg-card p-4"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{recordText(episode, ['episode_type'], '自省插曲')}</span><span className="text-xs text-muted-foreground">{formatTime(Number(episode.created_at) || null)}</span></div><dl className="mt-3 grid gap-3 text-sm"><div><dt className="text-muted-foreground">外部触发</dt><dd>{recordText(episode, ['trigger'])}</dd></div><div><dt className="text-muted-foreground">内心独白</dt><dd>{recordText(episode, ['bot_inner_thought'])}</dd></div><div><dt className="text-muted-foreground">回复与后果</dt><dd>{recordText(episode, ['bot_reply', 'outcome'])}</dd></div></dl></article>)}</div></ScrollArea></TabsContent>
      <TabsContent value="memory" className="min-h-0 flex-1 pt-4"><ScrollArea className="max-h-[55vh]"><div className="flex flex-col gap-4 pr-3">{supportAnchors.length || challengeAnchors.length ? <section className="grid gap-3 md:grid-cols-2"><div className="rounded-lg border border-primary/20 bg-primary/5 p-3"><div className="flex items-center justify-between gap-2"><h4 className="text-sm font-medium">支持经历锚点</h4><Badge variant="secondary">{supportAnchors.length} 条</Badge></div><div className="mt-3 flex flex-col gap-2">{supportAnchors.map((message) => <article key={`support:${message.id}`} className="rounded-md border bg-card p-3"><div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{message.sender_name || message.sender_id || '发送者未记录'}</span><span>{formatTime(message.timestamp)}</span></div><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p></article>) || <p className="text-sm text-muted-foreground">暂无可还原的支持锚点。</p>}</div></div><div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3"><div className="flex items-center justify-between gap-2"><h4 className="text-sm font-medium">反证经历锚点</h4><Badge variant="outline">{challengeAnchors.length} 条</Badge></div><div className="mt-3 flex flex-col gap-2">{challengeAnchors.map((message) => <article key={`challenge:${message.id}`} className="rounded-md border bg-card p-3"><div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{message.sender_name || message.sender_id || '发送者未记录'}</span><span>{formatTime(message.timestamp)}</span></div><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p></article>) || <p className="text-sm text-muted-foreground">暂无已记录的反证锚点。</p>}</div></div></section> : null}{messages.map((message) => <article key={`${message.role}:${message.id}`} data-evidence-role={message.role} className={message.role === 'anchor' ? 'rounded-lg border border-primary/30 bg-primary/5 p-3' : 'rounded-lg border bg-card p-3'}><div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{message.sender_name || message.sender_id || '发送者未记录'}</span><span>{formatTime(message.timestamp)}</span></div><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p>{message.role === 'anchor' ? <Badge className="mt-2" variant="secondary">信念来源锚点</Badge> : null}</article>)}</div></ScrollArea></TabsContent>
    </Tabs> : null}
    {!loading && payload && !messages.length && !payload.relationship_events.length && !payload.episodes.length ? <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">该信念没有可安全还原的本群证据；请先确认已选 Bot 和群。</div> : null}
    </div>
  </DialogContent></Dialog>
}

export function BeliefsPage() {
  const pagination = usePaginationSearchParams()
  const [searchParams] = useSearchParams()
  const [payload, setPayload] = useState<Awaited<ReturnType<typeof listBeliefs>> | null>(null)
  const [queryStatus, setQueryStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [mutating, setMutating] = useState<number | null>(null)
  const [batchMutating, setBatchMutating] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [evidenceItem, setEvidenceItem] = useState<BeliefItem | null>(null)
  const [deepLinkedItem, setDeepLinkedItem] = useState<BeliefItem | null>(null)
  const [deepLinkStatus, setDeepLinkStatus] = useState<'loading' | ObjectRefState | null>(null)
  const listRequest = useRef(0)
  const botId = searchParams.get('bot_id') ?? ''
  const sessionId = searchParams.get('session_id') ?? ''
  const visibility = searchParams.get('visibility') ?? 'group'
  const objectRef = searchParams.get('ref') ?? ''
  const objectId = searchParams.get('object_id') ?? ''
  const type = (searchParams.get('type') ?? '') as BeliefType | ''
  const status = searchParams.get('status') ?? ''
  const search = searchParams.get('search') ?? ''
  const evidenceHealth = searchParams.get('evidence_health') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const [filterDraft, setFilterDraft] = useState({ search, type, status, evidenceHealth })

  useEffect(() => setFilterDraft({ search, type, status, evidenceHealth }), [evidenceHealth, search, status, type])

  const scope = useMemo<ScopedSelection | null>(() => botId && sessionId && visibility === 'group' ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId, visibility])
  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])

  const load = useCallback(async () => {
    const request = ++listRequest.current
    if (!scope) {
      setPayload(null)
      setSelectedIds([])
      setQueryStatus('empty')
      return
    }
    setQueryStatus('loading')
    setError(undefined)
    try {
      const next = await listBeliefs({
        ...scope,
        limit: pagination.limit,
        offset: pagination.offset,
        type: type || undefined,
        status: status || undefined,
        search: search || undefined,
        evidence_health: evidenceHealth || undefined,
      })
      if (request !== listRequest.current) return
      setPayload(next)
      setSelectedIds([])
      setQueryStatus(next.items.length ? 'success' : 'empty')
    } catch (reason) {
      if (request !== listRequest.current) return
      setPayload(null)
      setSelectedIds([])
      setError(reason)
      setQueryStatus('error')
    }
  }, [evidenceHealth, pagination.limit, pagination.offset, scope, search, status, type])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!objectRef) { setDeepLinkedItem(null); setDeepLinkStatus(null); return }
    if (!scope) { setDeepLinkedItem(null); setDeepLinkStatus('scope-mismatch'); return }
    if (objectId && !/^\d+$/.test(objectId)) { setDeepLinkedItem(null); setDeepLinkStatus('not-found'); return }
    const query = new URLSearchParams({ ref: objectRef, ...scope })
    const endpoint = objectId ? `/api/beliefs/${objectId}?${query.toString()}` : `/api/beliefs/resolve?${query.toString()}`
    let cancelled = false
    setDeepLinkStatus('loading')
    fetchJson<{ item: BeliefItem }>(endpoint).then((result) => {
      if (cancelled) return
      setDeepLinkedItem(result.item)
      setDeepLinkStatus('ready')
    }).catch((reason) => {
      if (cancelled) return
      setDeepLinkedItem(null)
      setDeepLinkStatus(deepLinkFailureState(reason))
    })
    return () => { cancelled = true }
  }, [objectId, objectRef, scope])

  async function transition(item: BeliefItem, action: 'approve' | 'archive') {
    if (!scope || !item.actions[action].available) return
    setMutating(item.id)
    try {
      const result = action === 'approve' ? await approveBelief(item, scope) : await archiveBelief(item, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认生命周期变更成功')
      toast.success(action === 'approve' ? '信念已通过证据门并激活' : '信念已归档')
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '信念状态变更失败'))
    } finally {
      setMutating(null)
    }
  }

  async function transitionSelected(action: 'approve' | 'archive') {
    if (!scope) return
    const items = (payload?.items ?? []).filter((item) => selectedIds.includes(item.id))
    if (!items.length) return
    setBatchMutating(true)
    try {
      const result = await batchTransitionBeliefs(items, action, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认批量生命周期变更成功')
      toast.success(action === 'approve' ? `已批量激活 ${result.transitioned_count} 条信念` : `已批量归档 ${result.transitioned_count} 条信念`)
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '批量生命周期变更失败'))
    } finally {
      setBatchMutating(false)
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    pagination.setFilters({ search: filterDraft.search.trim() || null, type: filterDraft.type || null, status: filterDraft.status || null, evidence_health: filterDraft.evidenceHealth || null })
  }

  function resetFilters() {
    setFilterDraft({ search: '', type: '', status: '', evidenceHealth: '' })
    pagination.setFilters({ search: null, type: null, status: null, evidence_health: null })
  }

  const pageItems = payload?.items ?? []
  const selectedItems = pageItems.filter((item) => selectedIds.includes(item.id))
  const allPageSelected = pageItems.length > 0 && pageItems.every((item) => selectedIds.includes(item.id))
  const activeCount = payload ? pageItems.filter((item) => item.status === 'active').length : '—'
  const pendingCount = payload ? pageItems.filter((item) => item.status === 'pending').length : '—'
  const evidenceCount = payload ? pageItems.reduce((sum, item) => sum + item.evidence.length, 0) : '—'
  const totalText = payload?.page.total_status === 'exact' && payload.page.total !== null ? payload.page.total : '—'
  const batchAvailable = payload?.capabilities.batch_lifecycle?.available === true
  const selectedCanApprove = selectedItems.length > 0 && selectedItems.every((item) => item.actions.approve.available && item.object_ref)
  const selectedCanArchive = selectedItems.length > 0 && selectedItems.every((item) => item.actions.archive.available && item.object_ref)

  return <div data-slot="beliefs-page" className="flex flex-col gap-6">
    <Card className="overflow-hidden border-primary/10 bg-gradient-to-br from-primary/5 via-card to-card"><CardHeader className="gap-4 sm:flex-row sm:items-start sm:justify-between"><div className="flex gap-3"><div className="rounded-xl bg-primary/10 p-3 text-primary"><BrainCircuitIcon className="size-6" /></div><div><CardTitle className="text-xl">信念审核与证据链</CardTitle><CardDescription className="mt-1 max-w-3xl">审核、批量改状态和多阶证据追溯都只作用于当前 Bot 和群，并核对版本。</CardDescription></div></div><Badge variant="outline" className="w-fit"><ShieldCheckIcon className="size-3.5" />受控生命周期</Badge></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><div className="rounded-lg border bg-background/80 p-3"><p className="text-xs text-muted-foreground">匹配总数</p><p className="mt-1 text-2xl font-semibold tabular-nums">{totalText}</p></div><div className="rounded-lg border bg-background/80 p-3"><p className="text-xs text-muted-foreground">本页已生效</p><p className="mt-1 text-2xl font-semibold tabular-nums">{activeCount}</p></div><div className="rounded-lg border bg-background/80 p-3"><p className="text-xs text-muted-foreground">本页待审核</p><p className="mt-1 text-2xl font-semibold tabular-nums">{pendingCount}</p></div><div className="rounded-lg border bg-background/80 p-3"><p className="text-xs text-muted-foreground">本页证据引用</p><p className="mt-1 text-2xl font-semibold tabular-nums text-primary">{evidenceCount}</p></div></CardContent></Card>

    {deepLinkStatus ? <Alert data-slot="belief-deep-link-state" variant={deepLinkStatus === 'ready' || deepLinkStatus === 'loading' ? 'default' : 'destructive'}><AlertTitle>{deepLinkStatus === 'loading' ? '正在校验跳转链接' : deepLinkStatus === 'ready' ? '已定位这条信念' : '无法打开这条信念'}</AlertTitle><AlertDescription>{deepLinkStatus === 'ready' && deepLinkedItem ? <span><strong>{TYPE_LABELS[deepLinkedItem.type]}</strong>：{deepLinkedItem.content}</span> : deepLinkStatus === 'loading' ? '正在验证当前群和版本是否还对得上。' : DEEP_LINK_LABELS[deepLinkStatus as Exclude<ObjectRefState, 'ready'>]}</AlertDescription></Alert> : null}

    <ScopeFilterBar
      botId={botId}
      sessionId={sessionId}
      loadBots={loadBots}
      loadSessions={loadSessions}
      onBotChange={(val) => pagination.setFilters({ bot_id: val, session_id: null })}
      onSessionChange={(val) => pagination.setFilters({ session_id: val })}
      searchValue={filterDraft.search}
      onSearchChange={(val) => setFilterDraft((current) => ({ ...current, search: val }))}
      searchPlaceholder="搜索信念内容或锚定句…"
      onSubmit={submitSearch}
      onReset={resetFilters}
      actions={
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled
          title={humanizeReason(payload?.capabilities.create?.reason_code, '带证据的新建命令尚未开放')}
        >
          <PlusIcon data-icon="inline-start" />
          新增信念
        </Button>
      }
    >
      <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
        <FieldLabel>信念类型</FieldLabel>
        <Select
          value={filterDraft.type || 'all'}
          onValueChange={(value) => setFilterDraft((current) => ({ ...current, type: value === 'all' ? '' : value as BeliefType }))}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">全部类型</SelectItem>
              {Object.entries(TYPE_LABELS).map(([value, label]) => (
                <SelectItem key={value} value={value}>{label}</SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
        <FieldLabel>生命周期</FieldLabel>
        <Select
          value={filterDraft.status || 'all'}
          onValueChange={(value) => setFilterDraft((current) => ({ ...current, status: value === 'all' ? '' : value }))}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">全部状态</SelectItem>
              <SelectItem value="pending">待审核</SelectItem>
              <SelectItem value="active">已生效</SelectItem>
              <SelectItem value="archived">已归档</SelectItem>
              <SelectItem value="quarantined">已隔离</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
        <FieldLabel>证据健康</FieldLabel>
        <Select
          value={filterDraft.evidenceHealth || 'all'}
          onValueChange={(value) => setFilterDraft((current) => ({ ...current, evidenceHealth: value === 'all' ? '' : value }))}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">不限证据</SelectItem>
              <SelectItem value="available">证据可用</SelectItem>
              <SelectItem value="unavailable">证据不可用</SelectItem>
              <SelectItem value="quarantined">证据已隔离</SelectItem>
              <SelectItem value="unknown">证据未知</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
    </ScopeFilterBar>

    <Card>
      <CardHeader>
        <CardTitle className="text-base">信念清单</CardTitle>
        <CardDescription>浏览当前群聊沉淀的共识信念，支持查看多阶证据与生命周期审核。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <BatchActionBar
          selectedCount={selectedItems.length}
          disabled={batchMutating || !batchAvailable}
          onClear={() => setSelectedIds([])}
          extra={!payload?.capabilities.select_all_matching?.available ? `跨页全部匹配暂不可用：${humanizeReason(payload?.capabilities.select_all_matching?.reason_code, '需要服务端重新签发整批引用')}` : undefined}
        >
          <Button
            type="button"
            size="sm"
            disabled={batchMutating || !batchAvailable || !selectedCanApprove}
            onClick={() => void transitionSelected('approve')}
          >
            <CheckIcon data-icon="inline-start" />
            批量确认通过
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={batchMutating || !batchAvailable || !selectedCanArchive}
            onClick={() => void transitionSelected('archive')}
          >
            <ArchiveIcon data-icon="inline-start" />
            批量归档
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            disabled
            title={humanizeReason(payload?.capabilities.physical_delete?.reason_code, '删除功能暂未开放')}
          >
            <Trash2Icon data-icon="inline-start" />
            批量删除
          </Button>
        </BatchActionBar>
      <QueryState status={queryStatus} error={error} onRetry={() => void load()} title={!scope ? '请选择 Bot 与会话' : undefined} description={!scope ? '请在上方选择 Bot 和目标群聊。' : undefined}>
        <ResponsiveTable
          label="信念清单"
          table={
            <Table className="w-full table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8 px-2 py-2">
                    <input
                      aria-label="选择当前页全部信念"
                      type="checkbox"
                      checked={allPageSelected}
                      onChange={(event) => setSelectedIds(event.target.checked ? pageItems.map((item) => item.id) : [])}
                    />
                  </TableHead>
                  <TableHead className="w-auto px-2 py-2">信念内容与锚定句</TableHead>
                  <TableHead className="w-24 px-2 py-2">类型 / 状态</TableHead>
                  <TableHead className="w-28 px-2 py-2">置信度 / 门禁</TableHead>
                  <TableHead className="w-40 px-2 py-2 text-right">时间 / 操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pageItems.map((item) => (
                  <TableRow key={item.id} data-slot="belief-card" className={selectedIds.includes(item.id) ? 'bg-primary/5' : undefined}>
                    <TableCell className="w-8 px-2 py-2.5 align-top">
                      <input
                        aria-label={`选择信念 ${item.id}`}
                        type="checkbox"
                        checked={selectedIds.includes(item.id)}
                        onChange={(event) => setSelectedIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))}
                      />
                    </TableCell>
                    <TableCell className="w-auto px-2 py-2.5 align-top whitespace-normal break-words">
                      <p className="font-medium leading-relaxed text-sm">{item.content}</p>
                      {item.anchor_sentence ? (
                        <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                          <span className="font-medium text-foreground/70">锚定句：</span>
                          {item.anchor_sentence}
                        </p>
                      ) : null}
                    </TableCell>
                    <TableCell className="w-24 px-2 py-2.5 align-top whitespace-normal">
                      <div className="flex flex-col gap-1.5 items-start">
                        <Badge className={typeClass(item.type)}>{TYPE_LABELS[item.type]}</Badge>
                        <Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge>
                      </div>
                    </TableCell>
                    <TableCell className="w-28 px-2 py-2.5 align-top whitespace-normal">
                      <div className="flex flex-col gap-1 items-start">
                        <span className="font-mono text-xs font-semibold tabular-nums">{confidenceText(item.confidence)}</span>
                        <QualityDecisionBadge decision={item.evidence_health === 'available' ? 'allow' : 'quarantine'} />
                        <span className="font-mono text-[10px] text-muted-foreground mt-0.5">{item.evidence.length} 条引证</span>
                      </div>
                    </TableCell>
                    <TableCell className="w-40 px-2 py-2.5 align-top text-right whitespace-normal">
                      <div className="flex flex-col items-end gap-1.5">
                        <span className="text-[10px] font-mono text-muted-foreground">{formatTime(item.updated_at)}</span>
                        <div className="flex items-center gap-1">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="h-6 px-1.5 text-[11px]"
                            disabled={!item.object_ref || !payload?.capabilities.evidence?.available}
                            title={item.object_ref ? '还原同作用域证据链' : '缺少服务端签发的 ObjectRef'}
                            onClick={() => setEvidenceItem(item)}
                          >
                            <MessageSquareTextIcon className="size-3" data-icon="inline-start" />
                            证据
                          </Button>
                          {(item.status === 'pending' || item.status === 'quarantined') ? (
                            <Button
                              type="button"
                              size="icon-sm"
                              className="size-6"
                              aria-label={`通过信念 ${item.id}`}
                              disabled={mutating === item.id || !item.actions.approve.available}
                              title={humanizeReason(item.actions.approve.reason_code, '通过并激活')}
                              onClick={() => void transition(item, 'approve')}
                            >
                              <CheckIcon className="size-3" />
                            </Button>
                          ) : null}
                          <Button
                            type="button"
                            variant="outline"
                            size="icon-sm"
                            className="size-6"
                            aria-label={`归档信念 ${item.id}`}
                            disabled={mutating === item.id || !item.actions.archive.available}
                            title={humanizeReason(item.actions.archive.reason_code, '归档')}
                            onClick={() => void transition(item, 'archive')}
                          >
                            <ArchiveIcon className="size-3" />
                          </Button>
                          <ResponsiveDetail
                            title={TYPE_LABELS[item.type]}
                            description="证据、状态分量与受控生命周期操作"
                            className="sm:max-w-4xl"
                            trigger={
                              <Button type="button" variant="ghost" size="icon-sm" className="size-6" aria-label={`查看信念 ${item.id} 详情`} title="查看详情">
                                <EyeIcon className="size-3" />
                              </Button>
                            }
                          >
                            <BeliefDetails item={item} mutating={mutating === item.id} onTransition={(action) => void transition(item, action)} />
                          </ResponsiveDetail>
                        </div>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          }
          cards={pageItems.map((item) => (
            <article key={item.id} data-slot="belief-card" className={`flex flex-col gap-3 rounded-lg border bg-card p-4 ${selectedIds.includes(item.id) ? 'border-primary/50 bg-primary/5' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <label className="flex min-w-0 items-start gap-2">
                  <input
                    aria-label={`选择信念 ${item.id}`}
                    type="checkbox"
                    checked={selectedIds.includes(item.id)}
                    onChange={(event) => setSelectedIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))}
                  />
                  <span className="whitespace-pre-wrap break-words font-medium leading-6">{item.content}</span>
                </label>
                <Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge>
              </div>
              <div className="flex flex-wrap gap-2">
                <Badge className={typeClass(item.type)}>{TYPE_LABELS[item.type]}</Badge>
                <span className="text-sm text-muted-foreground">置信度 {confidenceText(item.confidence)} · {item.evidence.length} 条证据</span>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Button type="button" variant="outline" size="sm" disabled={!item.object_ref} onClick={() => setEvidenceItem(item)}>
                  <MessageSquareTextIcon data-icon="inline-start" />
                  证据
                </Button>
                {(item.status === 'pending' || item.status === 'quarantined') ? (
                  <Button type="button" size="sm" disabled={!item.actions.approve.available} onClick={() => void transition(item, 'approve')}>
                    <CheckIcon data-icon="inline-start" />
                    确认
                  </Button>
                ) : null}
                <Button type="button" variant="outline" size="sm" disabled={!item.actions.archive.available} onClick={() => void transition(item, 'archive')}>
                  <ArchiveIcon data-icon="inline-start" />
                  归档
                </Button>
                <ResponsiveDetail
                  title={TYPE_LABELS[item.type]}
                  description="证据、状态分量与受控生命周期操作"
                  className="sm:max-w-4xl"
                  trigger={<Button type="button" variant="outline" size="sm">详情</Button>}
                >
                  <BeliefDetails item={item} mutating={mutating === item.id} onTransition={(action) => void transition(item, action)} />
                </ResponsiveDetail>
              </div>
            </article>
          ))}
        />
      </QueryState>{payload ? <PaginationControls page={payload.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} /> : null}
    </CardContent></Card>


    <BeliefEvidenceDialog item={evidenceItem} scope={scope} onOpenChange={(open) => { if (!open) setEvidenceItem(null) }} />
  </div>
}

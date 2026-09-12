import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ArchiveIcon, BookOpenIcon, CheckIcon, Edit2Icon, EyeIcon, Globe2Icon, Loader2Icon, MessageSquareQuoteIcon, ShieldCheckIcon, XIcon, LockIcon } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { fetchJson } from '@/api/client'
import { humanizeApiError, humanizeReason } from '@/lib/reason-label'
import { archiveJargon, batchReviewJargons, getJargonEvidence, listJargonBlocklist, listJargons, promoteJargonToGlobal, removeJargonBlocklistItem, reviewJargon, updateJargonMeaning, type JargonBlocklistItem, type JargonEvidencePayload, type JargonItem, type JargonResponse, type JargonScopeSelection } from '@/api/jargon'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import {
  BatchActionBar,
  EvidenceList,
  ObjectDeepLink,
  PaginationControls,
  QueryState,
  ResponsiveDetail,
  ResponsiveTable,
  ScopeFilterBar,
  type ObjectRefState,
} from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'
import { GlobalJargonPanel } from './GlobalJargonPanel'

const STATUS_LABELS: Record<JargonItem['status'], string> = {
  pending: '待审核',
  confirmed: '已确认',
  rejected: '已拒绝',
}

const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  memory: '记忆证据',
  episode: '情节证据',
  relationship_event: '关系事件',
  message: '消息记录',
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

const DEEP_LINK_LABELS: Record<Exclude<ObjectRefState, 'ready'>, string> = {
  'not-found': '对象不存在或引用无效；不会用裸编号回退定位。',
  'scope-mismatch': '这条黑话不属于当前 Bot 和群。',
  'version-stale': '对象版本已更新，请从最新列表重新打开。',
}

function statusClass(status: JargonItem['status']) {
  if (status === 'confirmed') return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-600'
  if (status === 'pending') return 'border-amber-500/20 bg-amber-500/10 text-amber-600'
  return 'border-red-500/20 bg-red-500/10 text-red-600'
}

function confidenceText(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '未评估'
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
}

function normalizeJargonItem(item: JargonItem): JargonItem {
  return { ...item, anchors: Array.isArray(item.anchors) ? item.anchors : [] }
}

function normalizeJargonResponse(payload: JargonResponse): JargonResponse {
  return { ...payload, items: Array.isArray(payload.items) ? payload.items.map(normalizeJargonItem) : [] }
}

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '时间未记录'
}



function AuditMetric({ label, value, description }: { label: string; value: ReactNode; description?: string }) {
  return <div className="rounded-lg border bg-background/80 p-3"><p className="text-xs text-muted-foreground">{label}</p><div className="mt-1 text-lg font-semibold">{value}</div>{description ? <p className="mt-1 text-xs text-muted-foreground">{description}</p> : null}</div>
}

function EvidenceCards({ item }: { item: JargonItem }) {
  if (!item.anchors.length) return <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">没有可解析的锚点证据，因此不能安全通过审核。</div>
  return <div className="grid gap-3 sm:grid-cols-2">{item.anchors.slice(0, 4).map((anchor) => <div key={`${anchor.type}:${anchor.id}`} className="rounded-lg border bg-card p-3"><div className="flex items-center justify-between gap-2"><span className="font-medium">{EVIDENCE_TYPE_LABELS[anchor.type] ?? '上下文证据'}</span><Badge variant={anchor.availability === 'available' ? 'secondary' : 'outline'}>{anchor.availability === 'available' ? '可用' : '待核验'}</Badge></div><p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{anchor.summary || '已保留可追溯引用，可在技术详情中核对完整来源。'}</p></div>)}</div>
}

function JargonDetails({ item, reviewAvailable, busy, onReview }: { item: JargonItem; reviewAvailable: boolean; busy: boolean; onReview: (action: 'approve' | 'reject') => void }) {
  return <div className="flex flex-col gap-6">
    <section className="flex flex-col gap-3"><div className="flex flex-wrap items-center gap-2"><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge><Badge variant="outline">出现 {item.frequency} 次</Badge><Badge variant="outline">置信度 {confidenceText(item.confidence)}</Badge></div><div><h3 className="text-xl font-semibold">{item.word}</h3><p className="mt-2 text-base leading-7 text-muted-foreground">{item.meaning || '尚未形成可展示的释义。'}</p></div></section>
    <section className="flex flex-col gap-3"><div className="flex items-center justify-between"><h3 className="font-medium">证据卡</h3><span className="text-sm text-muted-foreground">{item.anchors.length} 条</span></div><EvidenceCards item={item} /></section>
    <Alert><ShieldCheckIcon /><AlertTitle>审核边界</AlertTitle><AlertDescription>通过要求本群可解析证据；拒绝会把规范化词形写入所有群共享的全局拉黑列表，阻止后续理解、入库与注入。</AlertDescription></Alert>
    <div className="flex flex-wrap gap-2 border-t pt-4"><Button disabled={busy || !reviewAvailable || item.status !== 'pending' || item.anchors.length === 0} onClick={() => onReview('approve')}><CheckIcon data-icon="inline-start" />通过审核</Button><Button variant="outline" disabled={busy || !reviewAvailable || item.status !== 'pending'} onClick={() => onReview('reject')}><XIcon data-icon="inline-start" />拒绝并全局拉黑</Button></div>
    <details className="rounded-lg border bg-muted/20 p-3"><summary className="cursor-pointer font-medium">技术字段与完整证据引用</summary><div className="mt-4 flex flex-col gap-4"><dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">修订版本</dt><dd className="font-mono">{item.revision}</dd></div><div><dt className="text-muted-foreground">审核状态</dt><dd className="break-all font-mono">{item.review_status}</dd></div><div><dt className="text-muted-foreground">来源</dt><dd className="break-all font-mono">{item.source}</dd></div><div><dt className="text-muted-foreground">规则版本</dt><dd className="break-all font-mono">{item.rule_version ?? '未记录'}</dd></div><div className="sm:col-span-2"><dt className="text-muted-foreground">当前群</dt><dd className="break-all font-mono">{item.bot_id} · {item.session_id}</dd></div></dl><EvidenceList evidence={item.anchors} emptyDescription="该条目没有可解析证据锚点，无法安全通过。" />{item.object_ref ? <ObjectDeepLink to="/jargon" objectRef={item.object_ref}>复制可复现跳转链接</ObjectDeepLink> : null}<details className="rounded-md border bg-background p-3"><summary className="cursor-pointer text-sm">查看晋升记录 JSON</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item.promotion ?? {}, null, 2)}</pre></details></div></details>
  </div>
}

function JargonEvidenceDialog({ item, scope, onClose }: { item: JargonItem | null; scope: JargonScopeSelection; onClose: () => void }) {
  const [before, setBefore] = useState(15)
  const [after, setAfter] = useState(15)
  const [payload, setPayload] = useState<JargonEvidencePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const evidenceRequest = useRef(0)

  const loadEvidence = useCallback(async (windowBefore: number, windowAfter: number) => {
    if (!item) return
    const request = ++evidenceRequest.current
    setLoading(true)
    setError('')
    try {
      const next = await getJargonEvidence(item, scope, windowBefore, windowAfter)
      if (request === evidenceRequest.current) setPayload(next)
    } catch (reason) {
      if (request !== evidenceRequest.current) return
      setPayload(null)
      setError(humanizeApiError(reason, '黑话证据加载失败'))
    } finally {
      if (request === evidenceRequest.current) setLoading(false)
    }
  }, [item, scope])

  useEffect(() => {
    if (!item) {
      evidenceRequest.current += 1
      setPayload(null)
      setLoading(false)
      setError('')
      return
    }
    setBefore(15)
    setAfter(15)
    void loadEvidence(15, 15)
  }, [item, loadEvidence])

  const beforeCount = payload?.messages.filter((message) => message.role === 'before').length ?? 0
  const afterCount = payload?.messages.filter((message) => message.role === 'after').length ?? 0

  return <Dialog open={Boolean(item)} onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent className="flex h-[min(80vh,760px)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
      <DialogHeader className="border-b p-4 pr-12">
        <DialogTitle>黑话证据{item ? ` · ${item.word}` : ''}</DialogTitle>
        <DialogDescription>只还原当前 Bot 和群里这条黑话锚点的前后聊天，不会用裸编号或旧群号跨群查找。</DialogDescription>
      </DialogHeader>
      <div className="flex flex-wrap items-end gap-3 border-b bg-muted/30 p-3">
        <Field className="w-24 gap-1">
          <FieldLabel htmlFor="jargon-evidence-before">前文条数</FieldLabel>
          <Input id="jargon-evidence-before" type="number" min="0" max="50" value={before} onChange={(event) => setBefore(Number(event.target.value) || 0)} />
        </Field>
        <Field className="w-24 gap-1">
          <FieldLabel htmlFor="jargon-evidence-after">后文条数</FieldLabel>
          <Input id="jargon-evidence-after" type="number" min="0" max="50" value={after} onChange={(event) => setAfter(Number(event.target.value) || 0)} />
        </Field>
        <Button type="button" size="sm" disabled={loading || !item} onClick={() => void loadEvidence(before, after)}>
          {loading ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <MessageSquareQuoteIcon data-icon="inline-start" />}
          刷新证据
        </Button>
        {payload ? <div className="ml-auto flex flex-wrap gap-2"><Badge variant={payload.used_fallback ? 'outline' : 'secondary'}>{payload.used_fallback ? '保存的回退上下文' : '同作用域动态上下文'}</Badge><Badge variant="outline">前 {beforeCount} / 后 {afterCount}</Badge><Badge variant="outline">锚点 {payload.anchor?.id ?? '无'}</Badge></div> : null}
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-3 p-4">
          {loading ? <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在读取同作用域证据上下文</div> : null}
          {!loading && error ? <Alert variant="destructive"><AlertTitle>证据读取失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
          {!loading && payload?.messages.length ? payload.messages.map((message) => <article key={`${message.role}:${message.id}`} data-evidence-role={message.role} className={message.role === 'anchor' ? 'rounded-lg border border-primary/30 bg-primary/5 p-3' : 'rounded-lg border bg-card p-3'}><div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{message.sender_name || message.sender_id || '发送者未记录'}</span><span>{formatTime(message.timestamp)}</span></div><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p>{message.role === 'anchor' ? <Badge className="mt-2" variant="secondary">黑话提取锚点</Badge> : null}</article>) : null}
          {!loading && payload && !payload.messages.length && payload.fallback_contexts.length ? <div className="flex flex-col gap-3"><Alert><ShieldCheckIcon /><AlertTitle>动态锚点不可用，展示该黑话保存的回退上下文</AlertTitle><AlertDescription>这些内容来自这条黑话保存的内容本身，不会根据不完整的会话标识猜测其他群。</AlertDescription></Alert>{payload.fallback_contexts.map((context, index) => <article key={`${index}:${context}`} className="rounded-lg border bg-card p-3 text-sm leading-6 whitespace-pre-wrap break-words">{context}</article>)}</div> : null}
          {!loading && payload && !payload.messages.length && !payload.fallback_contexts.length ? <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">该黑话没有可安全还原的上下文证据。</div> : null}
        </div>
      </ScrollArea>
    </DialogContent>
  </Dialog>
}


export function JargonPage() {
  const pagination = usePaginationSearchParams()
  const [params] = useSearchParams()
  const botId = params.get('bot_id') ?? ''
  const sessionId = params.get('session_id') ?? ''
  const visibility = params.get('visibility') ?? 'group'
  const objectRef = params.get('ref') ?? ''
  const objectId = params.get('object_id') ?? ''
  const statusFilter = params.get('status') ?? ''
  const search = params.get('search') ?? ''
  const sourceFilter = params.get('source') ?? ''
  const evidenceFilter = params.get('has_evidence') ?? ''
  const minFrequency = params.get('min_frequency') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const scope = useMemo<JargonScopeSelection>(() => ({ bot_id: botId, session_id: sessionId, visibility: 'group' }), [botId, sessionId])
  const [payload, setPayload] = useState<JargonResponse | null>(null)
  const [deepLinkedItem, setDeepLinkedItem] = useState<JargonItem | null>(null)
  const [deepLinkStatus, setDeepLinkStatus] = useState<'loading' | ObjectRefState | null>(null)
  const [queryStatus, setQueryStatus] = useState<'loading' | 'success' | 'empty' | 'error'>('empty')
  const [error, setError] = useState<unknown>()
  const [mutating, setMutating] = useState<number | null>(null)
  const [batchMutating, setBatchMutating] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [evidenceItem, setEvidenceItem] = useState<JargonItem | null>(null)
  const [editItem, setEditItem] = useState<JargonItem | null>(null)
  const [editMeaning, setEditMeaning] = useState('')
  const [archiveItem, setArchiveItem] = useState<JargonItem | null>(null)
  const [blocklist, setBlocklist] = useState<JargonBlocklistItem[]>([])
  const [blocklistStatus, setBlocklistStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [blocklistError, setBlocklistError] = useState<unknown>(null)
  const [removingBlocklistId, setRemovingBlocklistId] = useState<number | null>(null)
  const [filterDraft, setFilterDraft] = useState({ search, status: statusFilter, source: sourceFilter, hasEvidence: evidenceFilter, minFrequency })
  const [activeTab, setActiveTab] = useState<'local' | 'global'>('local')
  const listRequest = useRef(0)
  const blocklistRequest = useRef(0)

  useEffect(() => setFilterDraft({ search, status: statusFilter, source: sourceFilter, hasEvidence: evidenceFilter, minFrequency }), [evidenceFilter, minFrequency, search, sourceFilter, statusFilter])
  useEffect(() => {
    setEvidenceItem(null)
    setEditItem(null)
    setArchiveItem(null)
    setSelectedIds([])
  }, [botId, sessionId])
  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId), [botId])

  const load = useCallback(async () => {
    const request = ++listRequest.current
    if (!botId || !sessionId) { setPayload(null); setQueryStatus('empty'); return }
    setQueryStatus('loading')
    setError(undefined)
    try {
      const next = normalizeJargonResponse(await listJargons({
        ...scope,
        limit: pagination.limit,
        offset: pagination.offset,
        status: statusFilter || undefined,
        search: search || undefined,
        source: sourceFilter || undefined,
        has_evidence: evidenceFilter || undefined,
        min_frequency: minFrequency || undefined,
      }))
      if (request !== listRequest.current) return
      setPayload(next)
      setSelectedIds([])
      setQueryStatus(next.items.length ? 'success' : 'empty')
    } catch (reason) {
      if (request !== listRequest.current) return
      setPayload(null); setError(reason); setQueryStatus('error')
    }
  }, [botId, evidenceFilter, minFrequency, pagination.limit, pagination.offset, scope, search, sessionId, sourceFilter, statusFilter])

  const loadBlocklist = useCallback(async () => {
    const request = ++blocklistRequest.current
    setBlocklistError(null)
    setBlocklistStatus('loading')
    try {
      const next = await listJargonBlocklist()
      if (request !== blocklistRequest.current) return
      setBlocklist(Array.isArray(next.items) ? next.items : [])
      setBlocklistStatus('success')
    } catch (reason) {
      if (request !== blocklistRequest.current) return
      setBlocklist([])
      setBlocklistError(reason)
      setBlocklistStatus('error')
    }
  }, [])

  useEffect(() => { if (activeTab === 'local') { void load(); void loadBlocklist() } }, [activeTab, load, loadBlocklist])
  useEffect(() => {
    if (!objectRef) { setDeepLinkedItem(null); setDeepLinkStatus(null); return }
    if (!botId || !sessionId || visibility !== 'group') { setDeepLinkedItem(null); setDeepLinkStatus('scope-mismatch'); return }
    if (objectId && !/^\d+$/.test(objectId)) { setDeepLinkedItem(null); setDeepLinkStatus('not-found'); return }
    const query = new URLSearchParams({ ref: objectRef, ...scope })
    const endpoint = objectId ? `/api/jargon/${objectId}?${query.toString()}` : `/api/jargon/resolve?${query.toString()}`
    let cancelled = false
    setDeepLinkStatus('loading')
    fetchJson<{ item: JargonItem }>(endpoint).then((result) => {
      if (cancelled) return
      setDeepLinkedItem(normalizeJargonItem(result.item))
      setDeepLinkStatus('ready')
    }).catch((reason) => {
      if (cancelled) return
      setDeepLinkedItem(null)
      setDeepLinkStatus(deepLinkFailureState(reason))
    })
    return () => { cancelled = true }
  }, [botId, objectId, objectRef, scope, sessionId, visibility])

  async function review(item: JargonItem, action: 'approve' | 'reject') {
    setMutating(item.id)
    try {
      const result = await reviewJargon(item, action, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认审核命令成功')
      toast.success(action === 'approve' ? '候选已通过证据审核' : '候选已拒绝并全局拉黑')
      await Promise.all([load(), loadBlocklist()])
    } catch (reason) { toast.error(humanizeApiError(reason, '审核失败')) }
    finally { setMutating(null) }
  }

  async function reviewSelected(action: 'approve' | 'reject') {
    const selected = (payload?.items ?? []).filter((item) => selectedIds.includes(item.id))
    if (!selected.length) return
    setBatchMutating(true)
    try {
      const result = await batchReviewJargons(selected, action, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认批量审核成功')
      toast.success(action === 'approve' ? '已批量通过选择的黑话' : '已批量拒绝并全局拉黑')
      await Promise.all([load(), loadBlocklist()])
    } catch (reason) {
      toast.error(humanizeApiError(reason, '批量审核失败'))
    } finally {
      setBatchMutating(false)
    }
  }

  async function removeFromGlobalBlocklist(item: JargonBlocklistItem) {
    setRemovingBlocklistId(item.id)
    try {
      const result = await removeJargonBlocklistItem(item.id)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认解除全局拉黑')
      toast.success(`已解除“${item.word}”的手动全局拉黑`)
      await loadBlocklist()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '解除全局拉黑失败'))
    } finally {
      setRemovingBlocklistId(null)
    }
  }

  async function archiveSelected() {
    const selected = (payload?.items ?? []).filter((item) => selectedIds.includes(item.id))
    if (!selected.length) return
    setBatchMutating(true)
    try {
      const results = await Promise.all(selected.map((item) => archiveJargon(item, scope)))
      if (results.some((result) => !result.ok || result.operation.status !== 'succeeded')) {
        throw new Error('服务端未确认所有黑话归档成功')
      }
      toast.success(`已归档 ${selected.length} 条黑话记录，保留证据与审计信息`)
      setSelectedIds([])
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '批量归档失败'))
    } finally {
      setBatchMutating(false)
    }
  }

  async function saveMeaning() {
    if (!editItem) return
    setMutating(editItem.id)
    try {
      const result = await updateJargonMeaning(editItem, editMeaning, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认释义更新成功')
      toast.success('释义已更新，并重新进入待审核')
      setEditItem(null)
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '释义更新失败'))
    } finally {
      setMutating(null)
    }
  }

  async function archiveSelectedItem() {
    if (!archiveItem) return
    setMutating(archiveItem.id)
    try {
      const result = await archiveJargon(archiveItem, scope)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认归档成功')
      toast.success('黑话已归档')
      setArchiveItem(null)
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '归档失败'))
    } finally {
      setMutating(null)
    }
  }

  function openMeaningEditor(item: JargonItem) {
    setEditItem(item)
    setEditMeaning(item.meaning || '')
  }

  function toggleSelected(id: number, checked: boolean) {
    setSelectedIds((current) => checked ? [...current, id] : current.filter((value) => value !== id))
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    pagination.setFilters({
      search: filterDraft.search.trim() || null,
      status: filterDraft.status || null,
      source: filterDraft.source || null,
      has_evidence: filterDraft.hasEvidence || null,
      min_frequency: filterDraft.minFrequency.trim() || null,
    })
  }

  function resetFilters() {
    setFilterDraft({ search: '', status: '', source: '', hasEvidence: '', minFrequency: '' })
    pagination.setFilters({ search: null, status: null, source: null, has_evidence: null, min_frequency: null })
  }

  const promote = async (item: JargonItem) => {
    setMutating(item.id)
    try {
      await promoteJargonToGlobal(item, scope)
      toast.success(`已把「${item.word}」提升为广域黑话`)
    } catch (reason) {
      toast.error(humanizeApiError(reason, '提升为广域黑话失败'))
    } finally {
      setMutating(null)
    }
  }

  const pageItems = payload?.items ?? []
  const selectedItems = pageItems.filter((item) => selectedIds.includes(item.id))
  const allPageSelected = pageItems.length > 0 && selectedItems.length === pageItems.length
  const confirmedCount = payload ? pageItems.filter((item) => item.status === 'confirmed').length : '—'
  const pendingCount = payload ? pageItems.filter((item) => item.status === 'pending').length : '—'
  const anchorCount = payload ? pageItems.reduce((sum, item) => sum + item.anchors.length, 0) : '—'
  const totalText = payload?.page.total_status === 'exact' && payload.page.total !== null ? payload.page.total : '—'
  const reviewAvailable = payload?.capabilities.review?.available === true
  const editAvailable = payload?.capabilities.edit?.available === true
  const archiveAvailable = payload?.capabilities.archive?.available === true

  return <div data-slot="jargon-page" className="flex flex-col gap-6">
    <Card className="overflow-hidden border-primary/10 bg-gradient-to-br from-primary/5 via-card to-card"><CardHeader className="gap-4 sm:flex-row sm:items-start sm:justify-between"><div className="flex gap-3"><div className="rounded-xl bg-primary/10 p-3 text-primary"><MessageSquareQuoteIcon className="size-6" /></div><div><CardTitle className="text-xl">群聊黑话与广域资产</CardTitle><CardDescription className="mt-1 max-w-3xl">本群已确认黑话、待审核候选与内置广域参考资产严格分层；昵称、普通词和技术噪声不进入默认候选。</CardDescription></div></div><div className="flex items-center gap-2">{reviewAvailable ? <Badge variant="outline" className="border-emerald-500/25 bg-emerald-500/5 text-emerald-600"><ShieldCheckIcon className="size-3.5 mr-1" />证据审核中</Badge> : <Badge variant="outline" className="border-amber-500/25 bg-amber-500/5 text-amber-600"><LockIcon className="size-3.5 mr-1" />只读模式</Badge>}</div></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><AuditMetric label="匹配总数" value={totalText} /><AuditMetric label="本页已确认" value={confirmedCount} /><AuditMetric label="本页待审核" value={pendingCount} /><AuditMetric label="本页证据锚点" value={anchorCount} /></CardContent></Card>

    {deepLinkStatus ? <Alert data-slot="jargon-deep-link-state" variant={deepLinkStatus === 'ready' || deepLinkStatus === 'loading' ? 'default' : 'destructive'}><AlertTitle>{deepLinkStatus === 'loading' ? '正在校验跳转链接' : deepLinkStatus === 'ready' ? '已定位这条黑话' : '无法打开这条黑话'}</AlertTitle><AlertDescription>{deepLinkStatus === 'ready' && deepLinkedItem ? <span><strong>{deepLinkedItem.word}</strong>：{deepLinkedItem.meaning || '尚未形成释义'}</span> : deepLinkStatus === 'loading' ? '正在验证当前群和版本是否还对得上。' : DEEP_LINK_LABELS[deepLinkStatus as Exclude<ObjectRefState, 'ready'>]}</AlertDescription></Alert> : null}

    <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as 'local' | 'global')}><TabsList><TabsTrigger value="local"><BookOpenIcon />群聊黑话</TabsTrigger><TabsTrigger value="global"><Globe2Icon />广域黑话</TabsTrigger></TabsList>
      <TabsContent value="local" className="flex flex-col gap-4">
        <ScopeFilterBar
          botId={botId}
          sessionId={sessionId}
          loadBots={loadBots}
          loadSessions={loadSessions}
          onBotChange={(val) => pagination.setFilters({ bot_id: val, session_id: null })}
          onSessionChange={(val) => pagination.setFilters({ session_id: val })}
          searchValue={filterDraft.search}
          onSearchChange={(val) => setFilterDraft((current) => ({ ...current, search: val }))}
          searchPlaceholder="搜索黑话或释义…"
          onSubmit={submitSearch}
          onReset={resetFilters}
        >
          <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
            <FieldLabel>审核状态</FieldLabel>
            <Select
              value={filterDraft.status || 'all'}
              onValueChange={(value) => setFilterDraft((current) => ({ ...current, status: value === 'all' ? '' : value }))}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部状态</SelectItem>
                <SelectItem value="confirmed">已确认</SelectItem>
                <SelectItem value="pending">待审核</SelectItem>
                <SelectItem value="rejected">已拒绝</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
            <FieldLabel>来源</FieldLabel>
            <Select value={filterDraft.source || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, source: value === 'all' ? '' : value }))}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部来源</SelectItem>
                <SelectItem value="wave_memory">本群习得</SelectItem>
                <SelectItem value="holyman_skills">内置资产</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field className="w-32 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
            <FieldLabel>证据</FieldLabel>
            <Select value={filterDraft.hasEvidence || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, hasEvidence: value === 'all' ? '' : value }))}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">不限证据</SelectItem>
                <SelectItem value="yes">有证据锚点</SelectItem>
                <SelectItem value="no">无证据锚点</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field className="w-28 shrink-0 gap-0 [&_[data-slot=field-label]]:sr-only">
            <FieldLabel>最低频次</FieldLabel>
            <Input className="h-8" inputMode="numeric" placeholder="最低频次" value={filterDraft.minFrequency} onChange={(event) => setFilterDraft((current) => ({ ...current, minFrequency: event.target.value }))} />
          </Field>
        </ScopeFilterBar>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">群聊黑话清单</CardTitle>
            <CardDescription>主表只显示中文业务字段；来源、规则、对象引用和 JSON 收纳在详情中。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <BatchActionBar
              selectedCount={selectedItems.length}
              disabled={batchMutating}
              onClear={() => setSelectedIds([])}
            >
              <Button
                type="button"
                size="sm"
                disabled={batchMutating || selectedItems.some((item) => item.anchors.length === 0 || !item.object_ref)}
                onClick={() => void reviewSelected('approve')}
              >
                <CheckIcon data-icon="inline-start" />
                批量通过
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={batchMutating || selectedItems.some((item) => !item.object_ref)}
                onClick={() => void reviewSelected('reject')}
              >
                <XIcon data-icon="inline-start" />
                批量拒绝并全局拉黑
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={batchMutating || !archiveAvailable || selectedItems.some((item) => !item.object_ref)}
                onClick={() => void archiveSelected()}
              >
                <ArchiveIcon data-icon="inline-start" />
                批量归档
              </Button>
            </BatchActionBar>
<QueryState status={queryStatus} error={error} onRetry={() => void load()} title={!botId || !sessionId ? '请选择真实 Bot 与会话' : undefined} description={!botId || !sessionId ? '作用域未选择时不会查询，也不会补入默认 Bot。' : undefined}>
<ResponsiveTable label="群聊黑话清单" table={<Table className="w-full table-fixed">
<TableHeader><TableRow><TableHead className="w-8"><input aria-label="选择当前页全部黑话" type="checkbox" checked={allPageSelected} onChange={(event) => setSelectedIds(event.target.checked ? pageItems.map((item) => item.id) : [])} /></TableHead><TableHead className="w-28">词条</TableHead><TableHead className="w-auto">释义</TableHead><TableHead className="w-16">频次</TableHead><TableHead className="w-20">来源</TableHead><TableHead className="w-20">状态</TableHead><TableHead className="w-16">证据</TableHead><TableHead className="w-52 text-right">操作</TableHead></TableRow></TableHeader>
<TableBody>{pageItems.map((item) => <TableRow key={item.id} className={selectedIds.includes(item.id) ? 'bg-primary/5' : undefined}><TableCell><input aria-label={`选择黑话 ${item.word}`} type="checkbox" checked={selectedIds.includes(item.id)} onChange={(event) => toggleSelected(item.id, event.target.checked)} /></TableCell><TableCell className="font-semibold truncate">{item.word}</TableCell><TableCell className="whitespace-normal break-words"><p className="line-clamp-2 text-sm text-muted-foreground">{item.meaning || '尚未形成可展示的释义'}</p></TableCell><TableCell className="tabular-nums">{item.frequency}</TableCell><TableCell><Badge variant="secondary" className="font-mono text-[10px]">{item.source || 'wave_memory'}</Badge></TableCell><TableCell><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge></TableCell><TableCell>{item.anchors.length ? `${item.anchors.length} 条` : '无锚点'}</TableCell><TableCell className="text-right"><div className="flex justify-end gap-1"><Button type="button" variant="outline" size="sm" disabled={!item.object_ref} title={item.object_ref ? '还原同作用域聊天证据' : '缺少服务端签发的 ObjectRef'} onClick={() => setEvidenceItem(item)}><MessageSquareQuoteIcon data-icon="inline-start" />证据</Button>{item.status === 'pending' ? <><Button type="button" size="icon-sm" aria-label={`确认黑话 ${item.word}`} disabled={mutating === item.id || !reviewAvailable || item.anchors.length === 0} title="确认黑话" onClick={() => void review(item, 'approve')}><CheckIcon /></Button><Button type="button" variant="outline" size="icon-sm" aria-label={`拒绝并全局拉黑黑话 ${item.word}`} disabled={mutating === item.id || !reviewAvailable} title="拒绝并全局拉黑" onClick={() => void review(item, 'reject')}><XIcon /></Button></> : null}<Button type="button" variant="ghost" size="icon-sm" aria-label={`编辑黑话 ${item.word}`} disabled={!editAvailable || !item.object_ref} title={editAvailable ? '编辑释义；保存后回到待审核' : payload?.capabilities.edit?.reason_code ?? '编辑不可用'} onClick={() => openMeaningEditor(item)}><Edit2Icon /></Button><ResponsiveDetail title={item.word} description="黑话释义、证据引用与审核操作" className="sm:max-w-4xl" trigger={<Button type="button" variant="ghost" size="icon-sm" aria-label={`查看黑话 ${item.word} 详情`} title="查看详情"><EyeIcon /></Button>}><JargonDetails item={item} reviewAvailable={reviewAvailable} busy={mutating === item.id} onReview={(action) => void review(item, action)} /></ResponsiveDetail>{item.status === 'confirmed' ? <Button type="button" variant="ghost" size="icon-sm" aria-label={`把黑话 ${item.word} 提升为广域`} title={item.object_ref ? '提升为该 Bot 的广域黑话，所有群可用' : '缺少服务端签发的 ObjectRef'} disabled={mutating === item.id || !item.object_ref} onClick={() => void promote(item)}><Globe2Icon /></Button> : null}<Button type="button" variant="ghost" size="icon-sm" aria-label={`归档黑话 ${item.word}`} disabled={!archiveAvailable || !item.object_ref} title={archiveAvailable ? '归档并移出正式注入集合' : payload?.capabilities.archive?.reason_code ?? '归档不可用'} onClick={() => setArchiveItem(item)}><ArchiveIcon /></Button></div></TableCell></TableRow>)}</TableBody>
</Table>} cards={pageItems.map((item) => <article key={item.id} className={`flex flex-col gap-3 rounded-lg border bg-card p-4 ${selectedIds.includes(item.id) ? 'border-primary/50 bg-primary/5' : ''}`}><div className="flex items-start justify-between gap-2"><label className="flex min-w-0 items-start gap-2"><input aria-label={`选择黑话 ${item.word}`} type="checkbox" checked={selectedIds.includes(item.id)} onChange={(event) => toggleSelected(item.id, event.target.checked)} /><span><span className="block font-semibold">{item.word}</span><span className="mt-1 block whitespace-pre-wrap break-words text-sm text-muted-foreground">{item.meaning || '尚未形成可展示的释义'}</span></span></label><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge></div><div className="flex flex-wrap gap-2 text-xs text-muted-foreground"><span>频次 {item.frequency}</span><span>来源 {item.source || 'wave_memory'}</span><span>证据 {item.anchors.length} 条</span></div><div className="flex flex-wrap justify-end gap-2"><Button type="button" variant="outline" size="sm" disabled={!item.object_ref} onClick={() => setEvidenceItem(item)}><MessageSquareQuoteIcon data-icon="inline-start" />证据</Button><Button type="button" variant="outline" size="sm" disabled={!editAvailable || !item.object_ref} onClick={() => openMeaningEditor(item)}><Edit2Icon data-icon="inline-start" />编辑</Button>{item.status === 'confirmed' ? <Button type="button" variant="outline" size="sm" disabled={mutating === item.id || !item.object_ref} onClick={() => void promote(item)}>提升为广域</Button> : null}<ResponsiveDetail title={item.word} description="黑话释义、证据引用与审核操作" className="sm:max-w-4xl" trigger={<Button type="button" variant="outline" size="sm">详情</Button>}><JargonDetails item={item} reviewAvailable={reviewAvailable} busy={mutating === item.id} onReview={(action) => void review(item, action)} /></ResponsiveDetail></div></article>)} />
</QueryState>{payload && !payload.capabilities.review?.available ? <Alert>
<AlertTitle>审核能力当前不可用</AlertTitle>
<AlertDescription>服务端拒绝原因：{humanizeReason(payload.capabilities.review?.reason_code, '未提供')}</AlertDescription>
</Alert> : null}{payload ? <PaginationControls page={payload.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} /> : null}</CardContent>
</Card>

        <Card>
          <CardHeader><CardTitle className="text-base">全局黑话拉黑列表</CardTitle><CardDescription>所有群共享，展示规范化词形、来源、原因与时间；只有用户审核产生的手动项可在此解除。</CardDescription></CardHeader>
          <CardContent>
            <QueryState status={blocklistStatus} title="全局拉黑列表暂不可用" error={blocklistError} description="拉黑检查仍在服务端 fail-closed；此处仅为审计与解除入口。" onRetry={() => void loadBlocklist()}>
              {blocklist.length ? <ResponsiveTable label="全局黑话拉黑列表" table={<Table><TableHeader><TableRow><TableHead>规范化词形</TableHead><TableHead>原因</TableHead><TableHead>来源</TableHead><TableHead>记录时间</TableHead><TableHead className="w-24 text-right">操作</TableHead></TableRow></TableHeader><TableBody>{blocklist.map((item) => <TableRow key={item.id}><TableCell className="font-semibold">{item.word}</TableCell><TableCell className="text-sm text-muted-foreground">{item.reason}</TableCell><TableCell><Badge variant="outline" className="font-mono text-[10px]">{item.source}</Badge></TableCell><TableCell className="text-sm text-muted-foreground">{item.created_at ? formatTime(item.created_at) : '未记录'}</TableCell><TableCell className="text-right"><Button type="button" size="sm" variant="outline" disabled={item.source !== 'user_global_reject' || removingBlocklistId === item.id} title={item.source === 'user_global_reject' ? '解除手动全局拉黑' : 'Holyman 同步项不可手动删除'} onClick={() => void removeFromGlobalBlocklist(item)}>{removingBlocklistId === item.id ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <XIcon data-icon="inline-start" />}解除</Button></TableCell></TableRow>)}</TableBody></Table>} cards={blocklist.map((item) => <article key={item.id} className="flex flex-col gap-3 rounded-lg border bg-card p-4"><div className="flex items-start justify-between gap-3"><div><p className="font-semibold">{item.word}</p><p className="mt-1 text-sm text-muted-foreground">{item.reason}</p></div><Badge variant="outline" className="font-mono text-[10px]">{item.source}</Badge></div><div className="flex items-center justify-between gap-2 text-xs text-muted-foreground"><span>{item.created_at ? formatTime(item.created_at) : '未记录时间'}</span><Button type="button" size="sm" variant="outline" disabled={item.source !== 'user_global_reject' || removingBlocklistId === item.id} onClick={() => void removeFromGlobalBlocklist(item)}>解除全局拉黑</Button></div></article>)} /> : <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">当前没有全局拉黑词形。</div>}
            </QueryState>
          </CardContent>
        </Card>
      </TabsContent>

      <TabsContent value="global"><GlobalJargonPanel botId={botId} /></TabsContent>
    </Tabs>

    <Dialog open={Boolean(editItem)} onOpenChange={(open) => { if (!open && mutating === null) setEditItem(null) }}><DialogContent className="sm:max-w-lg"><DialogHeader><DialogTitle>编辑黑话释义{editItem ? ` · ${editItem.word}` : ''}</DialogTitle><DialogDescription>保存后该词条将重新进入待审核状态。</DialogDescription></DialogHeader><Field><FieldLabel htmlFor="jargon-edit-meaning">黑话释义</FieldLabel><Textarea id="jargon-edit-meaning" className="min-h-28" value={editMeaning} onChange={(event) => setEditMeaning(event.target.value)} /></Field><DialogFooter><Button type="button" variant="outline" disabled={mutating !== null} onClick={() => setEditItem(null)}>取消</Button><Button type="button" disabled={mutating !== null || !editMeaning.trim()} onClick={() => void saveMeaning()}>{mutating !== null ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <Edit2Icon data-icon="inline-start" />}保存并回到待审核</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(archiveItem)} onOpenChange={(open) => { if (!open && mutating === null) setArchiveItem(null) }}><DialogContent><DialogHeader><DialogTitle>归档黑话{archiveItem ? ` · ${archiveItem.word}` : ''}</DialogTitle><DialogDescription>将黑话移出正式注入列表，历史记录与证据依然保留。</DialogDescription></DialogHeader><Alert><ArchiveIcon /><AlertTitle>可随时恢复</AlertTitle><AlertDescription>归档后的黑话可随时在已归档列表中恢复，不会丢失数据。</AlertDescription></Alert><DialogFooter><Button type="button" variant="outline" disabled={mutating !== null} onClick={() => setArchiveItem(null)}>取消</Button><Button type="button" variant="destructive" disabled={mutating !== null} onClick={() => void archiveSelectedItem()}>{mutating !== null ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <ArchiveIcon data-icon="inline-start" />}确认归档</Button></DialogFooter></DialogContent></Dialog>
    <JargonEvidenceDialog item={evidenceItem} scope={scope} onClose={() => setEvidenceItem(null)} />
  </div>
}

export default JargonPage

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { AlertCircleIcon, ArchiveIcon, BookOpenIcon, CheckCircle2Icon, CheckIcon, DatabaseIcon, Edit2Icon, EyeIcon, Globe2Icon, Loader2Icon, MessageSquareQuoteIcon, RefreshCwIcon, ShieldCheckIcon, XIcon, LockIcon } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { fetchJson } from '@/api/client'
import { archiveJargon, batchReviewJargons, checkHolymanUpdate, getCatalogAudit, getJargonEvidence, listJargonBlocklist, listJargons, previewHolymanSync, removeJargonBlocklistItem, reviewJargon, updateJargonMeaning, type CatalogAssetRecord, type CatalogAuditPayload, type HolymanSyncPreviewPayload, type HolymanUpdateCheckPayload, type JargonBlocklistItem, type JargonEvidencePayload, type JargonItem, type JargonResponse, type JargonScopeSelection } from '@/api/jargon'
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

function textOf(item: CatalogAssetRecord, keys: string[], fallback = '—') {
  for (const key of keys) {
    const value = item[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number') return String(value)
  }
  return fallback
}

function listOf(item: CatalogAssetRecord, key: string) {
  const value = item[key]
  return Array.isArray(value) ? value.map(String).filter(Boolean) : []
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
      setError(reason instanceof Error ? reason.message : '黑话证据加载失败')
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

function CatalogRecordDetail({ title, item }: { title: string; item: CatalogAssetRecord }) {
  const tags = listOf(item, 'linked_terms')
  return <div className="flex flex-col gap-4"><div><h3 className="text-lg font-semibold">{title}</h3><p className="mt-2 whitespace-pre-wrap leading-7 text-muted-foreground">{textOf(item, ['meaning', 'summary', 'content', 'text'], '当前资产没有可展示的正文。')}</p></div>{tags.length ? <div><p className="mb-2 text-sm font-medium">关联词条</p><div className="flex flex-wrap gap-1">{tags.map((tag) => <Badge key={tag} variant="secondary">{tag}</Badge>)}</div></div> : null}<details className="rounded-lg border bg-muted/20 p-3"><summary className="cursor-pointer font-medium">技术字段与原始 JSON</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item, null, 2)}</pre></details></div>
}

function CatalogTable({ items, titleKeys, summaryKeys, emptyText, phrases = false }: { items: CatalogAssetRecord[]; titleKeys: string[]; summaryKeys: string[]; emptyText: string; phrases?: boolean }) {
  if (!items.length) return <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">{emptyText}</div>
  return <ResponsiveTable label="内置广域资产清单" table={<Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>内容摘要</TableHead>{phrases ? <TableHead className="w-40">分类 / 状态</TableHead> : null}<TableHead className="w-24 text-right">详情</TableHead></TableRow></TableHeader><TableBody>{items.map((item, index) => { const title = textOf(item, titleKeys, `资产 ${index + 1}`); return <TableRow key={`${title}:${index}`}><TableCell className="font-medium">{title}</TableCell><TableCell className="max-w-2xl"><p className="line-clamp-2 text-sm text-muted-foreground">{textOf(item, summaryKeys)}</p></TableCell>{phrases ? <TableCell><div className="flex flex-wrap items-center gap-1"><Badge variant="outline">{textOf(item, ['category_label', 'category'], '未分类')}</Badge><Badge variant={item.is_activated ? 'secondary' : 'outline'}>{item.is_activated ? '已启用' : '未启用'}</Badge><Button type="button" size="icon-sm" variant="ghost" disabled title="catalog_mutation_disabled"><RefreshCwIcon /></Button></div></TableCell> : null}<TableCell className="text-right"><ResponsiveDetail title={title} description="内置广域资产只读详情" className="sm:max-w-3xl" trigger={<Button type="button" variant="ghost" size="sm"><EyeIcon data-icon="inline-start" />查看</Button>}><CatalogRecordDetail title={title} item={item} /></ResponsiveDetail></TableCell></TableRow> })}</TableBody></Table>} cards={items.map((item, index) => { const title = textOf(item, titleKeys, `资产 ${index + 1}`); return <article key={`${title}:${index}`} className="flex flex-col gap-3 rounded-lg border bg-card p-4"><div><p className="font-medium">{title}</p><p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">{textOf(item, summaryKeys)}</p></div>{phrases ? <div className="flex flex-wrap gap-1"><Badge variant="outline">{textOf(item, ['category_label', 'category'], '未分类')}</Badge><Badge variant={item.is_activated ? 'secondary' : 'outline'}>{item.is_activated ? '已启用' : '未启用'}</Badge></div> : null}<ResponsiveDetail title={title} description="Holyman 广域资产只读详情" className="sm:max-w-3xl" trigger={<Button type="button" variant="outline" size="sm">查看详情</Button>}><CatalogRecordDetail title={title} item={item} /></ResponsiveDetail></article> })} />
}

function CatalogBrowser({ catalog }: { catalog: CatalogAuditPayload | null }) {
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('all')
  const [activationFilter, setActivationFilter] = useState('all')
  const [updateCheck, setUpdateCheck] = useState<HolymanUpdateCheckPayload | null>(null)
  const [updateChecking, setUpdateChecking] = useState(false)
  const [preview, setPreview] = useState<HolymanSyncPreviewPayload | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const normalized = search.trim().toLocaleLowerCase()
  const filter = useCallback((items: CatalogAssetRecord[] | undefined) => (items ?? []).filter((item) => !normalized || Object.values(item).some((value) => typeof value === 'string' && value.toLocaleLowerCase().includes(normalized))), [normalized])
  const categories = useMemo(() => Array.from(new Set((catalog?.phrases ?? []).map((item) => String(item.category ?? '')).filter(Boolean))).sort(), [catalog?.phrases])
  const phrases = filter(catalog?.phrases).filter((item) => (categoryFilter === 'all' || String(item.category) === categoryFilter) && (activationFilter === 'all' || (activationFilter === 'active') === Boolean(item.is_activated)))
  const concepts = filter(catalog?.concepts)
  const examples = filter(catalog?.examples)
  const corpus = filter(catalog?.corpus)
  const candidates = filter(catalog?.candidates)
  const blocked = useMemo<CatalogAssetRecord[]>(() => Object.entries(catalog?.blocked ?? {}).map(([word, reason]) => ({ word, reason })), [catalog?.blocked])
  const sourceCount = typeof catalog?.manifest_summary?.source_count === 'number' ? catalog.manifest_summary.source_count : null
  const parseStatuses = catalog?.manifest_summary?.parse_statuses
  const parsedCorpus = catalog?.quality_summary?.parsed_corpus_count
  const declaredCorpus = catalog?.quality_summary?.declared_corpus_count
  const errorCount = typeof catalog?.quality_summary?.error_count === 'number' ? catalog.quality_summary.error_count : null

  async function refreshUpdateCheck() {
    setUpdateChecking(true)
    try {
      const result = await checkHolymanUpdate(true)
      setUpdateCheck(result)
      toast.success(result.has_update ? '检测到内置资产远端更新' : '内置资产已是最新')
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '内置资产更新检查失败')
    } finally {
      setUpdateChecking(false)
    }
  }

  async function openSyncPreview() {
    setPreviewOpen(true)
    setPreviewLoading(true)
    setPreview(null)
    try {
      setPreview(await previewHolymanSync(true))
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '内置资产同步预览失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  return <div className="flex flex-col gap-5">
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><AuditMetric label="精选口癖" value={catalog?.phrases ? `${catalog.phrases.length} 条` : '未提供'} description="仅显式授权项可参与理解提示" /><AuditMetric label="文化概念" value={catalog?.concepts ? `${catalog.concepts.length} 组` : '未提供'} description="只读语境参考" /><AuditMetric label="声音样本与知识" value={catalog?.examples ? `${catalog.examples.length} 条` : '未提供'} description="只读语境参考" /><AuditMetric label="原始语料" value={catalog?.corpus ? `${catalog.corpus.length} 条` : typeof catalog?.corpus_summary?.count === 'number' ? `${catalog.corpus_summary.count} 条` : '未提供'} description="只读参考，不进入提示" /></div>

    <Card className="bg-muted/10"><CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between"><div><CardTitle className="text-base">广域资产审计</CardTitle><CardDescription>恢复旧版更新检查与同步预览；正式同步仍由安全契约禁用。</CardDescription></div><div className="flex flex-wrap items-center gap-2"><Button type="button" size="sm" variant="outline" disabled={updateChecking} onClick={() => void refreshUpdateCheck()}>{updateChecking ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <RefreshCwIcon data-icon="inline-start" />}检查更新</Button><Button type="button" size="sm" onClick={() => void openSyncPreview()}><EyeIcon data-icon="inline-start" />同步预览</Button><Button type="button" size="sm" variant="outline" disabled title="catalog_sync_command_unavailable">确认同步</Button><span className="text-xs text-muted-foreground">同步未开放，不是点击失败。</span></div></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><AuditMetric label="本地版本" value={updateCheck?.local_version || catalog?.local_version || '未知'} /><AuditMetric label="远端版本" value={updateCheck?.remote_version || catalog?.remote_version || '未知'} description={updateCheck ? updateCheck.has_update ? '检测到可用更新' : '当前已是最新' : undefined} /><AuditMetric label="清单来源文件" value={sourceCount ?? '未提供'} description={parseStatuses && typeof parseStatuses === 'object' ? `已记录 ${Object.values(parseStatuses).reduce<number>((sum, value) => sum + Number(value || 0), 0)} 个解析结果` : '暂无解析摘要'} /><AuditMetric label="语料核对" value={`${String(parsedCorpus ?? '—')} / ${String(declaredCorpus ?? '—')}`} description={errorCount === null ? '未提供质量错误计数' : errorCount > 0 ? `${errorCount} 个质量错误` : '服务端报告无质量错误'} /></CardContent></Card>

    <Alert><Globe2Icon /><AlertTitle>广域资产分层浏览</AlertTitle><AlertDescription>资产策略为“仅辅助理解”。更新检查与同步预览可用；开关、候选写入、拉黑写入和正式同步仍由安全契约禁用，并保留可见状态。</AlertDescription></Alert>

    <div className="flex flex-wrap items-end gap-2"><Field className="min-w-56 flex-1"><FieldLabel htmlFor="catalog-search">广域资产筛选</FieldLabel><Input id="catalog-search" value={search} placeholder="搜索口癖、概念、声音样本、语料或候选" onChange={(event) => setSearch(event.target.value)} /></Field><Field className="w-40"><FieldLabel>口癖分类</FieldLabel><Select value={categoryFilter} onValueChange={setCategoryFilter}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部分类</SelectItem>{categories.map((category) => <SelectItem key={category} value={category}>{category}</SelectItem>)}</SelectContent></Select></Field><Field className="w-36"><FieldLabel>启用状态</FieldLabel><Select value={activationFilter} onValueChange={setActivationFilter}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem><SelectItem value="active">已启用</SelectItem><SelectItem value="inactive">未启用</SelectItem></SelectContent></Select></Field></div>

    <Tabs defaultValue="phrases"><TabsList className="grid h-auto w-full grid-cols-2 gap-1 sm:grid-cols-3 xl:grid-cols-6"><TabsTrigger value="phrases">精选口癖</TabsTrigger><TabsTrigger value="concepts">文化概念</TabsTrigger><TabsTrigger value="examples">声音样本</TabsTrigger><TabsTrigger value="corpus">原始语料</TabsTrigger><TabsTrigger value="candidates">资产候选</TabsTrigger><TabsTrigger value="blocked">屏蔽项</TabsTrigger></TabsList>
      <TabsContent value="phrases" className="pt-3"><CatalogTable items={phrases} titleKeys={['word', 'title']} summaryKeys={['meaning', 'summary']} emptyText="没有符合筛选条件的精选口癖。" phrases /></TabsContent>
      <TabsContent value="concepts" className="pt-3"><CatalogTable items={concepts} titleKeys={['title', 'key', 'word']} summaryKeys={['summary', 'content', 'meaning']} emptyText="没有符合筛选条件的文化概念。" /></TabsContent>
      <TabsContent value="examples" className="pt-3"><CatalogTable items={examples} titleKeys={['title', 'word']} summaryKeys={['text', 'content', 'summary']} emptyText="没有符合筛选条件的声音样本与知识。" /></TabsContent>
      <TabsContent value="corpus" className="pt-3"><CatalogTable items={corpus} titleKeys={['title', 'source', 'line']} summaryKeys={['preview', 'text', 'content']} emptyText="没有符合筛选条件的原始语料。" /></TabsContent>
      <TabsContent value="candidates" className="pt-3"><Alert className="mb-3"><ShieldCheckIcon /><AlertTitle>资产候选审计</AlertTitle><AlertDescription className="flex flex-wrap items-center gap-2"><span>候选清单已恢复；正式全局资产审核命令尚未开放。</span><Button type="button" size="sm" disabled title="catalog_mutation_disabled">批量通过</Button><Button type="button" size="sm" variant="outline" disabled title="catalog_mutation_disabled">批量拒绝并全局拉黑</Button></AlertDescription></Alert><CatalogTable items={candidates} titleKeys={['word', 'title']} summaryKeys={['meaning', 'reason', 'summary']} emptyText="资产包中没有符合筛选条件的候选。" /></TabsContent>
      <TabsContent value="blocked" className="pt-3"><div className="mb-3 flex gap-2"><Input disabled placeholder="添加屏蔽词（正式命令未开放）" /><Button type="button" disabled title="catalog_mutation_disabled">添加屏蔽</Button></div><CatalogTable items={filter(blocked)} titleKeys={['word']} summaryKeys={['reason']} emptyText="没有符合筛选条件的屏蔽项。" /></TabsContent>
    </Tabs>

    <details className="rounded-lg border bg-muted/20 p-3"><summary className="cursor-pointer font-medium">技术字段与资产审计 JSON</summary><div className="mt-4 grid gap-4"><dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">资产类型</dt><dd className="break-all font-mono">{catalog?.asset_type ?? '未知'}</dd></div><div><dt className="text-muted-foreground">运行策略</dt><dd className="break-all font-mono">{catalog?.runtime_policy ?? '未知'}</dd></div><div><dt className="text-muted-foreground">资产状态</dt><dd className="break-all font-mono">{catalog?.asset_status ?? '未知'}</dd></div><div><dt className="text-muted-foreground">检查时间</dt><dd className="break-all font-mono">{catalog?.checked_at ?? '未知'}</dd></div><div><dt className="text-muted-foreground">原始语料策略</dt><dd className="font-mono">reference-only</dd></div><div><dt className="text-muted-foreground">精选匹配标记</dt><dd className="font-mono">runtime-match</dd></div><div><dt className="text-muted-foreground">同步能力代码</dt><dd className="break-all font-mono">catalog_sync_command_unavailable</dd></div></dl><details className="rounded-md border bg-background p-3"><summary className="cursor-pointer text-sm">manifest / quality report</summary><pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ manifest: catalog?.manifest, quality_report: catalog?.quality_report, corpus_counts: catalog?.corpus_counts }, null, 2)}</pre></details></div></details>
    <Dialog open={previewOpen} onOpenChange={setPreviewOpen}><DialogContent className="flex max-h-[86vh] flex-col sm:max-w-4xl"><DialogHeader><DialogTitle>内置资产同步预览</DialogTitle><DialogDescription>只读取远端并在内存中比较；当前正式同步仍禁用，不会从此弹窗写入资产。</DialogDescription></DialogHeader>{previewLoading ? <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在读取远端并生成差异</div> : preview ? <ScrollArea className="min-h-0 flex-1"><div className="flex flex-col gap-4 pr-3"><div className="grid gap-3 sm:grid-cols-2"><AuditMetric label="本地版本" value={preview.local_version || '未知'} description={preview.local_content_hash || '无 hash'} /><AuditMetric label="远端版本" value={preview.remote_version || '未知'} description={preview.remote_content_hash || '无 hash'} /></div><Alert variant={preview.will_update ? 'default' : undefined}>{preview.will_update ? <AlertCircleIcon /> : <CheckCircle2Icon />}<AlertTitle>{preview.will_update ? '检测到资产变化' : '没有资产变化'}</AlertTitle><AlertDescription>{preview.safety?.statement || '原始语料保持 reference-only，不直接进入 prompt。'}</AlertDescription></Alert><div className="overflow-auto rounded-lg border"><Table><TableHeader><TableRow><TableHead>资产层</TableHead><TableHead className="text-right">本地</TableHead><TableHead className="text-right">远端</TableHead><TableHead className="text-right">变化</TableHead></TableRow></TableHeader><TableBody>{Object.keys({ ...preview.local_counts, ...preview.remote_counts }).map((key) => <TableRow key={key}><TableCell>{key}</TableCell><TableCell className="text-right font-mono">{preview.local_counts[key] ?? 0}</TableCell><TableCell className="text-right font-mono">{preview.remote_counts[key] ?? 0}</TableCell><TableCell className="text-right font-mono">{preview.delta_counts[key] > 0 ? `+${preview.delta_counts[key]}` : preview.delta_counts[key] ?? 0}</TableCell></TableRow>)}</TableBody></Table></div><div className="grid gap-3 md:grid-cols-3">{([['新增口癖', preview.samples?.added_phrases ?? []], ['变更口癖', preview.samples?.changed_phrases ?? []], ['移除口癖', preview.samples?.removed_phrases ?? []]] as const).map(([title, words]) => <Card key={title}><CardHeader className="py-3"><CardTitle className="text-sm">{title}</CardTitle></CardHeader><CardContent className="flex flex-wrap gap-1 pt-0">{words.length ? words.slice(0, 12).map((word) => <Badge key={word} variant="secondary">{word}</Badge>) : <span className="text-sm text-muted-foreground">无变化</span>}</CardContent></Card>)}</div></div></ScrollArea> : <Alert variant="destructive"><AlertTitle>同步预览不可用</AlertTitle><AlertDescription>服务端没有返回可展示的差异。</AlertDescription></Alert>}<DialogFooter><Button type="button" variant="outline" onClick={() => setPreviewOpen(false)}>关闭</Button><Button type="button" disabled title="catalog_sync_command_unavailable">确认同步并写入</Button></DialogFooter></DialogContent></Dialog>
  </div>
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
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const scope = useMemo<JargonScopeSelection>(() => ({ bot_id: botId, session_id: sessionId, visibility: 'group' }), [botId, sessionId])
  const [payload, setPayload] = useState<JargonResponse | null>(null)
  const [deepLinkedItem, setDeepLinkedItem] = useState<JargonItem | null>(null)
  const [deepLinkStatus, setDeepLinkStatus] = useState<'loading' | ObjectRefState | null>(null)
  const [catalog, setCatalog] = useState<CatalogAuditPayload | null>(null)
  const [catalogStatus, setCatalogStatus] = useState<'loading' | 'success' | 'error'>('loading')
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
  const [removingBlocklistId, setRemovingBlocklistId] = useState<number | null>(null)
  const [filterDraft, setFilterDraft] = useState({ search, status: statusFilter })
  const [activeTab, setActiveTab] = useState<'local' | 'catalog'>('local')
  const listRequest = useRef(0)
  const blocklistRequest = useRef(0)
  const catalogRequest = useRef(0)

  useEffect(() => setFilterDraft({ search, status: statusFilter }), [search, statusFilter])
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
      const next = normalizeJargonResponse(await listJargons({ ...scope, limit: pagination.limit, offset: pagination.offset, status: statusFilter || undefined, search: search || undefined }))
      if (request !== listRequest.current) return
      setPayload(next)
      setSelectedIds([])
      setQueryStatus(next.items.length ? 'success' : 'empty')
    } catch (reason) {
      if (request !== listRequest.current) return
      setPayload(null); setError(reason); setQueryStatus('error')
    }
  }, [botId, pagination.limit, pagination.offset, scope, search, sessionId, statusFilter])

  const loadBlocklist = useCallback(async () => {
    const request = ++blocklistRequest.current
    setBlocklistStatus('loading')
    try {
      const next = await listJargonBlocklist()
      if (request !== blocklistRequest.current) return
      setBlocklist(Array.isArray(next.items) ? next.items : [])
      setBlocklistStatus('success')
    } catch {
      if (request !== blocklistRequest.current) return
      setBlocklist([])
      setBlocklistStatus('error')
    }
  }, [])

  const loadCatalog = useCallback(async () => {
    const request = ++catalogRequest.current
    setCatalogStatus('loading')
    try {
      const next = await getCatalogAudit()
      if (request !== catalogRequest.current) return
      setCatalog(next); setCatalogStatus('success')
    } catch {
      if (request !== catalogRequest.current) return
      setCatalog(null); setCatalogStatus('error')
    }
  }, [])

  useEffect(() => { if (activeTab === 'local') { void load(); void loadBlocklist() } }, [activeTab, load, loadBlocklist])
  useEffect(() => { if (activeTab === 'catalog') void loadCatalog() }, [activeTab, loadCatalog])
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
    } catch (reason) { toast.error(reason instanceof Error ? reason.message : '审核失败') }
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
      toast.error(reason instanceof Error ? reason.message : '批量审核失败')
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
      toast.error(reason instanceof Error ? reason.message : '解除全局拉黑失败')
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
      toast.error(reason instanceof Error ? reason.message : '批量归档失败')
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
      toast.error(reason instanceof Error ? reason.message : '释义更新失败')
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
      toast.success('黑话已从正式注入集合归档，未物理删除')
      setArchiveItem(null)
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '归档失败')
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
    pagination.setFilters({ search: filterDraft.search.trim() || null, status: filterDraft.status || null })
  }

  function resetFilters() {
    setFilterDraft({ search: '', status: '' })
    pagination.setFilters({ search: null, status: null })
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

    <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as 'local' | 'catalog')}><TabsList><TabsTrigger value="local"><BookOpenIcon />群聊黑话</TabsTrigger><TabsTrigger value="catalog"><Globe2Icon />内置广域资产</TabsTrigger></TabsList>
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
<ResponsiveTable label="群聊黑话清单" table={<Table>
<TableHeader><TableRow><TableHead className="w-10"><input aria-label="选择当前页全部黑话" type="checkbox" checked={allPageSelected} onChange={(event) => setSelectedIds(event.target.checked ? pageItems.map((item) => item.id) : [])} /></TableHead><TableHead>词条</TableHead><TableHead>释义</TableHead><TableHead className="w-20">频次</TableHead><TableHead className="w-24">来源</TableHead><TableHead className="w-24">状态</TableHead><TableHead className="w-20">证据</TableHead><TableHead className="w-64 text-right">操作</TableHead></TableRow></TableHeader>
<TableBody>{pageItems.map((item) => <TableRow key={item.id} className={selectedIds.includes(item.id) ? 'bg-primary/5' : undefined}><TableCell><input aria-label={`选择黑话 ${item.word}`} type="checkbox" checked={selectedIds.includes(item.id)} onChange={(event) => toggleSelected(item.id, event.target.checked)} /></TableCell><TableCell className="font-semibold">{item.word}</TableCell><TableCell className="max-w-xl"><p className="line-clamp-2 text-sm text-muted-foreground">{item.meaning || '尚未形成可展示的释义'}</p></TableCell><TableCell className="tabular-nums">{item.frequency}</TableCell><TableCell><Badge variant="secondary" className="font-mono text-[10px]">{item.source || 'wave_memory'}</Badge></TableCell><TableCell><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge></TableCell><TableCell>{item.anchors.length ? `${item.anchors.length} 条` : '无锚点'}</TableCell><TableCell className="text-right"><div className="flex justify-end gap-1"><Button type="button" variant="outline" size="sm" disabled={!item.object_ref} title={item.object_ref ? '还原同作用域聊天证据' : '缺少服务端签发的 ObjectRef'} onClick={() => setEvidenceItem(item)}><MessageSquareQuoteIcon data-icon="inline-start" />证据</Button>{item.status === 'pending' ? <><Button type="button" size="icon-sm" aria-label={`确认黑话 ${item.word}`} disabled={mutating === item.id || !reviewAvailable || item.anchors.length === 0} title="确认黑话" onClick={() => void review(item, 'approve')}><CheckIcon /></Button><Button type="button" variant="outline" size="icon-sm" aria-label={`拒绝并全局拉黑黑话 ${item.word}`} disabled={mutating === item.id || !reviewAvailable} title="拒绝并全局拉黑" onClick={() => void review(item, 'reject')}><XIcon /></Button></> : null}<Button type="button" variant="ghost" size="icon-sm" aria-label={`编辑黑话 ${item.word}`} disabled={!editAvailable || !item.object_ref} title={editAvailable ? '编辑释义；保存后回到待审核' : payload?.capabilities.edit?.reason_code ?? '编辑不可用'} onClick={() => openMeaningEditor(item)}><Edit2Icon /></Button><ResponsiveDetail title={item.word} description="黑话释义、证据引用与审核操作" className="sm:max-w-4xl" trigger={<Button type="button" variant="ghost" size="icon-sm" aria-label={`查看黑话 ${item.word} 详情`} title="查看详情"><EyeIcon /></Button>}><JargonDetails item={item} reviewAvailable={reviewAvailable} busy={mutating === item.id} onReview={(action) => void review(item, action)} /></ResponsiveDetail><Button type="button" variant="ghost" size="icon-sm" aria-label={`归档黑话 ${item.word}`} disabled={!archiveAvailable || !item.object_ref} title={archiveAvailable ? '归档并移出正式注入集合' : payload?.capabilities.archive?.reason_code ?? '归档不可用'} onClick={() => setArchiveItem(item)}><ArchiveIcon /></Button></div></TableCell></TableRow>)}</TableBody>
</Table>} cards={pageItems.map((item) => <article key={item.id} className={`flex flex-col gap-3 rounded-lg border bg-card p-4 ${selectedIds.includes(item.id) ? 'border-primary/50 bg-primary/5' : ''}`}><div className="flex items-start justify-between gap-2"><label className="flex min-w-0 items-start gap-2"><input aria-label={`选择黑话 ${item.word}`} type="checkbox" checked={selectedIds.includes(item.id)} onChange={(event) => toggleSelected(item.id, event.target.checked)} /><span><span className="block font-semibold">{item.word}</span><span className="mt-1 block whitespace-pre-wrap break-words text-sm text-muted-foreground">{item.meaning || '尚未形成可展示的释义'}</span></span></label><Badge className={statusClass(item.status)}>{STATUS_LABELS[item.status]}</Badge></div><div className="flex flex-wrap gap-2 text-xs text-muted-foreground"><span>频次 {item.frequency}</span><span>来源 {item.source || 'wave_memory'}</span><span>证据 {item.anchors.length} 条</span></div><div className="flex flex-wrap justify-end gap-2"><Button type="button" variant="outline" size="sm" disabled={!item.object_ref} onClick={() => setEvidenceItem(item)}><MessageSquareQuoteIcon data-icon="inline-start" />证据</Button><Button type="button" variant="outline" size="sm" disabled={!editAvailable || !item.object_ref} onClick={() => openMeaningEditor(item)}><Edit2Icon data-icon="inline-start" />编辑</Button><ResponsiveDetail title={item.word} description="黑话释义、证据引用与审核操作" className="sm:max-w-4xl" trigger={<Button type="button" variant="outline" size="sm">详情</Button>}><JargonDetails item={item} reviewAvailable={reviewAvailable} busy={mutating === item.id} onReview={(action) => void review(item, action)} /></ResponsiveDetail></div></article>)} />
</QueryState>{payload && !payload.capabilities.review?.available ? <Alert>
<AlertTitle>审核能力当前不可用</AlertTitle>
<AlertDescription>服务端拒绝原因：{payload.capabilities.review?.reason_code ?? '未提供'}</AlertDescription>
</Alert> : null}{payload ? <PaginationControls page={payload.page} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} /> : null}</CardContent>
</Card>

        <Card>
          <CardHeader><CardTitle className="text-base">全局黑话拉黑列表</CardTitle><CardDescription>所有群共享，展示规范化词形、来源、原因与时间；只有用户审核产生的手动项可在此解除。</CardDescription></CardHeader>
          <CardContent>
            <QueryState status={blocklistStatus} title="全局拉黑列表暂不可用" description="拉黑检查仍在服务端 fail-closed；此处仅为审计与解除入口。" onRetry={() => void loadBlocklist()}>
              {blocklist.length ? <ResponsiveTable label="全局黑话拉黑列表" table={<Table><TableHeader><TableRow><TableHead>规范化词形</TableHead><TableHead>原因</TableHead><TableHead>来源</TableHead><TableHead>记录时间</TableHead><TableHead className="w-24 text-right">操作</TableHead></TableRow></TableHeader><TableBody>{blocklist.map((item) => <TableRow key={item.id}><TableCell className="font-semibold">{item.word}</TableCell><TableCell className="text-sm text-muted-foreground">{item.reason}</TableCell><TableCell><Badge variant="outline" className="font-mono text-[10px]">{item.source}</Badge></TableCell><TableCell className="text-sm text-muted-foreground">{item.created_at ? formatTime(item.created_at) : '未记录'}</TableCell><TableCell className="text-right"><Button type="button" size="sm" variant="outline" disabled={item.source !== 'user_global_reject' || removingBlocklistId === item.id} title={item.source === 'user_global_reject' ? '解除手动全局拉黑' : 'Holyman 同步项不可手动删除'} onClick={() => void removeFromGlobalBlocklist(item)}>{removingBlocklistId === item.id ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <XIcon data-icon="inline-start" />}解除</Button></TableCell></TableRow>)}</TableBody></Table>} cards={blocklist.map((item) => <article key={item.id} className="flex flex-col gap-3 rounded-lg border bg-card p-4"><div className="flex items-start justify-between gap-3"><div><p className="font-semibold">{item.word}</p><p className="mt-1 text-sm text-muted-foreground">{item.reason}</p></div><Badge variant="outline" className="font-mono text-[10px]">{item.source}</Badge></div><div className="flex items-center justify-between gap-2 text-xs text-muted-foreground"><span>{item.created_at ? formatTime(item.created_at) : '未记录时间'}</span><Button type="button" size="sm" variant="outline" disabled={item.source !== 'user_global_reject' || removingBlocklistId === item.id} onClick={() => void removeFromGlobalBlocklist(item)}>解除全局拉黑</Button></div></article>)} /> : <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">当前没有全局拉黑词形。</div>}
            </QueryState>
          </CardContent>
        </Card>
      </TabsContent>

      <TabsContent value="catalog"><Card><CardHeader><div className="flex items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2 text-base"><DatabaseIcon className="size-4" />广域资产浏览</CardTitle><CardDescription className="mt-1">只读审计 Holyman 分层参考资产，不赋予本地审核语义，也不提供同步写入。</CardDescription></div><Badge variant={catalog?.asset_status === 'ready' ? 'secondary' : 'outline'}>{catalog?.asset_status === 'ready' ? '资产就绪' : '状态待核验'}</Badge></div></CardHeader><CardContent><QueryState status={catalogStatus} title="广域资产暂不可用" description="未使用演示数据或旧缓存替代。" onRetry={() => void loadCatalog()}>{catalog ? <CatalogBrowser catalog={catalog} /> : null}</QueryState></CardContent></Card></TabsContent>
    </Tabs>

    <Dialog open={Boolean(editItem)} onOpenChange={(open) => { if (!open && mutating === null) setEditItem(null) }}><DialogContent className="sm:max-w-lg"><DialogHeader><DialogTitle>编辑黑话释义{editItem ? ` · ${editItem.word}` : ''}</DialogTitle><DialogDescription>保存会推进 revision，并把该黑话重新置为待审核；不会绕过现有证据门禁直接修改已确认语义。</DialogDescription></DialogHeader><Field><FieldLabel htmlFor="jargon-edit-meaning">黑话释义</FieldLabel><Textarea id="jargon-edit-meaning" className="min-h-28" value={editMeaning} onChange={(event) => setEditMeaning(event.target.value)} /></Field><DialogFooter><Button type="button" variant="outline" disabled={mutating !== null} onClick={() => setEditItem(null)}>取消</Button><Button type="button" disabled={mutating !== null || !editMeaning.trim()} onClick={() => void saveMeaning()}>{mutating !== null ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <Edit2Icon data-icon="inline-start" />}保存并回到待审核</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(archiveItem)} onOpenChange={(open) => { if (!open && mutating === null) setArchiveItem(null) }}><DialogContent><DialogHeader><DialogTitle>归档黑话{archiveItem ? ` · ${archiveItem.word}` : ''}</DialogTitle><DialogDescription>该操作会把黑话移出正式注入集合，但保留记录、证据和审计信息；不会物理删除数据库行。</DialogDescription></DialogHeader><Alert><ArchiveIcon /><AlertTitle>可恢复的安全移除</AlertTitle><AlertDescription>旧版物理删除已替换为 scoped 归档，避免误删和跨 Scope 数据破坏。</AlertDescription></Alert><DialogFooter><Button type="button" variant="outline" disabled={mutating !== null} onClick={() => setArchiveItem(null)}>取消</Button><Button type="button" variant="destructive" disabled={mutating !== null} onClick={() => void archiveSelectedItem()}>{mutating !== null ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <ArchiveIcon data-icon="inline-start" />}确认归档</Button></DialogFooter></DialogContent></Dialog>
    <JargonEvidenceDialog item={evidenceItem} scope={scope} onClose={() => setEvidenceItem(null)} />
  </div>
}

export default JargonPage

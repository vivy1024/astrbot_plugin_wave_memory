import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AlertCircleIcon, CheckCircle2Icon, FileEditIcon, Loader2Icon, RefreshCwIcon, SaveIcon, SearchIcon, TagIcon, Trash2Icon } from 'lucide-react'
import { toast } from 'sonner'

import {
  batchDeleteMemories,
  correctMemoryTags,
  deleteMemory,
  getMemoryDetail,
  getMemoryTagState,
  getSimilarMemories,
  listMemories,
  memoryBatchStreamUrl,
  reEmbedMemory,
  runPostStream,
  undoMemoryTagCorrection,
  updateMemory,
  type MemoryDetail,
  type MemoryItem,
  type MemoryRefInput,
  type MemoryScope,
  type MemoryTagState,
  type SimilarMemoryItem,
  type StreamProgress,
} from '@/api/memories'
import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import { type TagExecutionOptions, type TagWritePolicy } from '@/api/tags'
import { TagExtractionConfigPanel } from '@/components/tag/TagExtractionConfigPanel'
import { DeclarativeDataTable, PaginationControls, QueryState, ScopeSelect, type ObjectRefState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Textarea } from '@/components/ui/textarea'
import { useCanonicalScopeDefault, usePaginationSearchParams } from '@/hooks/use-pagination-search-params'

const SOURCES = ['live', 'chat', 'noise', 'core', 'identity_quarantine', 'evolution', 'bzz_experience', 'experience', 'lore', 'book_lore', 'oni_lore', 'bot_reply', 'fewshot']

const DEEP_LINK_LABELS: Record<Exclude<ObjectRefState, 'ready'>, string> = {
  'not-found': '对象不存在、引用无效或版本已变化；不会用裸编号回退定位。',
  'scope-mismatch': '这条记忆不属于当前 Bot 和群。',
  'version-stale': '对象版本已更新，请从最新列表重新打开。',
}

interface ConfirmAction {
  title: string
  description: string
  label: string
  destructive?: boolean
  run: () => Promise<void> | void
}

function deepLinkFailureState(reason: unknown): ObjectRefState {
  const payload = reason instanceof Error && 'payload' in reason ? (reason as Error & { payload?: unknown }).payload : undefined
  const code = typeof payload === 'object' && payload !== null && 'error' in payload ? (payload as { error?: { code?: unknown } }).error?.code : undefined
  if (code === 'scope_mismatch') return 'scope-mismatch'
  if (code === 'version_stale') return 'version-stale'
  return 'not-found'
}

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function tagBadgeClass(type = 'keyword'): string {
  const colors: Record<string, string> = {
    person: 'border-pink-500/20 bg-pink-500/10 text-pink-500',
    topic: 'border-blue-500/20 bg-blue-500/10 text-blue-500',
    entity: 'border-red-500/20 bg-red-500/10 text-red-500',
    event: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-500',
    emotion: 'border-amber-500/20 bg-amber-500/10 text-amber-500',
  }
  return `border font-normal text-[10px] ${colors[type] ?? 'border-border/50 bg-muted text-muted-foreground'}`
}

export function MemoriesPage() {
  const pagination = usePaginationSearchParams()
  const [params, setParams] = useSearchParams()
  const botId = params.get('bot_id') ?? ''
  const sessionId = params.get('session_id') ?? ''
  const visibility = params.get('visibility') ?? 'group'
  const objectRef = params.get('ref') ?? ''
  const objectId = params.get('object_id') ?? ''
  const search = params.get('search') ?? ''
  const source = params.get('source') ?? ''
  const sender = params.get('sender') ?? ''
  const hasTags = params.get('has_tags') ?? ''
  const hasVector = params.get('has_vector') ?? ''
  useCanonicalScopeDefault({ botId, sessionId, setFilters: pagination.setFilters })
  const scope = useMemo<MemoryScope | null>(() => botId && sessionId && visibility === 'group' ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId, visibility])

  const [filterDraft, setFilterDraft] = useState({ search, source, sender, hasTags, hasVector })
  const [payload, setPayload] = useState<Awaited<ReturnType<typeof listMemories>> | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'empty' | 'unknown' | 'error'>('empty')
  const [error, setError] = useState<unknown>()
  const [selectedRefs, setSelectedRefs] = useState<string[]>([])

  const [detailOpen, setDetailOpen] = useState(false)
  const [detail, setDetail] = useState<MemoryDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [deepLinkStatus, setDeepLinkStatus] = useState<'loading' | ObjectRefState | null>(null)
  const [detailRetry, setDetailRetry] = useState(0)
  const [content, setContent] = useState('')
  const [importance, setImportance] = useState(0)
  const [saving, setSaving] = useState(false)
  const [similarLoading, setSimilarLoading] = useState(false)
  const [similarItems, setSimilarItems] = useState<SimilarMemoryItem[]>([])
  const [newTagName, setNewTagName] = useState('')
  const [tagReason, setTagReason] = useState('')
  const [tagState, setTagState] = useState<MemoryTagState | null>(null)
  const resolvedRef = useRef('')
  const detailRequest = useRef(0)
  const listRequest = useRef(0)

  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null)
  const [confirmRunning, setConfirmRunning] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [streamOpen, setStreamOpen] = useState(false)
  const [streamTitle, setStreamTitle] = useState('')
  const [streamProgress, setStreamProgress] = useState<StreamProgress | null>(null)
  const [streamLog, setStreamLog] = useState<string[]>([])
  const [streamRunning, setStreamRunning] = useState(false)
  const [tagBatchSize, setTagBatchSize] = useState(20)
  const [tagWritePolicy, setTagWritePolicy] = useState<TagWritePolicy>('missing_only')

  useEffect(() => setFilterDraft({ search, source, sender, hasTags, hasVector }), [hasTags, hasVector, search, sender, source])

  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    const options = scopeOptionsFor(await getScopeOptions(), ['session'])
    return botId ? options.filter((option) => option.description?.startsWith(`${botId} ·`)) : options
  }, [botId])

  const load = useCallback(async () => {
    const request = ++listRequest.current
    if (!scope) {
      setPayload(null)
      setSelectedRefs([])
      setStatus('empty')
      return
    }
    setStatus('loading')
    setError(undefined)
    try {
      const next = await listMemories({ ...scope, limit: pagination.limit, offset: pagination.offset, search: search || undefined, source: source || undefined, sender: sender || undefined, has_tags: hasTags || undefined, has_vector: hasVector || undefined })
      if (request !== listRequest.current) return
      setPayload(next)
      setSelectedRefs([])
      setStatus(next.items.length ? 'success' : next.page.total_status === 'unavailable' ? 'unknown' : 'empty')
    } catch (reason) {
      if (request !== listRequest.current) return
      setPayload(null)
      setError(reason)
      setStatus('error')
    }
  }, [hasTags, hasVector, pagination.limit, pagination.offset, scope, search, sender, source])

  useEffect(() => { void load() }, [load])

  const hydrateDetail = useCallback(async (detailUrl: string): Promise<MemoryDetail | null> => {
    const request = ++detailRequest.current
    setDetailOpen(true)
    setDetail(null)
    setContent('')
    setSimilarItems([])
    setTagState(null)
    setTagReason('')
    setDetailLoading(true)
    setSimilarLoading(true)
    try {
      const result = await getMemoryDetail(detailUrl)
      if (request !== detailRequest.current) return null
      resolvedRef.current = result.item.ref
      setDetail(result.item)
      setContent(result.item.content)
      setImportance(Number(result.item.importance ?? 0))
      setDeepLinkStatus('ready')
      try {
        const tags = await getMemoryTagState(result.item)
        if (request === detailRequest.current) setTagState(tags.item)
      } catch {
        if (request === detailRequest.current) setTagState(null)
      }
      try {
        const similar = await getSimilarMemories(result.item)
        if (request === detailRequest.current) setSimilarItems(similar.items ?? [])
      } catch {
        if (request === detailRequest.current) setSimilarItems([])
      }
      return result.item
    } finally {
      if (request === detailRequest.current) {
        setDetailLoading(false)
        setSimilarLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!objectRef) { setDeepLinkStatus(null); return }
    if (resolvedRef.current === objectRef) return
    if (!scope) { setDetail(null); setDeepLinkStatus('scope-mismatch'); return }
    if (objectId && !/^\d+$/.test(objectId)) { setDetail(null); setDeepLinkStatus('not-found'); return }
    const query = new URLSearchParams({ ref: objectRef, ...scope })
    const endpoint = objectId ? `/api/memories/${objectId}?${query.toString()}` : `/api/memories/resolve?${query.toString()}`
    let cancelled = false
    setDeepLinkStatus('loading')
    hydrateDetail(endpoint).catch((reason) => {
      if (cancelled) return
      setDetail(null)
      setDetailOpen(false)
      setDeepLinkStatus(deepLinkFailureState(reason))
    })
    return () => { cancelled = true }
  }, [detailRetry, hydrateDetail, objectId, objectRef, scope])

  async function open(item: MemoryItem) {
    try {
      const result = await hydrateDetail(item.detail_url)
      if (!result) return
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('ref', result.ref)
        next.set('object_id', String(result.id))
        next.set('bot_id', result.bot_id)
        next.set('session_id', result.session_id)
        next.set('visibility', result.visibility)
        return next
      })
    } catch (reason) {
      setDeepLinkStatus(deepLinkFailureState(reason))
      toast.error(reason instanceof Error ? reason.message : '记忆详情加载失败')
    }
  }

  function closeDetail() {
    detailRequest.current += 1
    setDetailOpen(false)
    setDetail(null)
    setContent('')
    setDetailLoading(false)
    setSimilarLoading(false)
    setSimilarItems([])
    setTagState(null)
    setTagReason('')
    resolvedRef.current = ''
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.delete('ref')
      next.delete('object_id')
      return next
    })
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    pagination.setFilters({
      search: filterDraft.search.trim() || null,
      source: filterDraft.source || null,
      sender: filterDraft.sender || null,
      has_tags: filterDraft.hasTags || null,
      has_vector: filterDraft.hasVector || null,
    })
  }

  function resetFilters() {
    setFilterDraft({ search: '', source: '', sender: '', hasTags: '', hasVector: '' })
    pagination.setFilters({ search: null, source: null, sender: null, has_tags: null, has_vector: null })
  }

  useEffect(() => {
    setSelectedRefs([])
    if (!detailOpen && !resolvedRef.current) return
    detailRequest.current += 1
    resolvedRef.current = ''
    setDetailOpen(false)
    setDetail(null)
    setContent('')
    setSimilarItems([])
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.delete('ref')
      next.delete('object_id')
      return next
    })
  // 结果集变化后，旧 ObjectRef/勾选绝不能继续作用于新的分页或 Scope。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botId, hasTags, hasVector, pagination.limit, pagination.offset, search, sender, source, sessionId, visibility])

  function selectedItems(): MemoryItem[] {
    const selected = new Set(selectedRefs)
    return payload?.items.filter((item) => selected.has(item.ref)) ?? []
  }

  function toggleAll(checked: boolean) {
    setSelectedRefs(checked ? (payload?.items.map((item) => item.ref) ?? []) : [])
  }

  function toggleRow(ref: string, checked: boolean) {
    setSelectedRefs((current) => checked ? [...current, ref] : current.filter((value) => value !== ref))
  }

  async function saveDetail() {
    if (!detail) return
    setSaving(true)
    try {
      const result = await updateMemory(detail.mutation_url, content, importance)
      if (!result.ok || result.operation.status !== 'succeeded' || !result.item) throw new Error('服务端未确认更新成功')
      const next = result.item
      resolvedRef.current = next.ref
      setDetail(next)
      setContent(next.content)
      setParams((current) => {
        const paramsNext = new URLSearchParams(current)
        paramsNext.set('ref', next.ref)
        paramsNext.set('object_id', String(next.id))
        return paramsNext
      })
      toast.success('记忆已按当前群更新')
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '更新失败')
    } finally {
      setSaving(false)
    }
  }

  async function removeDetail() {
    if (!detail) return
    setSaving(true)
    try {
      const result = await deleteMemory(detail.mutation_url)
      if (!result.ok || result.operation.status !== 'succeeded') throw new Error('服务端未确认删除成功')
      toast.success('记忆已删除')
      closeDetail()
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '删除失败')
    } finally {
      setSaving(false)
    }
  }

  async function reEmbedDetail() {
    if (!detail) return
    setSaving(true)
    try {
      const result = await reEmbedMemory(detail)
      if (result.ok === false) throw new Error(result.error ?? '服务端未确认向量化任务')
      if (result.operation?.status !== 'succeeded') {
        toast.message(`重新向量化任务已受理（${result.operation?.status ?? result.status ?? '状态未知'}），尚未确认完成`)
        return
      }
      setDetail({ ...detail, has_vector: true })
      toast.success('服务端已确认重新向量化完成')
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '重新向量化失败')
    } finally {
      setSaving(false)
    }
  }

  async function applyTagCorrection(operation: 'add' | 'remove', name: string) {
    if (!detail) return
    const reason = tagReason.trim()
    if (!reason) { toast.warning('请先填写校准理由'); return }
    setSaving(true)
    try {
      const result = await correctMemoryTags(detail, operation, [name], reason)
      if (!result.ok || result.operation.status !== 'succeeded' || !result.item) throw new Error('服务端未确认标签校准成功')
      setDetail({ ...detail, ...result.item.memory })
      setTagState(result.item.tags)
      resolvedRef.current = result.item.memory.ref
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('ref', result.item!.memory.ref)
        next.set('object_id', String(result.item!.memory.id))
        return next
      })
      setNewTagName('')
      setTagReason('')
      toast.success(operation === 'add' ? `已人工纳入标签“${name}”` : `已人工排除标签“${name}”`)
    } catch (failure) {
      toast.error(failure instanceof Error ? failure.message : '标签校准失败')
    } finally {
      setSaving(false)
    }
  }

  async function undoTagCorrection() {
    if (!detail || !tagState?.manual) return
    const reason = tagReason.trim()
    if (!reason) { toast.warning('请先填写撤销理由'); return }
    setSaving(true)
    try {
      const result = await undoMemoryTagCorrection(detail, tagState.manual.ref, reason)
      if (!result.ok || result.operation.status !== 'succeeded' || !result.item) throw new Error('服务端未确认撤销成功')
      setDetail({ ...detail, ...result.item.memory })
      setTagState(result.item.tags)
      resolvedRef.current = result.item.memory.ref
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('ref', result.item!.memory.ref)
        next.set('object_id', String(result.item!.memory.id))
        return next
      })
      setTagReason('')
      toast.success('已撤销当前人工标签校准')
    } catch (failure) {
      toast.error(failure instanceof Error ? failure.message : '撤销标签校准失败')
    } finally {
      setSaving(false)
    }
  }

  async function batchDelete() {
    if (!scope) return
    const refs: MemoryRefInput[] = selectedItems().map((item) => ({ id: item.id, ref: item.ref }))
    const result = await batchDeleteMemories(scope, refs)
    if (!result.ok) throw new Error('服务端未确认批量删除成功')
    toast.success(`已删除 ${result.deleted} 条记忆`)
    await load()
  }

  function startStream(action: 're-embed' | 'extract-tags') {
    if (!scope || streamRunning) return
    const refs: MemoryRefInput[] = selectedItems().map((item) => ({ id: item.id, ref: item.ref }))
    if (!refs.length) return
    setStreamTitle(action === 're-embed' ? '批量重新向量化' : '批量提取标签')
    setStreamProgress(null)
    setStreamLog([`[INIT] 已提交当前群的 ${refs.length} 条记忆。`])
    setStreamOpen(true)
    setStreamRunning(true)
    void runPostStream(memoryBatchStreamUrl(action, scope), refs, (state) => {
      setStreamProgress(state)
      setStreamLog((current) => [...current, `[PROGRESS] ${Math.round((state.progress ?? 0) * 100)}% · ${state.processed ?? 0}/${state.total} · 失败 ${state.errors ?? 0}`].slice(-50))
      if (state.done) {
        setStreamLog((current) => [...current, '[SUCCESS] 批量任务完成。'])
        setSelectedRefs([])
        void load()
      }
    }, action === 'extract-tags' ? { payload: { extract_tags: true, tag_batch_size: tagBatchSize, tag_write_policy: tagWritePolicy } } : undefined).catch((reason) => {
      const message = reason instanceof Error ? reason.message : '批量任务失败'
      setStreamLog((current) => [...current, `[ERROR] ${message}`])
      toast.error(message)
    }).finally(() => setStreamRunning(false))
  }

  async function executeConfirm() {
    if (!confirmAction) return
    setConfirmRunning(true)
    try {
      await confirmAction.run()
      setConfirmAction(null)
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '操作失败')
    } finally {
      setConfirmRunning(false)
    }
  }

  const items = payload?.items ?? []

  function formatCount(value: unknown): string {
    return value !== undefined && value !== null && value !== '' ? String(value) : '0'
  }

  return (
    <div data-slot="memories-page" className="flex flex-col gap-5">
      {/* 极简精致 Header 控制条 */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-bold tracking-tight">记忆管理器</h1>
          <p className="text-xs text-muted-foreground">搜索、查看、修改长期记忆权重或物理擦除记忆。</p>
        </div>

        {/* 真实的统计汇总 */}
        <div className="flex flex-wrap gap-2 text-xs">
          <div className="rounded-lg border bg-muted/20 px-3 py-2 text-center min-w-[70px]">
            <div className="text-muted-foreground scale-95 origin-center mb-0.5">记忆数</div>
            <div className="font-semibold">{payload ? formatCount(payload.page.total) : '...'}</div>
          </div>
          <div className="rounded-lg border bg-muted/20 px-3 py-2 text-center min-w-[70px]">
            <div className="text-muted-foreground scale-95 origin-center mb-0.5">有向量</div>
            <div className="font-semibold text-emerald-500">
              {payload ? formatCount(payload.items.filter(i => i.has_vector).length) : '...'}
            </div>
          </div>
        </div>
      </div>

      <Card className="border-border/60">
        <CardContent className="p-4 flex flex-col gap-4">
          <form className="flex flex-wrap items-center gap-2" onSubmit={submitSearch}>
            <div className="w-48 shrink-0">
              <ScopeSelect value={botId || undefined} loadOptions={loadBots} label="Bot" onValueChange={(value) => pagination.setFilters({ bot_id: value, session_id: null })} />
            </div>
            <div className="w-56 shrink-0">
              <ScopeSelect value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" disabled={!botId} onValueChange={(value) => pagination.setFilters({ session_id: value })} />
            </div>
            <div className="relative flex-1 min-w-[200px]">
              <SearchIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
              <Input className="pl-8 h-8" id="memory-search" placeholder="搜索记忆内容..." value={filterDraft.search} onChange={(event) => setFilterDraft((current) => ({ ...current, search: event.target.value }))} />
            </div>
            <div className="w-36 shrink-0">
              <Select value={filterDraft.source || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, source: value === 'all' ? '' : value }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部来源</SelectItem>
                  {SOURCES.map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="w-36 shrink-0">
              <Select value={filterDraft.hasTags || 'all'} onValueChange={(value) => setFilterDraft((current) => ({ ...current, hasTags: value === 'all' ? '' : value }))}>
                <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部标签状态</SelectItem>
                  <SelectItem value="true">有标签</SelectItem>
                  <SelectItem value="false">无标签</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button type="submit" disabled={!scope || status === 'loading'} size="sm" className="h-8">搜索</Button>

            <Button type="button" variant="outline" size="sm" className="h-8" onClick={resetFilters}>重置</Button>
          </form>

          <Separator />

          {deepLinkStatus && deepLinkStatus !== 'ready' ? <Alert variant={deepLinkStatus === 'loading' ? 'default' : 'destructive'}><AlertTitle>{deepLinkStatus === 'loading' ? '正在校验跳转链接' : '无法打开这条记忆'}</AlertTitle><AlertDescription className="flex flex-wrap items-center gap-2">{deepLinkStatus === 'loading' ? '正在验证当前群和版本是否还对得上。' : <><span>{DEEP_LINK_LABELS[deepLinkStatus]}</span><Button size="sm" variant="outline" onClick={() => setDetailRetry((value) => value + 1)}>重试</Button><Button size="sm" variant="ghost" onClick={closeDetail}>关闭并清除引用</Button></>}</AlertDescription></Alert> : null}

          {selectedRefs.length ? <div className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3 mb-4"><Badge variant="secondary">已选 {selectedRefs.length} 条</Badge><Button size="sm" variant="destructive" onClick={() => setConfirmAction({ title: '永久删除所选记忆？', description: `将删除 ${selectedRefs.length} 条当前群记忆及其标签关联，操作不可撤销。`, label: '确认批量删除', destructive: true, run: batchDelete })}><Trash2Icon data-icon="inline-start" />批量删除</Button><Button size="sm" variant="outline" disabled={streamRunning} onClick={() => setConfirmAction({ title: '批量重新向量化？', description: `将重算 ${selectedRefs.length} 条记忆的向量并触发索引修复。`, label: '确认执行', run: () => startStream('re-embed') })}><RefreshCwIcon data-icon="inline-start" />批量重新向量化</Button><Button size="sm" variant="outline" disabled={streamRunning} onClick={() => setConfirmAction({ title: '批量提取标签？', description: `将按当前策略处理 ${selectedRefs.length} 条记忆；追加或替换可能改变已有标签。`, label: '确认执行', run: () => startStream('extract-tags') })}><TagIcon data-icon="inline-start" />批量提取标签</Button><Button size="sm" variant="outline" onClick={() => setConfigOpen(true)}>提取配置</Button><Button size="sm" variant="ghost" className="ml-auto" onClick={() => setSelectedRefs([])}>取消选择</Button></div> : null}

          <QueryState status={status} error={error} onRetry={() => void load()} title={!scope ? '请选择 Bot 和群' : undefined} description={!scope ? '记忆管理不接受默认 Bot、私聊或未绑定群；不会从裸编号猜当前群。' : payload?.page.reason_code ?? undefined}>

            <DeclarativeDataTable
              label="记忆条目清单"
              items={items}
              keyExtractor={(row) => String(row.ref)}
              selectedKeys={new Set(selectedRefs)}
              onToggleSelect={(row) => toggleRow(row.ref, !selectedRefs.includes(row.ref))}
              onToggleAll={(checked) => toggleAll(checked)}
              onRowClick={(row) => void open(row)}
              rowClassName={(row) => selectedRefs.includes(row.ref) ? 'bg-primary/5' : undefined}
              columns={[
                { key: 'id', header: 'ID', className: 'w-16 font-mono text-xs text-muted-foreground', render: (row) => `#${row.id}` },
                { key: 'content', header: '内容', isTitle: true, className: 'max-w-md cursor-pointer truncate hover:text-primary', render: (row) => row.content },
                { key: 'sender', header: '发送者', className: 'max-w-32 truncate text-muted-foreground', render: (row) => row.sender_name ?? row.sender_id ?? '未记录' },
                { key: 'source', header: '来源', render: (row) => <Badge variant="secondary" className="font-mono text-[10px]">{row.source ?? '未记录'}</Badge> },
                { key: 'tags', header: '标签', render: (row) => <div className="flex flex-wrap gap-1">{row.tags?.length ? row.tags.slice(0, 2).map((tag, index) => <Badge key={`${tag.name}-${index}`} className={tagBadgeClass(tag.type)}>{tag.name}</Badge>) : <span className="text-xs text-muted-foreground">—</span>}</div> },
                { key: 'vector', header: '向量', className: 'text-center', render: (row) => <span className={row.has_vector ? 'font-bold text-emerald-500' : 'font-bold text-destructive'} title={row.has_vector ? '有向量' : '无向量'}>{row.has_vector ? '有向量' : '无向量'}</span> },
                { key: 'time', header: '时间', className: 'whitespace-nowrap font-mono text-xs text-muted-foreground', render: (row) => formatTime(row.timestamp) },
                { key: 'actions', header: null, render: (row) => <Button variant="ghost" size="icon-sm" title="打开详情" onClick={(e) => { e.stopPropagation(); void open(row) }}><FileEditIcon /></Button> },
              ]}
            />
          </QueryState>
          {payload ? <PaginationControls className="mt-4" page={payload.page} disabled={status === 'loading'} onOffsetChange={pagination.setOffset} onLimitChange={pagination.setLimit} /> : null}
        </CardContent>
      </Card>


      <Sheet open={detailOpen} onOpenChange={(openValue) => { if (!openValue) closeDetail() }}>
        <SheetContent className="w-full gap-0 overflow-hidden sm:max-w-2xl">
          <SheetHeader className="border-b pr-12"><SheetTitle>记忆明细</SheetTitle><SheetDescription>{detail ? `${detail.bot_id} · ${detail.session_id} · 版本 ${detail.version}` : '正在按当前群读取'}</SheetDescription></SheetHeader>
          <ScrollArea className="flex-1"><div className="flex flex-col gap-5 p-4">
            {detailLoading ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在加载详情与相似记忆</div> : detail ? <>
              <Field><FieldLabel htmlFor="memory-content">内容</FieldLabel><Textarea id="memory-content" className="min-h-36" value={content} onChange={(event) => setContent(event.target.value)} /><FieldDescription>保存会更新版本，并重新读取这条记忆。</FieldDescription></Field>
              <div className="grid gap-3 rounded-lg border bg-muted/20 p-3 text-xs sm:grid-cols-2"><div><span className="text-muted-foreground">发送者：</span>{detail.sender_name ?? detail.sender_id}</div><div><span className="text-muted-foreground">来源：</span>{detail.source ?? '未记录'}</div><div><span className="text-muted-foreground">重要度：</span><Input type="number" min="0" max="1" step="0.01" className="ml-2 inline-flex h-8 w-24" value={importance} onChange={(event) => setImportance(Number(event.target.value))} /></div><div><span className="text-muted-foreground">向量：</span><Badge variant={detail.has_vector ? 'secondary' : 'destructive'}>{detail.has_vector ? '已入库' : '缺失'}</Badge></div><div><span className="text-muted-foreground">创建时间：</span>{formatTime(detail.timestamp)}</div><div><span className="text-muted-foreground">访问次数：</span>{detail.access_count ?? '未记录'}</div></div>
              <Field><FieldLabel>标签精确校准</FieldLabel><div className="flex flex-col gap-3 rounded-lg border bg-muted/10 p-3">{tagState ? <><div className="flex flex-col gap-2"><span className="text-xs text-muted-foreground">自动基线</span><div className="flex min-h-7 flex-wrap gap-1.5">{tagState.automatic.length ? tagState.automatic.map((tag, index) => <Badge key={`automatic-${tag.name}-${index}`} variant="outline" className={tagBadgeClass(tag.tag_type ?? tag.type)}>{tag.name}</Badge>) : <span className="text-xs text-muted-foreground">无自动标签</span>}</div></div><div className="flex flex-col gap-2"><span className="text-xs text-muted-foreground">当前生效</span><div className="flex min-h-7 flex-wrap gap-1.5">{tagState.effective.map((tag, index) => <Badge key={`effective-${tag.name}-${index}`} className={`${tagBadgeClass(tag.tag_type ?? tag.type)} flex items-center gap-1 pr-1`}>{tag.name}<Button type="button" variant="ghost" size="icon-xs" className="size-8" aria-label={`人工排除标签 ${tag.name}`} onClick={() => setConfirmAction({ title: `人工排除标签“${tag.name}”？`, description: '自动基线会保留，本次变更将作为有理由、可撤销的本群人工校准。', label: '确认排除', destructive: true, run: () => applyTagCorrection('remove', tag.name) })}>×</Button></Badge>)}</div></div>{tagState.manual ? <Alert><TagIcon /><AlertTitle>人工校准生效中</AlertTitle><AlertDescription>{tagState.manual.reason} · 版本 {tagState.manual.revision}</AlertDescription></Alert> : null}</> : <Alert><AlertCircleIcon /><AlertTitle>标签状态不可用</AlertTitle><AlertDescription>未能读取自动、人工和生效标签，页面不会把未知状态伪装为空。</AlertDescription></Alert>}<Field><FieldLabel htmlFor="memory-tag-reason">校准理由</FieldLabel><Textarea id="memory-tag-reason" name="memory-tag-reason" autoComplete="off" maxLength={1000} value={tagReason} onChange={(event) => setTagReason(event.target.value)} placeholder="例如：自动提取遗漏了本条记忆的核心主题…" /></Field><div className="flex flex-col gap-2 sm:flex-row sm:items-end"><Field className="min-w-0 flex-1"><FieldLabel htmlFor="memory-tag-name">人工纳入标签</FieldLabel><Input id="memory-tag-name" name="memory-tag-name" autoComplete="off" maxLength={200} className="w-full text-xs" placeholder="例如：项目决策…" value={newTagName} onChange={(event) => setNewTagName(event.target.value)} /></Field><Button type="button" size="sm" disabled={saving || !newTagName.trim()} onClick={() => void applyTagCorrection('add', newTagName.trim())}>人工纳入</Button>{tagState?.manual ? <Button type="button" size="sm" variant="outline" disabled={saving} onClick={() => setConfirmAction({ title: '撤销当前 Tag 人工校准？', description: '将恢复上一层人工校准；若不存在上一层，则恢复自动基线。', label: '确认撤销', run: undoTagCorrection })}>撤销校准</Button> : null}</div></div></Field>
              <Card className="border-primary/20 bg-primary/5"><CardHeader className="py-3"><CardTitle className="text-sm">相似记忆</CardTitle><CardDescription>按当前记忆的向量查询相似内容；结果只读展示，不会用裸编号跳到别的群。</CardDescription></CardHeader><CardContent className="flex flex-col gap-2 pt-0">{similarLoading ? <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="animate-spin" />计算中</div> : similarItems.length ? similarItems.map((item) => <div key={item.id} className="rounded-lg border bg-background/50 p-2.5"><div className="flex justify-between gap-2 text-[10px] font-mono text-muted-foreground"><span>#{item.id} · {item.source || '未记录'}</span><span className="font-semibold text-primary">相似度 {item.similarity}%</span></div><p className="mt-1 line-clamp-2 text-xs leading-relaxed">{item.content}</p></div>) : <p className="py-3 text-center text-xs text-muted-foreground">未找到相似记录，或当前记忆没有可用向量。</p>}</CardContent></Card>
              <div className="flex flex-wrap gap-2 border-t pt-4"><Button disabled={saving} onClick={() => void saveDetail()}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存并重新读取</Button><Button disabled={saving} variant="outline" onClick={() => setConfirmAction({ title: '重新向量化当前记忆？', description: '将重算向量并触发索引修复，不修改正文版本。', label: '确认重新向量化', run: reEmbedDetail })}><RefreshCwIcon />重新向量化</Button><Button disabled={saving} variant="destructive" className="ml-auto" onClick={() => setConfirmAction({ title: `永久删除记忆 #${detail.id}？`, description: '将按当前记忆删除，操作不可撤销。', label: '确认删除', destructive: true, run: removeDetail })}><Trash2Icon />删除当前记忆</Button></div>
            </> : <Alert variant="destructive"><AlertTitle>详情不可用</AlertTitle><AlertDescription>无法读取当前记忆。</AlertDescription></Alert>}
          </div></ScrollArea>
        </SheetContent>
      </Sheet>

      <Dialog open={Boolean(confirmAction)} onOpenChange={(openValue) => { if (!openValue && !confirmRunning) setConfirmAction(null) }}><DialogContent><DialogHeader><DialogTitle>{confirmAction?.title}</DialogTitle><DialogDescription>{confirmAction?.description}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={confirmRunning} onClick={() => setConfirmAction(null)}>取消</Button><Button variant={confirmAction?.destructive ? 'destructive' : 'default'} disabled={confirmRunning} onClick={() => void executeConfirm()}>{confirmRunning ? <Loader2Icon className="animate-spin" /> : null}{confirmAction?.label}</Button></DialogFooter></DialogContent></Dialog>

      <Dialog open={configOpen} onOpenChange={setConfigOpen}><DialogContent className="sm:max-w-md"><DialogHeader><DialogTitle>标签提取参数</DialogTitle><DialogDescription>配置仅用于当前勾选的本群记忆。</DialogDescription></DialogHeader><TagExtractionConfigPanel title="LLM 运行配置" description="只补缺最安全；追加或替换会在执行前再次确认。" disabled={streamRunning} onOptionsChange={(options: Required<TagExecutionOptions>) => { setTagBatchSize(options.tag_batch_size); setTagWritePolicy(options.tag_write_policy) }} /><DialogFooter><Button onClick={() => setConfigOpen(false)}>保存配置</Button></DialogFooter></DialogContent></Dialog>

      <Dialog open={streamOpen} onOpenChange={setStreamOpen}><DialogContent className="sm:max-w-lg"><DialogHeader><DialogTitle>{streamTitle}</DialogTitle><DialogDescription>批量任务只提交当前群里服务端确认过的记忆。</DialogDescription></DialogHeader><div className="flex flex-col gap-4">{streamProgress ? <div><div className="mb-1 flex justify-between text-xs"><span>{Math.round(streamProgress.progress * 100)}%</span><span>{streamProgress.processed ?? 0}/{streamProgress.total} · 失败 {streamProgress.errors ?? 0}</span></div><div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary transition-all" style={{ width: `${streamProgress.progress * 100}%` }} /></div></div> : <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在连接批量任务</div>}<ScrollArea className="h-40 rounded-lg border bg-muted/40 p-3 font-mono text-[10px]">{streamLog.map((line, index) => <div key={`${index}-${line}`} className={line.includes('[ERROR]') ? 'text-destructive' : line.includes('[SUCCESS]') ? 'text-emerald-500' : undefined}>{line}</div>)}</ScrollArea>{streamProgress?.done ? <Alert className="border-emerald-500/20 bg-emerald-500/10"><CheckCircle2Icon /><AlertTitle>处理完成</AlertTitle></Alert> : null}</div></DialogContent></Dialog>
    </div>
  )
}

export default MemoriesPage

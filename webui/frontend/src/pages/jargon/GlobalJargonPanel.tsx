import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CheckCircle2Icon,
  DatabaseIcon,
  EyeIcon,
  Globe2Icon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  Trash2Icon,
  XCircleIcon,
} from 'lucide-react'
import { toast } from 'sonner'

import {
  checkHolymanUpdate,
  deleteGlobalJargon,
  getCatalogAudit,
  listGlobalJargon,
  previewHolymanSync,
  setGlobalJargonStatus,
  upsertGlobalJargon,
  type BotJargonItem,
  type BotJargonResponse,
  type CatalogAssetRecord,
  type CatalogAuditPayload,
  type HolymanSyncPreviewPayload,
  type HolymanUpdateCheckPayload,
} from '@/api/jargon'
import { humanizeApiError } from '@/lib/reason-label'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { QueryState, ResponsiveTable } from '@/components/shared'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'

const SOURCE_LABELS: Record<string, string> = {
  manual: '手工维护',
  promoted: '群黑话提升',
  holyman_import: '内置资产',
  holyman_skills: '内置资产',
  manual_deleted: '已移除',
}

const PREVIEW_LIMIT = 40

/** Holyman 原始文件名 → 人类可读来源名，便于看出各层内容来自哪个文档。 */
const SOURCE_FILE_LABELS: Record<string, string> = {
  '神人.skill/_knowledge/gaming.md': '游戏观',
  '神人.skill/_knowledge/internet-culture.md': '互联网文化观',
  '神人.skill/_persona/communication.md': '沟通模式',
  '神人.skill/_persona/rules.md': '人格规则',
  '神人.skill/_persona/values.md': '价值观',
  '神人.skill/_quotes/iconic.md': '标志语录',
  '神人.skill/_quotes/internal.md': '私下语录',
  '神人.skill/SKILL.md': '技能主文件',
  '神人.skill/_meta/sources.md': '素材来源',
}

function sourceLabel(item: CatalogAssetRecord): string {
  const raw = String(item.source ?? '')
  if (!raw) return ''
  return SOURCE_FILE_LABELS[raw] ?? raw.split('/').pop() ?? raw
}

interface DraftState {
  word: string
  meaning: string
  isNew: boolean
}

function textOf(item: CatalogAssetRecord, keys: string[], fallback = '—'): string {
  for (const key of keys) {
    const value = item[key]
    if (typeof value === 'string' && value.trim()) return value
    if (typeof value === 'number') return String(value)
  }
  return fallback
}

function matchesSearch(item: CatalogAssetRecord, needle: string): boolean {
  if (!needle) return true
  return Object.values(item).some((value) => typeof value === 'string' && value.toLocaleLowerCase().includes(needle))
}

/** 只读资产层：明确声明「只读参考・不注入」，绝不提供启用开关。 */
function ReadOnlyLayer({
  icon,
  title,
  description,
  items,
  titleKeys,
  summaryKeys,
  search,
}: {
  icon: React.ReactNode
  title: string
  description: string
  items: CatalogAssetRecord[]
  titleKeys: string[]
  summaryKeys: string[]
  search: string
}) {
  const [expanded, setExpanded] = useState(false)
  const filtered = useMemo(
    () => items.filter((item) => matchesSearch(item, search)),
    [items, search],
  )
  const visible = expanded ? filtered : filtered.slice(0, PREVIEW_LIMIT)

  return <Card>
    <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <CardTitle className="flex items-center gap-2 text-base">{icon}{title}</CardTitle>
        <CardDescription className="mt-1">{description}</CardDescription>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{filtered.length} 条</Badge>
        <Badge variant="secondary">只读参考・不注入</Badge>
      </div>
    </CardHeader>
    <CardContent className="flex flex-col gap-3">
      {visible.length ? <ResponsiveTable
        label={`${title}清单`}
        table={<Table><TableHeader><TableRow>
          <TableHead className="w-1/3">名称</TableHead>
          <TableHead>内容摘要</TableHead>
          <TableHead className="w-28">来源文档</TableHead>
        </TableRow></TableHeader>
        <TableBody>{visible.map((item, index) => <TableRow key={`${title}:${index}`}>
          <TableCell className="font-medium">{textOf(item, titleKeys, `条目 ${index + 1}`)}</TableCell>
          <TableCell className="whitespace-normal break-words text-sm text-muted-foreground">{textOf(item, summaryKeys)}</TableCell>
          <TableCell>{sourceLabel(item) ? <Badge variant="outline" className="text-[10px]">{sourceLabel(item)}</Badge> : null}</TableCell>
        </TableRow>)}</TableBody></Table>}
        cards={visible.map((item, index) => <article key={`${title}:${index}`} className="flex flex-col gap-2 rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <p className="font-medium">{textOf(item, titleKeys, `条目 ${index + 1}`)}</p>
            {sourceLabel(item) ? <Badge variant="outline" className="text-[10px]">{sourceLabel(item)}</Badge> : null}
          </div>
          <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{textOf(item, summaryKeys)}</p>
        </article>)}
      /> : <p className="py-6 text-center text-sm text-muted-foreground">{search ? '没有匹配的条目。' : '该层暂无条目。'}</p>}
      {filtered.length > PREVIEW_LIMIT ? <div className="flex justify-center">
        <Button type="button" variant="outline" size="sm" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起' : `显示全部 ${filtered.length} 条`}
        </Button>
      </div> : null}
    </CardContent>
  </Card>
}

/**
 * 广域黑话（Holyman-skills 完整资产）。
 *
 * 这里是**一个实体**：整个内置资产按层分区展示，用户不再需要在「广域黑话」和「内置广域
 * 资产」两个界面之间来回跳。只有「精选口癖」是可注入的，因此只有它有启用开关；其余层
 * 明确标注「只读参考・不注入」。
 */
export function GlobalJargonPanel({ botId }: { botId: string }) {
  const [payload, setPayload] = useState<BotJargonResponse | null>(null)
  const [status, setStatus] = useState<'unknown' | 'loading' | 'success' | 'empty' | 'error'>('unknown')
  const [error, setError] = useState<unknown>()
  const [catalog, setCatalog] = useState<CatalogAuditPayload | null>(null)
  const [catalogStatus, setCatalogStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [catalogError, setCatalogError] = useState<unknown>(null)
  const [draft, setDraft] = useState<DraftState | null>(null)
  const [busyWord, setBusyWord] = useState<string | null>(null)
  const [removeTarget, setRemoveTarget] = useState<BotJargonItem | null>(null)
  const [search, setSearch] = useState('')
  const [updateCheck, setUpdateCheck] = useState<HolymanUpdateCheckPayload | null>(null)
  const [updateChecking, setUpdateChecking] = useState(false)
  const [preview, setPreview] = useState<HolymanSyncPreviewPayload | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const request = useRef(0)

  const load = useCallback(async () => {
    if (!botId) { setPayload(null); setStatus('unknown'); return }
    const current = ++request.current
    setStatus('loading')
    setError(undefined)
    try {
      const value = await listGlobalJargon(botId)
      if (current !== request.current) return
      setPayload(value)
      setStatus((value.items ?? []).length ? 'success' : 'empty')
    } catch (reason) {
      if (current !== request.current) return
      setPayload(null)
      setError(reason)
      setStatus('error')
    }
  }, [botId])

  const loadCatalog = useCallback(async () => {
    setCatalogStatus('loading')
    setCatalogError(null)
    try {
      setCatalog(await getCatalogAudit())
      setCatalogStatus('success')
    } catch (reason) {
      setCatalog(null)
      setCatalogError(reason)
      setCatalogStatus('error')
    }
  }, [])

  useEffect(() => { void load(); void loadCatalog() }, [load, loadCatalog])

  const normalizedSearch = search.trim().toLocaleLowerCase()
  const items = payload?.items ?? []
  const builtinCount = items.filter((item) => item.is_builtin).length
  const activeCount = items.filter((item) => item.status === 'active').length

  const toggleStatus = async (item: BotJargonItem) => {
    setBusyWord(item.word)
    try {
      await setGlobalJargonStatus(botId, item.word, item.status === 'active' ? 'inactive' : 'active')
      toast.success(item.status === 'active' ? '已停用该广域黑话' : '已启用该广域黑话')
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '更新广域黑话状态失败'))
    } finally {
      setBusyWord(null)
    }
  }

  const saveDraft = async () => {
    if (!draft) return
    setBusyWord(draft.word)
    try {
      await upsertGlobalJargon(botId, { word: draft.word.trim(), meaning: draft.meaning.trim(), status: 'active' })
      toast.success(draft.isNew ? '已新增广域黑话' : '已更新广域黑话')
      setDraft(null)
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '保存广域黑话失败'))
    } finally {
      setBusyWord(null)
    }
  }

  const confirmRemove = async () => {
    if (!removeTarget) return
    setBusyWord(removeTarget.word)
    try {
      await deleteGlobalJargon(botId, removeTarget.word)
      toast.success('已移除；内置词会留标记，不会被资产同步复活')
      setRemoveTarget(null)
      await load()
    } catch (reason) {
      toast.error(humanizeApiError(reason, '移除广域黑话失败'))
    } finally {
      setBusyWord(null)
    }
  }

  async function refreshUpdateCheck() {
    setUpdateChecking(true)
    try {
      const result = await checkHolymanUpdate(true)
      setUpdateCheck(result)
      toast.success(result.has_update ? '检测到内置资产远端更新' : '内置资产已是最新')
    } catch (reason) {
      toast.error(humanizeApiError(reason, '内置资产更新检查失败'))
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
      toast.error(humanizeApiError(reason, '内置资产同步预览失败'))
    } finally {
      setPreviewLoading(false)
    }
  }

  const searchableItems = useMemo(
    () => items.filter((item) => !normalizedSearch || item.word.toLocaleLowerCase().includes(normalizedSearch) || item.meaning.toLocaleLowerCase().includes(normalizedSearch)),
    [items, normalizedSearch],
  )

  const concepts = catalog?.concepts ?? []
  const examples = catalog?.examples ?? []
  const corpus = catalog?.corpus ?? []
  const candidates = catalog?.candidates ?? []
  const blocked = useMemo<CatalogAssetRecord[]>(
    () => Object.entries(catalog?.blocked ?? {}).map(([word, reason]) => ({ word, reason })),
    [catalog?.blocked],
  )

  return <div className="flex flex-col gap-4">
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base"><Globe2Icon className="size-4" />广域黑话（Holyman-skills）</CardTitle>
            <CardDescription className="mt-1 max-w-3xl">
              整个内置资产在此按层汇总：只有「精选口癖」会进入提示词并可按词启停；文化概念、声音样本、原始语料与候选仅为只读参考，永远不会注入，因此不提供启用开关。手工新增与群内提升的词也归入精选口癖层，与其共用同一注入名额池。
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" disabled={!botId} onClick={() => setDraft({ word: '', meaning: '', isNew: true })}>
              <PlusIcon data-icon="inline-start" />新增广域黑话
            </Button>
            <Button type="button" size="icon-sm" variant="outline" disabled={!botId || status === 'loading'} onClick={() => { void load(); void loadCatalog() }} aria-label="刷新广域黑话">
              <RefreshCwIcon className={status === 'loading' ? 'animate-spin' : undefined} />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <div className="rounded-lg border p-3">
            <p className="text-sm text-muted-foreground">精选口癖（可注入）</p>
            <p className="text-lg font-semibold">{activeCount} / {items.length} 已启用</p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-sm text-muted-foreground">来自内置资产</p>
            <p className="text-lg font-semibold">{builtinCount} 条</p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-sm text-muted-foreground">只读参考资产合计</p>
            <p className="text-lg font-semibold">{concepts.length + examples.length + corpus.length + candidates.length + blocked.length} 条</p>
          </div>
        </div>
        <Alert>
          <ShieldCheckIcon />
          <AlertTitle>风格人格包为可选、默认关闭</AlertTitle>
          <AlertDescription>
            holyman-skills 原生是人格提示词包。若要把它作为「说话方式参考」注入（会改变风格），请在通道配置页开启 holyman_persona；它与既有 Bot 人格并存，且整包需通过身份安全守卫。
          </AlertDescription>
        </Alert>
        <div className="flex flex-wrap items-end gap-2">
          <Field className="min-w-56 flex-1">
            <FieldLabel htmlFor="global-jargon-search">筛选全部层级</FieldLabel>
            <Input id="global-jargon-search" value={search} placeholder="搜索口癖、概念、声音样本、语料或候选" onChange={(event) => setSearch(event.target.value)} />
          </Field>
        </div>
      </CardContent>
    </Card>

    {/* 第 1 层：精选口癖 —— 唯一可注入、唯一带启用开关 */}
    <Card>
      <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle className="flex items-center gap-2 text-base"><CheckCircle2Icon className="size-4" />精选口癖</CardTitle>
          <CardDescription className="mt-1">唯一会进入提示词的一层；按词启停，停用后立即对所有群失效。手工新增与群内提升的词也在这一层。</CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{activeCount} 条已启用</Badge>
          <Badge variant="outline">{builtinCount} 条内置</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!botId ? <Alert>
          <AlertTitle>请先选择 Bot</AlertTitle>
          <AlertDescription>广域黑话按 Bot 归属，与当前选中的群无关；请在上方作用域里先选一个 Bot。</AlertDescription>
        </Alert> : null}
        {botId ? <QueryState
          status={status === 'unknown' ? 'empty' : status}
          error={error}
          title="广域黑话读取失败"
          emptyTitle="该 Bot 还没有广域黑话"
          emptyDescription="内置广域口癖默认启用并会列在这里；也可以先在群聊黑话里确认一条，再提升为广域。"
          onRetry={() => void load()}
        >
          <ResponsiveTable
            label="精选口癖清单"
            table={<Table className="w-full table-fixed">
              <TableHeader><TableRow>
                <TableHead className="w-1/5">词条</TableHead>
                <TableHead className="w-auto">释义</TableHead>
                <TableHead className="w-28">来源</TableHead>
                <TableHead className="w-20">状态</TableHead>
                <TableHead className="w-56 text-right">操作</TableHead>
              </TableRow></TableHeader>
              <TableBody>{searchableItems.map((item) => <TableRow key={item.word}>
                <TableCell className="font-semibold truncate">{item.word}</TableCell>
                <TableCell className="whitespace-normal break-words text-sm text-muted-foreground">{item.meaning || '尚未填写释义'}</TableCell>
                <TableCell><Badge variant="outline" className="text-[10px]">{SOURCE_LABELS[item.source] ?? item.source}</Badge></TableCell>
                <TableCell><Badge variant={item.status === 'active' ? 'secondary' : 'outline'}>{item.status === 'active' ? '已启用' : '已停用'}</Badge></TableCell>
                <TableCell className="text-right">
                  <div className="flex justify-end gap-1">
                    <Button type="button" variant="ghost" size="icon-sm" aria-label={`编辑广域黑话 ${item.word}`} disabled={busyWord === item.word} onClick={() => setDraft({ word: item.word, meaning: item.meaning, isNew: false })}>
                      <PencilIcon />
                    </Button>
                    <Button type="button" variant="ghost" size="icon-sm" aria-label={`${item.status === 'active' ? '停用' : '启用'}广域黑话 ${item.word}`} disabled={busyWord === item.word} onClick={() => void toggleStatus(item)}>
                      {item.status === 'active' ? <XCircleIcon /> : <CheckCircle2Icon />}
                    </Button>
                    <Button type="button" variant="ghost" size="icon-sm" aria-label={`移除广域黑话 ${item.word}`} disabled={busyWord === item.word} onClick={() => setRemoveTarget(item)}>
                      <Trash2Icon />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>)}</TableBody>
            </Table>}
            cards={searchableItems.map((item) => <article key={item.word} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
              <div className="flex items-start justify-between gap-2">
                <span className="font-semibold">{item.word}</span>
                <div className="flex flex-wrap gap-1">
                  <Badge variant="outline" className="text-[10px]">{SOURCE_LABELS[item.source] ?? item.source}</Badge>
                  <Badge variant={item.status === 'active' ? 'secondary' : 'outline'}>{item.status === 'active' ? '已启用' : '已停用'}</Badge>
                </div>
              </div>
              <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{item.meaning || '尚未填写释义'}</p>
              <div className="flex flex-wrap justify-end gap-2">
                <Button type="button" variant="outline" size="sm" disabled={busyWord === item.word} onClick={() => setDraft({ word: item.word, meaning: item.meaning, isNew: false })}>编辑</Button>
                <Button type="button" variant="outline" size="sm" disabled={busyWord === item.word} onClick={() => void toggleStatus(item)}>{item.status === 'active' ? '停用' : '启用'}</Button>
                <Button type="button" variant="outline" size="sm" disabled={busyWord === item.word} onClick={() => setRemoveTarget(item)}>移除</Button>
              </div>
            </article>)}
          />
        </QueryState> : null}
      </CardContent>
    </Card>

    {catalogError ? <Alert variant="destructive">
      <AlertTitle>只读资产加载失败</AlertTitle>
      <AlertDescription>{humanizeApiError(catalogError, '内置资产读取失败')}　<Button type="button" variant="outline" size="sm" onClick={() => void loadCatalog()}>重试</Button></AlertDescription>
    </Alert> : null}

    {catalogStatus === 'loading' ? <div className="flex min-h-32 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在加载内置资产…</div> : null}

    {catalogStatus === 'success' ? <>
      <ReadOnlyLayer
        icon={<DatabaseIcon className="size-4" />}
        title="文化概念"
        description="Holyman-skills 的背景与规则概念；只作为理解语境，不进入提示词。"
        items={concepts}
        titleKeys={['title', 'word']}
        summaryKeys={['summary', 'meaning', 'content']}
        search={normalizedSearch}
      />
      <ReadOnlyLayer
        icon={<DatabaseIcon className="size-4" />}
        title="声音样本与知识"
        description="语言基因、语气矩阵与引用样本；只读参考，不进入提示词。"
        items={examples}
        titleKeys={['title', 'word']}
        summaryKeys={['text', 'content', 'summary']}
        search={normalizedSearch}
      />
      <ReadOnlyLayer
        icon={<DatabaseIcon className="size-4" />}
        title="原始语料"
        description="神言原文语料；体量大且未经审核，永远只读参考、绝不注入。"
        items={corpus}
        titleKeys={['id', 'title']}
        summaryKeys={['text']}
        search={normalizedSearch}
      />
      <ReadOnlyLayer
        icon={<DatabaseIcon className="size-4" />}
        title="资产候选"
        description="从语料里挖掘出的候选词。历史上由已下线的审核入口处理，当前审核能力不可用，因此只读展示。"
        items={candidates}
        titleKeys={['word']}
        summaryKeys={['reason', 'source']}
        search={normalizedSearch}
      />
      <ReadOnlyLayer
        icon={<DatabaseIcon className="size-4" />}
        title="屏蔽项"
        description="明确排除的词形，不会进入注入或候选。"
        items={blocked}
        titleKeys={['word']}
        summaryKeys={['reason']}
        search={normalizedSearch}
      />
    </> : null}

    <Card className="bg-muted/10">
      <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle className="text-base">内置资产版本审计</CardTitle>
          <CardDescription>查看资产版本、远端更新状态与同步预览；同步写入当前未开放。</CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" size="sm" variant="outline" disabled={updateChecking} onClick={() => void refreshUpdateCheck()}>
            {updateChecking ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <RefreshCwIcon data-icon="inline-start" />}检查更新
          </Button>
          <Button type="button" size="sm" onClick={() => void openSyncPreview()}><EyeIcon data-icon="inline-start" />同步预览</Button>
        </div>
      </CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">本地版本</p><p className="font-mono">{updateCheck?.local_version || catalog?.local_version || '未知'}</p></div>
        <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">远端版本</p><p className="font-mono">{updateCheck?.remote_version || catalog?.remote_version || '未知'}</p></div>
        <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">资产状态</p><p className="font-mono">{catalog?.asset_status ?? '未知'}</p></div>
        <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">语料核对</p><p className="font-mono">{`${String(catalog?.quality_summary?.parsed_corpus_count ?? '—')} / ${String(catalog?.quality_summary?.declared_corpus_count ?? '—')}`}</p></div>
      </CardContent>
      <CardContent className="border-t pt-4">
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">注入策略与资产字段</summary>
          <dl className="mt-3 grid gap-3 sm:grid-cols-2">
            <div><dt className="text-muted-foreground">资产类型</dt><dd className="break-all font-mono">{catalog?.asset_type ?? '未知'}</dd></div>
            <div><dt className="text-muted-foreground">运行策略</dt><dd className="break-all font-mono">{catalog?.runtime_policy ?? '未知'}</dd></div>
            <div><dt className="text-muted-foreground">文化概念/声音样本/语料</dt><dd className="font-mono">reference-only</dd></div>
            <div><dt className="text-muted-foreground">精选口癖</dt><dd className="font-mono">runtime-match</dd></div>
            <div><dt className="text-muted-foreground">同步能力代码</dt><dd className="break-all font-mono">catalog_sync_command_unavailable</dd></div>
          </dl>
        </details>
      </CardContent>
    </Card>

    <Dialog open={Boolean(draft)} onOpenChange={(open) => { if (!open && !busyWord) setDraft(null) }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{draft?.isNew ? '新增广域黑话' : `编辑广域黑话 · ${draft?.word ?? ''}`}</DialogTitle>
          <DialogDescription>广域黑话对所有群生效；保存后立即参与注入匹配。</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel htmlFor="global-jargon-word">词条</FieldLabel>
          <Input id="global-jargon-word" value={draft?.word ?? ''} disabled={!draft?.isNew} onChange={(event) => setDraft((current) => current ? { ...current, word: event.target.value } : current)} placeholder="例如：抽象的梗本身" />
        </Field>
        <Field>
          <FieldLabel htmlFor="global-jargon-meaning">释义</FieldLabel>
          <Textarea id="global-jargon-meaning" className="min-h-28" value={draft?.meaning ?? ''} onChange={(event) => setDraft((current) => current ? { ...current, meaning: event.target.value } : current)} />
        </Field>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={Boolean(busyWord)} onClick={() => setDraft(null)}>取消</Button>
          <Button type="button" disabled={Boolean(busyWord) || !draft?.word.trim() || !draft?.meaning.trim()} onClick={() => void saveDraft()}>
            {busyWord ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <CheckCircle2Icon data-icon="inline-start" />}保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={Boolean(removeTarget)} onOpenChange={(open) => { if (!open && !busyWord) setRemoveTarget(null) }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>移除广域黑话{removeTarget ? ` · ${removeTarget.word}` : ''}</DialogTitle>
          <DialogDescription>移除后不再参与任何群的注入；内置词会以标记覆盖，避免被资产同步复活。</DialogDescription>
        </DialogHeader>
        <Alert>
          <Trash2Icon />
          <AlertTitle>保留移除标记</AlertTitle>
          <AlertDescription>内置词无法物理删除，会留下标记以覆盖资产默认值，因此同步内置资产不会把它重新启用。</AlertDescription>
        </Alert>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={Boolean(busyWord)} onClick={() => setRemoveTarget(null)}>取消</Button>
          <Button type="button" variant="destructive" disabled={Boolean(busyWord)} onClick={() => void confirmRemove()}>
            {busyWord ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <Trash2Icon data-icon="inline-start" />}确认移除
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
      <DialogContent className="flex max-h-[86vh] flex-col sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>内置资产同步预览</DialogTitle>
          <DialogDescription>只读取远端并在内存中比较；正式同步仍禁用，不会写入资产。</DialogDescription>
        </DialogHeader>
        {previewLoading ? <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="animate-spin" />正在读取远端并生成差异</div>
          : preview ? <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">本地版本</p><p className="font-mono">{preview.local_version || '未知'}</p></div>
            <div className="rounded-lg border p-3 text-sm"><p className="text-muted-foreground">远端版本</p><p className="font-mono">{preview.remote_version || '未知'}</p></div>
          </div> : <Alert variant="destructive"><AlertTitle>同步预览不可用</AlertTitle><AlertDescription>服务端没有返回可展示的差异。</AlertDescription></Alert>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setPreviewOpen(false)}>关闭</Button>
          <Button type="button" disabled title="catalog_sync_command_unavailable">确认同步并写入</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}

export default GlobalJargonPanel

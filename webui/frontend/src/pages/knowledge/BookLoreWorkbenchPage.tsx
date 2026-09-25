import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BookPlusIcon, DatabaseZapIcon, FilePlus2Icon, RefreshCwIcon, SaveIcon, Trash2Icon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import {
  deleteWorkbenchNote,
  getWorkbenchNote,
  getWorkbenchOverview,
  importChapters,
  listWorkbenchNotes,
  previewChapters,
  saveWorkbenchNote,
  syncWorkbenchIndex,
  type ChapterPreview,
  type WorkbenchNote,
  type WorkbenchNoteRow,
  type WorkbenchOverview,
} from '@/api/bookLoreWorkbench'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

const number = new Intl.NumberFormat('zh-CN')
const TIER_LABELS = { notes: '笔记（章节、人物、世界观…）', communities: '社区摘要（GraphRAG）', entities: '实体（GraphRAG）' } as const
const ACTION_LABELS: Record<string, string> = { save_note: '保存笔记', delete_note: '删除笔记', import_chapters: '导入章节' }
const selectClass = 'h-8 rounded-md border bg-background px-2 text-xs text-foreground'

function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function BookLoreWorkbenchPage() {
  const [overview, setOverview] = useState<WorkbenchOverview | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState<unknown>()
  const [tab, setTab] = useState('overview')
  const [notesVersion, setNotesVersion] = useState(0)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setOverview(await getWorkbenchOverview(signal))
      setStatus('success')
    } catch (reason) {
      if (isRequestCancelled(reason)) return
      setError(reason)
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const changed = useCallback(async () => {
    setNotesVersion((value) => value + 1)
    await load()
  }, [load])

  return (
    <div className="flex flex-col gap-4" data-page="book-lore-workbench">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <header className="max-w-2xl">
          <h1 className="text-xl font-bold tracking-tight">书设工作台</h1>
          <p className="text-xs text-muted-foreground">
            维护书设笔记与新章节。保存即写库、算向量、更新检索索引，Bot 下一句话就能用到，不用重启。
            GraphRAG 的实体与社区摘要只读，浏览请到 <Link className="underline" to="/knowledge/book-lore">书设定</Link>。
          </p>
        </header>
        <Button type="button" size="sm" variant="outline" onClick={() => void load()}><RefreshCwIcon aria-hidden="true" />刷新</Button>
      </div>

      <QueryState status={status} error={error} title="书设工作台不可用" onRetry={() => void load()}>
        {overview ? (
          <Tabs value={tab} onValueChange={setTab} className="flex flex-col gap-4">
            <TabsList>
              <TabsTrigger value="overview">概览</TabsTrigger>
              <TabsTrigger value="import">导入章节</TabsTrigger>
              <TabsTrigger value="notes">笔记</TabsTrigger>
            </TabsList>
            <TabsContent value="overview" className="flex flex-col gap-4">
              <IndexHealth overview={overview} onChanged={changed} />
              <Sources overview={overview} onChanged={changed} />
              <RecentEdits overview={overview} />
            </TabsContent>
            <TabsContent value="import"><PasteImport overview={overview} onChanged={changed} /></TabsContent>
            <TabsContent value="notes"><NotesEditor overview={overview} version={notesVersion} onChanged={changed} /></TabsContent>
          </Tabs>
        ) : null}
      </QueryState>
    </div>
  )
}

function IndexHealth({ overview, onChanged }: { overview: WorkbenchOverview; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  async function sync() {
    setBusy(true)
    try {
      const result = await syncWorkbenchIndex()
      toast.success(`补齐 ${number.format(result.indexed)} 条（新算向量 ${number.format(result.embedded)}）${result.failed ? `，${result.failed} 条向量服务失败` : ''}${result.remaining ? `，还剩 ${number.format(result.remaining)} 条` : ''}`)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }
  const gaps = overview.notes_not_indexed + overview.stale_index_entries
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">检索索引</CardTitle>
        <CardDescription>书设检索与注入只认向量索引里的条目。库里有、索引里没有的笔记，Bot 看不到。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4">
        {gaps > 0 ? (
          <Alert>
            <DatabaseZapIcon aria-hidden="true" />
            <AlertTitle>{number.format(overview.notes_not_indexed)} 条笔记没进索引{overview.stale_index_entries ? `，${overview.stale_index_entries} 条索引指向已删除的笔记` : ''}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center gap-2">
              <span>多是之前手工插入库里、没重建索引的笔记{overview.notes_not_indexed_sample.length ? `（如 ${overview.notes_not_indexed_sample.slice(0, 4).join('、')}）` : ''}。</span>
              <Button type="button" size="sm" disabled={busy || !overview.available} onClick={() => void sync()}><DatabaseZapIcon aria-hidden="true" />补齐索引</Button>
            </AlertDescription>
          </Alert>
        ) : <p className="text-sm text-muted-foreground">库与索引一致。</p>}
        <table className="w-full text-sm tabular-nums">
          <thead className="text-xs text-muted-foreground"><tr><th className="text-left font-normal">类别</th><th className="text-right font-normal">库内</th><th className="text-right font-normal">有向量</th><th className="text-right font-normal">已入索引</th></tr></thead>
          <tbody>
            {(['notes', 'communities', 'entities'] as const).map((tier) => (
              <tr key={tier} className="border-t">
                <td className="py-1.5">{TIER_LABELS[tier]}</td>
                <td className="text-right">{number.format(overview.counts[tier].rows)}</td>
                <td className="text-right">{number.format(overview.counts[tier].with_vector)}</td>
                <td className="text-right">{number.format(overview.indexed[tier])}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-muted-foreground">章节笔记 {number.format(overview.chapter_count)} 章，最新第 {overview.latest_chapter ?? '—'} 章。</p>
      </CardContent>
    </Card>
  )
}

function ArcSelect({ overview, value, onChange, id }: { overview: WorkbenchOverview; value: string; onChange: (value: string) => void; id: string }) {
  return (
    <select id={id} aria-label="归入卷" className={selectClass} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">沿用最新一章的卷</option>
      {overview.facets.arcs.filter((arc) => arc.name).map((arc) => <option key={arc.name} value={arc.name}>{arc.name}（{arc.count}）</option>)}
    </select>
  )
}

function Sources({ overview, onChanged }: { overview: WorkbenchOverview; onChanged: () => Promise<void> }) {
  const [arc, setArc] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function run(path: string) {
    setBusy(true)
    try {
      const result = await importChapters({ path, arc })
      toast.success(`导入 ${result.chapters?.length ?? 0} 章，${result.indexed} 章已可检索${result.pending_vectors ? `，${result.pending_vectors} 章待补向量` : ''}`)
      setConfirming(null)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">小说原文</CardTitle>
        <CardDescription>扫描插件数据目录和 novel_docs 下的 .txt，按「第N章」切章，列出书设库里还没有的章节。更新原文文件后刷新即可看到。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-4">
        {overview.sources.length ? overview.sources.map((source) => (
          <div key={source.path} className="flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm">
            <span className="font-medium">{source.name}</span>
            <span className="text-xs text-muted-foreground">{number.format(source.chapters)} 章 · 最新 {source.latest_title} · {(source.size / 1024 / 1024).toFixed(1)} MB · 修改于 {when(source.modified_at)}</span>
            {source.new_chapters ? <Badge>{source.new_chapters} 章未入库（第 {source.new_range?.[0]}–{source.new_range?.[1]} 章）</Badge> : <Badge variant="secondary">已全部入库</Badge>}
            {source.new_chapters ? (
              <div className="ml-auto flex items-center gap-2">
                {confirming === source.path ? (
                  <>
                    <ArcSelect overview={overview} value={arc} onChange={setArc} id={`arc-${source.name}`} />
                    <Button type="button" size="sm" disabled={busy} onClick={() => void run(source.path)}>确认导入</Button>
                    <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>取消</Button>
                  </>
                ) : (
                  <Button type="button" size="sm" disabled={busy || !overview.available} onClick={() => setConfirming(source.path)}><BookPlusIcon aria-hidden="true" />导入新章节</Button>
                )}
              </div>
            ) : null}
          </div>
        )) : <p className="text-sm text-muted-foreground">没有找到小说原文。可以把 .txt 放进插件数据目录，或在「导入章节」里直接粘贴。</p>}
      </CardContent>
    </Card>
  )
}

function RecentEdits({ overview }: { overview: WorkbenchOverview }) {
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3"><CardTitle className="text-sm">最近改动</CardTitle></CardHeader>
      <CardContent className="p-4">
        {overview.recent_edits.length ? (
          <ul className="flex flex-col gap-1 text-xs">
            {overview.recent_edits.map((edit) => (
              <li key={edit.id} className="flex gap-2">
                <span className="shrink-0 text-muted-foreground">{when(edit.at)}</span>
                <Badge variant="outline">{ACTION_LABELS[edit.action] ?? edit.action}</Badge>
                <span className="font-mono">{edit.target}</span>
                {typeof edit.detail.title === 'string' ? <span className="truncate text-muted-foreground">{edit.detail.title}</span> : null}
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-muted-foreground">工作台还没有改动记录。</p>}
      </CardContent>
    </Card>
  )
}

function PasteImport({ overview, onChanged }: { overview: WorkbenchOverview; onChanged: () => Promise<void> }) {
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<ChapterPreview[] | null>(null)
  const [arc, setArc] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [busy, setBusy] = useState(false)
  async function parse() {
    setBusy(true)
    try {
      setPreview((await previewChapters(text)).chapters)
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }
  async function run() {
    setBusy(true)
    try {
      const result = await importChapters({ text, arc, overwrite })
      toast.success(`导入 ${result.chapters?.length ?? 0} 章，${result.indexed} 章已可检索`)
      setText('')
      setPreview(null)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }
  const importable = preview?.filter((chapter) => overwrite || !chapter.exists).length ?? 0
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">粘贴新章节</CardTitle>
        <CardDescription>每章以「第N章 标题」开头。导入后每章一条「章节事件」笔记（note_chN），当场算向量进索引。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4">
        <Textarea aria-label="章节文本" rows={10} value={text} onChange={(event) => { setText(event.target.value); setPreview(null) }} placeholder={'第962章 ……\n正文……\n\n第963章 ……'} />
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" size="sm" variant="outline" disabled={busy || !text.trim()} onClick={() => void parse()}>解析章节</Button>
          {preview ? (
            <>
              <ArcSelect overview={overview} value={arc} onChange={setArc} id="paste-arc" />
              <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />覆盖库里已有的章</label>
              <Button type="button" size="sm" disabled={busy || !importable || !overview.available} onClick={() => void run()}><FilePlus2Icon aria-hidden="true" />导入 {importable} 章</Button>
            </>
          ) : null}
        </div>
        {preview ? (
          preview.length ? (
            <ul className="flex max-h-64 flex-col gap-1 overflow-auto rounded-md border p-2 text-xs" aria-label="解析出的章节">
              {preview.map((chapter) => (
                <li key={chapter.number} className="flex gap-2">
                  <span className="w-14 shrink-0 font-mono">第{chapter.number}章</span>
                  <span className="min-w-0 flex-1 truncate">{chapter.title}</span>
                  <span className="shrink-0 text-muted-foreground">{number.format(chapter.length)} 字</span>
                  {chapter.exists ? <Badge variant="outline">已有</Badge> : <Badge>新</Badge>}
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted-foreground">没有找到「第N章」开头的章节。</p>
        ) : null}
      </CardContent>
    </Card>
  )
}

const EMPTY_NOTE: Partial<WorkbenchNote> = { id: '', title: '', category: '世界观', arc: '', content: '' }

function NotesEditor({ overview, version, onChanged }: { overview: WorkbenchOverview; version: number; onChanged: () => Promise<void> }) {
  const [filters, setFilters] = useState({ q: '', category: '', arc: '', indexed: '' as '' | '0' | '1' })
  const [draftQuery, setDraftQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<WorkbenchNoteRow[]>([])
  const [total, setTotal] = useState(0)
  const [editing, setEditing] = useState<Partial<WorkbenchNote> | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const limit = 30

  useEffect(() => {
    const controller = new AbortController()
    listWorkbenchNotes({ ...filters, limit, offset }, controller.signal)
      .then((page) => { setRows(page.items); setTotal(page.total) })
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason)) })
    return () => controller.abort()
  }, [filters, offset, version])

  async function open(id: string) {
    try {
      setEditing((await getWorkbenchNote(id)).item)
      setConfirmDelete(false)
    } catch (reason) {
      toast.error(errorText(reason))
    }
  }

  async function save() {
    if (!editing) return
    setBusy(true)
    try {
      const result = await saveWorkbenchNote(editing)
      toast.success(result.indexed ? '已保存，已可检索' : '已保存；向量服务暂时不可用，稍后在概览里补齐索引')
      setEditing(null)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!editing?.id) return
    setBusy(true)
    try {
      await deleteWorkbenchNote(editing.id)
      toast.success('已删除')
      setEditing(null)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="gap-2 border-b pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">笔记（{number.format(total)}）</CardTitle>
            <Button type="button" size="sm" variant="outline" onClick={() => { setEditing({ ...EMPTY_NOTE }); setConfirmDelete(false) }}><FilePlus2Icon aria-hidden="true" />新建笔记</Button>
          </div>
          <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters((f) => ({ ...f, q: draftQuery.trim() })) }}>
            <Input aria-label="搜索笔记" className="h-8 w-40 text-xs" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="标题 / 正文 / id" />
            <select aria-label="类别" className={selectClass} value={filters.category} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, category: event.target.value })) }}>
              <option value="">全部类别</option>
              {overview.facets.categories.map((c) => <option key={c.name} value={c.name}>{c.name || '（无）'}（{c.count}）</option>)}
            </select>
            <select aria-label="卷" className={selectClass} value={filters.arc} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, arc: event.target.value })) }}>
              <option value="">全部卷</option>
              {overview.facets.arcs.map((a) => <option key={a.name} value={a.name}>{a.name || '（无）'}（{a.count}）</option>)}
            </select>
            <select aria-label="索引状态" className={selectClass} value={filters.indexed} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, indexed: event.target.value as '' | '0' | '1' })) }}>
              <option value="">全部</option>
              <option value="0">未入索引</option>
              <option value="1">已入索引</option>
            </select>
            <Button type="submit" size="sm">查询</Button>
          </form>
        </CardHeader>
        <CardContent className="p-2">
          <ul className="flex flex-col divide-y" aria-label="笔记列表">
            {rows.map((row) => (
              <li key={row.id}>
                <button type="button" className="flex w-full flex-col gap-0.5 p-2 text-left hover:bg-muted/50" onClick={() => void open(row.id)}>
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{row.title}</span>
                    <Badge variant="outline">{row.category || '无类别'}</Badge>
                    {row.indexed ? null : <Badge variant="destructive">未入索引</Badge>}
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{row.id} · {row.arc || '无卷'} · {number.format(row.length)} 字 · {row.preview}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between p-2 text-xs text-muted-foreground">
            <span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} / ${number.format(total)}` : '没有匹配的笔记'}</span>
            <span className="flex gap-2">
              <Button type="button" size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</Button>
              <Button type="button" size="sm" variant="ghost" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</Button>
            </span>
          </div>
        </CardContent>
      </Card>

      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">{editing ? (editing.id ? `编辑 ${editing.id}` : '新建笔记') : '选择一条笔记编辑'}</CardTitle>
          {editing && 'indexed' in editing ? <CardDescription>{editing.indexed ? '已在检索索引里' : '还没进检索索引，保存后会自动加入'}</CardDescription> : null}
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4">
          {editing ? (
            <>
              <Input aria-label="标题" value={editing.title ?? ''} onChange={(event) => setEditing({ ...editing, title: event.target.value })} placeholder="标题" />
              <div className="flex flex-wrap gap-2">
                <Input aria-label="类别" className="w-40" list="workbench-categories" value={editing.category ?? ''} onChange={(event) => setEditing({ ...editing, category: event.target.value })} placeholder="类别" />
                <datalist id="workbench-categories">{overview.facets.categories.map((c) => <option key={c.name} value={c.name} />)}</datalist>
                <Input aria-label="卷" className="w-48" list="workbench-arcs" value={editing.arc ?? ''} onChange={(event) => setEditing({ ...editing, arc: event.target.value })} placeholder="卷（如 arc05_化神及以后）" />
                <datalist id="workbench-arcs">{overview.facets.arcs.map((a) => <option key={a.name} value={a.name} />)}</datalist>
              </div>
              <Textarea aria-label="正文" rows={16} value={editing.content ?? ''} onChange={(event) => setEditing({ ...editing, content: event.target.value })} placeholder="正文" />
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" disabled={busy || !editing.title?.trim() || !editing.content?.trim()} onClick={() => void save()}><SaveIcon aria-hidden="true" />保存并更新索引</Button>
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>取消</Button>
                {editing.id ? (
                  confirmDelete ? (
                    <>
                      <span className="self-center text-xs text-destructive">删除后 Bot 不再能检索到这条书设</span>
                      <Button type="button" size="sm" variant="destructive" disabled={busy} onClick={() => void remove()}>确认删除</Button>
                    </>
                  ) : (
                    <Button type="button" size="sm" variant="outline" className="ml-auto" disabled={busy} onClick={() => setConfirmDelete(true)}><Trash2Icon aria-hidden="true" />删除</Button>
                  )
                ) : null}
              </div>
            </>
          ) : <p className="text-sm text-muted-foreground">从左侧列表选择，或新建一条笔记。</p>}
        </CardContent>
      </Card>
    </div>
  )
}

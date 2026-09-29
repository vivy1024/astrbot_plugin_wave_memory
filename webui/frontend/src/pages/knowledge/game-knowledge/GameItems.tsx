import { useCallback, useEffect, useState } from 'react'
import { ArchiveIcon, CheckCheckIcon, ExternalLinkIcon, FilePlus2Icon, SaveIcon, Undo2Icon } from 'lucide-react'
import { toast } from 'sonner'

import { isRequestCancelled } from '@/api/client'
import {
  getGameCard,
  getGameTerm,
  listGameCards,
  listGameTerms,
  saveGameCard,
  saveGameTerm,
  setGameItemsStatus,
  type GameCard,
  type GameItemKind,
  type GameItemStatus,
  type GameLibrary,
  type GameTerm,
} from '@/api/gameKnowledge'
import { BatchActionBar } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

import { SOURCE_TYPE_LABELS, STATUS_LABELS, STATUS_VARIANTS, errorText, number, selectClass, splitLines, splitList, when } from './shared'

const PAGE_SIZE = 30

function triggerSummary(card: Pick<GameCard, 'keywords' | 'conditions_text'>): string {
  const parts: string[] = []
  if (card.keywords.length) parts.push(`关键词：${card.keywords.slice(0, 6).join('、')}${card.keywords.length > 6 ? '…' : ''}`)
  if (card.conditions_text.length) parts.push(`条件：${card.conditions_text.join('；')}`)
  return parts.join(' · ') || '没有触发条件：只能通过工具查到，不会自动注入'
}

function StatusBadge({ status }: { status: GameItemStatus }) {
  return <Badge variant={STATUS_VARIANTS[status]}>{STATUS_LABELS[status]}</Badge>
}

function SourceLink({ url }: { url: string }) {
  if (!url) return null
  return (
    <a className="inline-flex items-center gap-1 text-xs underline" href={url} target="_blank" rel="noreferrer">
      <ExternalLinkIcon className="size-3" aria-hidden="true" />查看出处
    </a>
  )
}

/** 编辑区底部的状态切换：带版本号走批量接口，对不上就提示刷新。 */
function StatusActions({ gameId, kind, id, version, status, disabled, onDone }: {
  gameId: string
  kind: GameItemKind
  id: number
  version: number
  status: GameItemStatus
  disabled: boolean
  onDone: () => Promise<void>
}) {
  async function change(target: GameItemStatus) {
    try {
      const result = await setGameItemsStatus(gameId, kind, [id], target, { [String(id)]: version })
      if (result.conflicts.length) toast.error('刚被别处改过，请重新打开后再改')
      else toast.success(`已${target === 'active' ? '启用' : target === 'archived' ? '归档' : '退回草稿'}`)
      await onDone()
    } catch (reason) {
      toast.error(errorText(reason, '改状态失败'))
    }
  }
  return (
    <>
      {status !== 'active' ? <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void change('active')}><CheckCheckIcon aria-hidden="true" />启用</Button> : null}
      {status !== 'archived' ? <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void change('archived')}><ArchiveIcon aria-hidden="true" />归档</Button> : null}
      {status !== 'draft' ? <Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => void change('draft')}><Undo2Icon aria-hidden="true" />退回草稿</Button> : null}
    </>
  )
}

interface CardForm {
  id?: number
  version?: number
  status: GameItemStatus
  title: string
  stage: string
  points: string
  pitfalls: string
  keywords: string
  conditions: string
  source: string
  source_url: string
  note: string
  source_type?: string
  updated_at?: number
}

const EMPTY_CARD: CardForm = { status: 'draft', title: '', stage: '', points: '', pitfalls: '', keywords: '', conditions: '', source: '', source_url: '', note: '' }

function cardForm(card: GameCard): CardForm {
  return {
    id: card.id,
    version: card.version,
    status: card.status,
    title: card.title,
    stage: card.stage,
    points: card.points.join('\n'),
    pitfalls: card.pitfalls.join('\n'),
    keywords: card.keywords.join('、'),
    conditions: card.conditions_text.join('\n'),
    source: card.source,
    source_url: card.source_url,
    note: card.note,
    source_type: card.source_type,
    updated_at: card.updated_at,
  }
}

function cardPayload(form: CardForm): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    title: form.title.trim(),
    stage: form.stage.trim(),
    points: splitLines(form.points),
    pitfalls: splitLines(form.pitfalls),
    keywords: splitList(form.keywords),
    conditions: splitLines(form.conditions),
    source: form.source.trim(),
    source_url: form.source_url.trim(),
    note: form.note.trim(),
  }
  if (form.id === undefined) payload.status = form.status
  return payload
}

export function CardsPanel({ game, stages, triggerRule, focusId, focusNonce, version, onChanged }: {
  game: GameLibrary
  stages: Array<{ name: string; count: number }>
  triggerRule: string
  focusId?: number
  focusNonce?: number
  version: number
  onChanged: () => Promise<void>
}) {
  const [filters, setFilters] = useState({ q: '', stage: '', status: '' })
  const [draftQuery, setDraftQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<GameCard[]>([])
  const [total, setTotal] = useState(0)
  const [editing, setEditing] = useState<CardForm | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    listGameCards(game.id, { ...filters, limit: PAGE_SIZE, offset }, controller.signal)
      .then((page) => { setRows(page.items); setTotal(page.total) })
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason, '知识卡加载失败')) })
    return () => controller.abort()
  }, [game.id, filters, offset, version, reload])

  const open = useCallback(async (id: number) => {
    try {
      setEditing(cardForm((await getGameCard(game.id, id)).item))
    } catch (reason) {
      toast.error(errorText(reason, '知识卡加载失败'))
    }
  }, [game.id])

  useEffect(() => {
    if (focusId !== undefined) void open(focusId)
  }, [focusId, focusNonce, open])

  const refresh = useCallback(async () => {
    setReload((value) => value + 1)
    await onChanged()
  }, [onChanged])

  async function save() {
    if (!editing) return
    setBusy(true)
    try {
      const result = await saveGameCard(game.id, cardPayload(editing), editing.id, editing.version)
      toast.success(editing.id === undefined ? '已新建知识卡' : '已保存')
      setEditing(cardForm(result.item))
      await refresh()
    } catch (reason) {
      toast.error(errorText(reason, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  async function reopen() {
    if (editing?.id !== undefined) await open(editing.id)
    await refresh()
  }

  const set = (patch: Partial<CardForm>) => setEditing((current) => (current ? { ...current, ...patch } : current))
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="gap-2 border-b pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">知识卡（{number.format(total)}）</CardTitle>
            <Button type="button" size="sm" variant="outline" onClick={() => setEditing({ ...EMPTY_CARD })}><FilePlus2Icon aria-hidden="true" />新建知识卡</Button>
          </div>
          <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters((f) => ({ ...f, q: draftQuery.trim() })) }}>
            <Input aria-label="搜索知识卡" className="h-8 w-40 text-xs" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="标题 / 正文 / 关键词" />
            <select aria-label="按阶段筛选" className={selectClass} value={filters.stage} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, stage: event.target.value })) }}>
              <option value="">全部阶段</option>
              {stages.filter((stage) => stage.name).map((stage) => <option key={stage.name} value={stage.name}>{stage.name}（{stage.count}）</option>)}
            </select>
            <select aria-label="按状态筛选" className={selectClass} value={filters.status} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, status: event.target.value })) }}>
              <option value="">全部状态</option>
              <option value="active">启用</option>
              <option value="draft">草稿</option>
              <option value="archived">归档</option>
            </select>
            <Button type="submit" size="sm">查询</Button>
          </form>
        </CardHeader>
        <CardContent className="p-2">
          <ul className="flex flex-col divide-y" aria-label="知识卡列表">
            {rows.map((card) => (
              <li key={card.id}>
                <button type="button" className="flex w-full flex-col gap-0.5 p-2 text-left hover:bg-muted/50" onClick={() => void open(card.id)}>
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{card.title}</span>
                    {card.stage ? <Badge variant="outline">{card.stage}</Badge> : null}
                    <StatusBadge status={card.status} />
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{triggerSummary(card)}</span>
                  {card.note ? <span className="truncate text-xs text-amber-700 dark:text-amber-400">{card.note}</span> : null}
                </button>
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between p-2 text-xs text-muted-foreground">
            <span>{total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} / ${number.format(total)}` : '没有匹配的知识卡'}</span>
            <span className="flex gap-2">
              <Button type="button" size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
              <Button type="button" size="sm" variant="ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
            </span>
          </div>
        </CardContent>
      </Card>

      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">{editing ? (editing.id !== undefined ? `编辑知识卡 · 版本 ${editing.version}` : '新建知识卡') : '选择一张知识卡编辑'}</CardTitle>
          <CardDescription>{triggerRule}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4">
          {editing ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={editing.status} />
                {editing.source_type ? <Badge variant="outline">{SOURCE_TYPE_LABELS[editing.source_type] ?? editing.source_type}</Badge> : null}
                {editing.updated_at ? <span className="text-xs text-muted-foreground">更新于 {when(editing.updated_at)}</span> : null}
                <SourceLink url={editing.source_url} />
              </div>
              <Input aria-label="标题" value={editing.title} onChange={(event) => set({ title: event.target.value })} placeholder="标题，如「第一夜怎么过」" />
              <Input aria-label="阶段" list="game-stages" value={editing.stage} onChange={(event) => set({ stage: event.target.value })} placeholder="阶段（自由填写，如 前期 / 筑基开局）" />
              <datalist id="game-stages">{stages.filter((s) => s.name).map((s) => <option key={s.name} value={s.name} />)}</datalist>
              <Textarea aria-label="要点" rows={5} value={editing.points} onChange={(event) => set({ points: event.target.value })} placeholder="要点 / 建议，每行一条" />
              <Textarea aria-label="注意" rows={3} value={editing.pitfalls} onChange={(event) => set({ pitfalls: event.target.value })} placeholder="坑 / 注意，每行一条" />
              <Input aria-label="触发关键词" value={editing.keywords} onChange={(event) => set({ keywords: event.target.value })} placeholder="触发关键词，用逗号或顿号分隔，如 苦力怕、爬行者" />
              <Textarea aria-label="数值条件" rows={2} value={editing.conditions} onChange={(event) => set({ conditions: event.target.value })} placeholder={'数值条件，每行一条：cycle 30..60 · health < 8 · time_of_day == night · dimension in nether, end · 前面加「必须」表示缺键不触发'} />
              <div className="grid gap-2 sm:grid-cols-2">
                <Input aria-label="出处" value={editing.source} onChange={(event) => set({ source: event.target.value })} placeholder="出处（教程、版本、作者…）" />
                <Input aria-label="出处链接" value={editing.source_url} onChange={(event) => set({ source_url: event.target.value })} placeholder="https://…" />
              </div>
              <Textarea aria-label="备注" rows={2} value={editing.note} onChange={(event) => set({ note: event.target.value })} placeholder="审核备注（不注入）" />
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" disabled={busy || !editing.title.trim() || !(editing.points.trim() || editing.pitfalls.trim())} onClick={() => void save()}><SaveIcon aria-hidden="true" />保存</Button>
                {editing.id !== undefined && editing.version !== undefined ? (
                  <StatusActions gameId={game.id} kind="cards" id={editing.id} version={editing.version} status={editing.status} disabled={busy} onDone={reopen} />
                ) : (
                  <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={editing.status === 'active'} onChange={(event) => set({ status: event.target.checked ? 'active' : 'draft' })} />保存后直接启用</label>
                )}
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>关闭</Button>
              </div>
            </>
          ) : <p className="text-sm text-muted-foreground">从左侧列表选择，或新建一张知识卡。只有「启用」的卡会在直播时注入、被工具查到。</p>}
        </CardContent>
      </Card>
    </div>
  )
}

interface TermForm {
  id?: number
  version?: number
  status: GameItemStatus
  term: string
  aliases: string
  explanation: string
  related: string
  source: string
  source_url: string
  note: string
  source_type?: string
}

const EMPTY_TERM: TermForm = { status: 'draft', term: '', aliases: '', explanation: '', related: '', source: '', source_url: '', note: '' }

function termForm(term: GameTerm): TermForm {
  return {
    id: term.id,
    version: term.version,
    status: term.status,
    term: term.term,
    aliases: term.aliases.join('、'),
    explanation: term.explanation,
    related: term.related.join('、'),
    source: term.source,
    source_url: term.source_url,
    note: term.note,
    source_type: term.source_type,
  }
}

function termPayload(form: TermForm): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    term: form.term.trim(),
    aliases: splitList(form.aliases),
    explanation: form.explanation.trim(),
    related: splitList(form.related),
    source: form.source.trim(),
    source_url: form.source_url.trim(),
    note: form.note.trim(),
  }
  if (form.id === undefined) payload.status = form.status
  return payload
}

export function TermsPanel({ game, focusId, focusNonce, version, onChanged }: { game: GameLibrary; focusId?: number; focusNonce?: number; version: number; onChanged: () => Promise<void> }) {
  const [filters, setFilters] = useState({ q: '', status: '' })
  const [draftQuery, setDraftQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<GameTerm[]>([])
  const [total, setTotal] = useState(0)
  const [editing, setEditing] = useState<TermForm | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    listGameTerms(game.id, { ...filters, limit: PAGE_SIZE, offset }, controller.signal)
      .then((page) => { setRows(page.items); setTotal(page.total) })
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason, '词条加载失败')) })
    return () => controller.abort()
  }, [game.id, filters, offset, version, reload])

  const open = useCallback(async (id: number) => {
    try {
      setEditing(termForm((await getGameTerm(game.id, id)).item))
    } catch (reason) {
      toast.error(errorText(reason, '词条加载失败'))
    }
  }, [game.id])

  useEffect(() => {
    if (focusId !== undefined) void open(focusId)
  }, [focusId, focusNonce, open])

  const refresh = useCallback(async () => {
    setReload((value) => value + 1)
    await onChanged()
  }, [onChanged])

  async function save() {
    if (!editing) return
    setBusy(true)
    try {
      const result = await saveGameTerm(game.id, termPayload(editing), editing.id, editing.version)
      toast.success(editing.id === undefined ? '已新建词条' : '已保存')
      setEditing(termForm(result.item))
      await refresh()
    } catch (reason) {
      toast.error(errorText(reason, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  async function reopen() {
    if (editing?.id !== undefined) await open(editing.id)
    await refresh()
  }

  const set = (patch: Partial<TermForm>) => setEditing((current) => (current ? { ...current, ...patch } : current))
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="gap-2 border-b pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">词条（{number.format(total)}）</CardTitle>
            <Button type="button" size="sm" variant="outline" onClick={() => setEditing({ ...EMPTY_TERM })}><FilePlus2Icon aria-hidden="true" />新建词条</Button>
          </div>
          <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters((f) => ({ ...f, q: draftQuery.trim() })) }}>
            <Input aria-label="搜索词条" className="h-8 w-40 text-xs" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="术语 / 别名 / 解释" />
            <select aria-label="词条状态" className={selectClass} value={filters.status} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, status: event.target.value })) }}>
              <option value="">全部状态</option>
              <option value="active">启用</option>
              <option value="draft">草稿</option>
              <option value="archived">归档</option>
            </select>
            <Button type="submit" size="sm">查询</Button>
          </form>
        </CardHeader>
        <CardContent className="p-2">
          <ul className="flex flex-col divide-y" aria-label="词条列表">
            {rows.map((term) => (
              <li key={term.id}>
                <button type="button" className="flex w-full flex-col gap-0.5 p-2 text-left hover:bg-muted/50" onClick={() => void open(term.id)}>
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{term.term}</span>
                    {term.aliases.length ? <span className="text-xs text-muted-foreground">{term.aliases.join('、')}</span> : null}
                    <StatusBadge status={term.status} />
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{term.explanation}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between p-2 text-xs text-muted-foreground">
            <span>{total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} / ${number.format(total)}` : '没有匹配的词条'}</span>
            <span className="flex gap-2">
              <Button type="button" size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
              <Button type="button" size="sm" variant="ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
            </span>
          </div>
        </CardContent>
      </Card>

      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">{editing ? (editing.id !== undefined ? `编辑词条 · 版本 ${editing.version}` : '新建词条') : '选择一个词条编辑'}</CardTitle>
          <CardDescription>消息、场景关键词或状态里出现术语或别名时，把解释附在攻略参考后面（每轮最多几条，见通道 top_k）。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4">
          {editing ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={editing.status} />
                {editing.source_type ? <Badge variant="outline">{SOURCE_TYPE_LABELS[editing.source_type] ?? editing.source_type}</Badge> : null}
                <SourceLink url={editing.source_url} />
              </div>
              <Input aria-label="术语" value={editing.term} onChange={(event) => set({ term: event.target.value })} placeholder="术语，如「苦力怕」" />
              <Input aria-label="别名" value={editing.aliases} onChange={(event) => set({ aliases: event.target.value })} placeholder="别名，用逗号或顿号分隔" />
              <Textarea aria-label="解释" rows={3} value={editing.explanation} onChange={(event) => set({ explanation: event.target.value })} placeholder="一两句话的解释" />
              <Input aria-label="相关条目" value={editing.related} onChange={(event) => set({ related: event.target.value })} placeholder="相关知识卡的键或词条，用逗号分隔" />
              <div className="grid gap-2 sm:grid-cols-2">
                <Input aria-label="词条出处" value={editing.source} onChange={(event) => set({ source: event.target.value })} placeholder="出处" />
                <Input aria-label="词条出处链接" value={editing.source_url} onChange={(event) => set({ source_url: event.target.value })} placeholder="https://…" />
              </div>
              <Textarea aria-label="词条备注" rows={2} value={editing.note} onChange={(event) => set({ note: event.target.value })} placeholder="审核备注（不注入）" />
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" disabled={busy || !editing.term.trim() || !editing.explanation.trim()} onClick={() => void save()}><SaveIcon aria-hidden="true" />保存</Button>
                {editing.id !== undefined && editing.version !== undefined ? (
                  <StatusActions gameId={game.id} kind="terms" id={editing.id} version={editing.version} status={editing.status} disabled={busy} onDone={reopen} />
                ) : (
                  <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={editing.status === 'active'} onChange={(event) => set({ status: event.target.checked ? 'active' : 'draft' })} />保存后直接启用</label>
                )}
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>关闭</Button>
              </div>
            </>
          ) : <p className="text-sm text-muted-foreground">从左侧列表选择，或新建一个词条。</p>}
        </CardContent>
      </Card>
    </div>
  )
}

type DraftRow = { id: number; version: number; title: string; stage: string; detail: string; note: string; source: string; sourceType: string }

function toDraftRows(kind: GameItemKind, items: Array<GameCard | GameTerm>): DraftRow[] {
  return items.map((item) => kind === 'cards'
    ? { id: item.id, version: item.version, title: (item as GameCard).title, stage: (item as GameCard).stage, detail: triggerSummary(item as GameCard), note: item.note, source: item.source, sourceType: item.source_type }
    : { id: item.id, version: item.version, title: (item as GameTerm).term, stage: '', detail: (item as GameTerm).explanation, note: item.note, source: item.source, sourceType: item.source_type })
}

export function ReviewPanel({ game, version, onChanged, onEdit }: {
  game: GameLibrary
  version: number
  onChanged: () => Promise<void>
  onEdit: (kind: GameItemKind, id: number) => void
}) {
  const [kind, setKind] = useState<GameItemKind>('cards')
  const [sourceType, setSourceType] = useState('')
  const [rows, setRows] = useState<DraftRow[]>([])
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const params = { status: 'draft', source_type: sourceType, limit: 200 }
    const request = kind === 'cards' ? listGameCards(game.id, params, controller.signal) : listGameTerms(game.id, params, controller.signal)
    request
      .then((page) => { setRows(toDraftRows(kind, page.items)); setTotal(page.total); setSelected(new Set()) })
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason, '草稿加载失败')) })
    return () => controller.abort()
  }, [game.id, kind, sourceType, version, reload])

  function toggle(id: number) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function apply(status: GameItemStatus) {
    const ids = rows.filter((row) => selected.has(row.id)).map((row) => row.id)
    if (!ids.length) return
    const versions = Object.fromEntries(rows.filter((row) => selected.has(row.id)).map((row) => [String(row.id), row.version]))
    setBusy(true)
    try {
      const result = await setGameItemsStatus(game.id, kind, ids, status, versions)
      const verb = status === 'active' ? '启用' : '归档'
      toast.success(`已${verb} ${result.changed.length} 条${result.conflicts.length ? `，${result.conflicts.length} 条刚被改过已跳过` : ''}`)
      setReload((value) => value + 1)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason, '批量操作失败'))
    } finally {
      setBusy(false)
    }
  }

  const allSelected = rows.length > 0 && rows.every((row) => selected.has(row.id))
  return (
    <Card className="border-border/60">
      <CardHeader className="gap-2 border-b pb-3">
        <CardTitle className="text-sm">草稿审核（{number.format(total)}）</CardTitle>
        <CardDescription>导入的起始包、记忆表迁移来的知识点都是草稿：看过、改好再启用。启用后直播时才会注入，工具才查得到。</CardDescription>
        <div className="flex flex-wrap items-center gap-2">
          <select aria-label="草稿类型" className={selectClass} value={kind} onChange={(event) => setKind(event.target.value as GameItemKind)}>
            <option value="cards">知识卡</option>
            <option value="terms">词条</option>
          </select>
          <select aria-label="草稿来源" className={selectClass} value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
            <option value="">全部来源</option>
            {Object.entries(SOURCE_TYPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <label className="flex items-center gap-1 text-xs"><input type="checkbox" aria-label="全选草稿" checked={allSelected} onChange={() => setSelected(allSelected ? new Set() : new Set(rows.map((row) => row.id)))} />全选（本页 {rows.length} 条）</label>
        </div>
        <BatchActionBar selectedCount={selected.size} onClear={() => setSelected(new Set())} disabled={busy}>
          <Button type="button" size="sm" disabled={busy} onClick={() => void apply('active')}><CheckCheckIcon aria-hidden="true" />批量启用</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void apply('archived')}><ArchiveIcon aria-hidden="true" />批量归档</Button>
        </BatchActionBar>
      </CardHeader>
      <CardContent className="p-2">
        {rows.length ? (
          <ul className="flex flex-col divide-y" aria-label="草稿列表">
            {rows.map((row) => (
              <li key={row.id} className="flex items-start gap-2 p-2">
                <input type="checkbox" className="mt-1" aria-label={`选择 ${row.title}`} checked={selected.has(row.id)} onChange={() => toggle(row.id)} />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{row.title}</span>
                    {row.stage ? <Badge variant="outline">{row.stage}</Badge> : null}
                    <Badge variant="outline">{SOURCE_TYPE_LABELS[row.sourceType] ?? row.sourceType}</Badge>
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{row.detail}</span>
                  {row.note ? <span className="text-xs text-amber-700 dark:text-amber-400">{row.note}</span> : null}
                  {row.source ? <span className="truncate text-xs text-muted-foreground">出处：{row.source}</span> : null}
                </div>
                <Button type="button" size="sm" variant="ghost" onClick={() => onEdit(kind, row.id)}>编辑</Button>
              </li>
            ))}
          </ul>
        ) : <p className="p-2 text-sm text-muted-foreground">没有待审核的草稿。</p>}
      </CardContent>
    </Card>
  )
}

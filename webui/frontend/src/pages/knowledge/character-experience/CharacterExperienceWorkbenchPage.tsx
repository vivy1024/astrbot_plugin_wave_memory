import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArchiveIcon, CheckCircle2Icon, DatabaseZapIcon, FilePlus2Icon, RefreshCwIcon, SaveIcon, SearchIcon, Undo2Icon } from 'lucide-react'
import { toast } from 'sonner'

import {
  CANON_LABELS,
  EMOTIONS,
  SOURCE_KIND_LABELS,
  STATUS_LABELS,
  UNASSIGNED_STAGE,
  createCharacterExperience,
  getCharacterExperience,
  getCharacterOverview,
  listCharacterExperiences,
  saveCharacterSettings,
  setCharacterExperienceStage,
  setCharacterExperienceStatus,
  startEmbedding,
  tryCharacterRecall,
  updateCharacterExperience,
  type CanonLevel,
  type CharacterExperience,
  type CharacterOverview,
  type ExperienceStatus,
  type RecallPreview,
} from '@/api/characterExperience'
import { isRequestCancelled } from '@/api/client'
import { useGlobalScope } from '@/app/global-scope'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

import { CharacterTimelinePanel } from './CharacterTimelinePanel'
import { ImportExportPanel, NovelExtractPanel, SettingComposePanel } from './CharacterSourcesPanel'
import { csv, errorText, fromCsv, jobActive, number, selectClass, stageLabel, when } from './shared'

export function CharacterExperienceWorkbenchPage() {
  const { botId } = useGlobalScope()
  const [overview, setOverview] = useState<CharacterOverview | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error' | 'empty'>('loading')
  const [error, setError] = useState<unknown>()
  const [tab, setTab] = useState('experiences')
  const [listVersion, setListVersion] = useState(0)
  // 序号守卫：切换 Bot 时旧请求可能后到，丢掉过期响应，免得把别的 Bot 的经历显示出来
  const request = useRef(0)

  const load = useCallback(async () => {
    const id = ++request.current
    if (!botId) {
      setOverview(null)
      setStatus('empty')
      return
    }
    try {
      const data = await getCharacterOverview(botId)
      if (id !== request.current) return
      setOverview(data)
      setStatus('success')
    } catch (reason) {
      if (id !== request.current || isRequestCancelled(reason)) return
      setError(reason)
      setStatus('error')
    }
  }, [botId])

  useEffect(() => {
    setStatus('loading')
    void load()
  }, [load])

  // 有任务在跑时定时刷新进度；任务跑完刷新一次经历列表（新写入的草稿）
  const running = Boolean(overview?.jobs.some(jobActive))
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) setListVersion((value) => value + 1)
    wasRunning.current = running
    if (!running) return
    const timer = window.setInterval(() => { void load() }, 4000)
    return () => window.clearInterval(timer)
  }, [running, load])

  const changed = useCallback(async () => {
    setListVersion((value) => value + 1)
    await load()
  }, [load])

  return (
    <div className="flex flex-col gap-4" data-page="character-experience-workbench">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <header className="max-w-3xl">
          <h1 className="text-xl font-bold tracking-tight">角色经历工作台</h1>
          <p className="text-xs text-muted-foreground">
            书中角色、原创角色进群之前「过去的人生」：从原著提取、按设定编写、手写或导入，审阅后启用。
            只有在 Bot 资料里开启了角色经历的 Bot 会想起，且只想起「当前时间点」及之前的经历；别的 Bot 完全看不到。
            群里长出来的经历在 <Link className="underline" to="/knowledge/experiences">经历片段</Link>。
          </p>
        </header>
        <Button type="button" size="sm" variant="outline" onClick={() => void changed()}><RefreshCwIcon aria-hidden="true" />刷新</Button>
      </div>

      <QueryState
        status={status}
        error={error}
        title="角色经历工作台不可用"
        emptyTitle="先选一个 Bot"
        emptyDescription="在顶栏选择要管理的 Bot，这里只显示她自己的角色经历。"
        onRetry={() => void load()}
      >
        {overview && botId && overview.bot.db_id === botId ? (
          <>
            <CharacterSummary botId={botId} overview={overview} onChanged={changed} />
            <Tabs value={tab} onValueChange={setTab} className="flex flex-col gap-4">
              <TabsList className="flex-wrap">
                <TabsTrigger value="experiences">经历</TabsTrigger>
                <TabsTrigger value="timeline">时间线</TabsTrigger>
                <TabsTrigger value="extract">从原著提取</TabsTrigger>
                <TabsTrigger value="compose">按设定编写</TabsTrigger>
                <TabsTrigger value="io">导入导出</TabsTrigger>
                <TabsTrigger value="try">试一试</TabsTrigger>
              </TabsList>
              <TabsContent value="experiences"><ExperienceBrowser botId={botId} overview={overview} version={listVersion} onChanged={changed} /></TabsContent>
              <TabsContent value="timeline"><CharacterTimelinePanel botId={botId} overview={overview} onChanged={changed} /></TabsContent>
              <TabsContent value="extract"><NovelExtractPanel botId={botId} overview={overview} onChanged={changed} /></TabsContent>
              <TabsContent value="compose"><SettingComposePanel botId={botId} overview={overview} onChanged={changed} /></TabsContent>
              <TabsContent value="io"><ImportExportPanel botId={botId} overview={overview} onChanged={changed} /></TabsContent>
              <TabsContent value="try"><TryRecall botId={botId} overview={overview} /></TabsContent>
            </Tabs>
          </>
        ) : null}
      </QueryState>
    </div>
  )
}

function CharacterSummary({ botId, overview, onChanged }: { botId: string; overview: CharacterOverview; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const { settings, counts, bot } = overview
  const current = overview.stages.find((stage) => stage.stage_key === settings.current_stage)

  async function patch(next: Parameters<typeof saveCharacterSettings>[2], success: string) {
    setBusy(true)
    try {
      await saveCharacterSettings(botId, bot.version, next)
      toast.success(success)
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  async function embed() {
    setBusy(true)
    try {
      const result = await startEmbedding(botId)
      toast.success(result.job ? '已排队补算向量' : '启用的经历都有向量了')
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
        <CardTitle className="text-sm">{bot.name} 的角色经历</CardTitle>
        <CardDescription>
          召回条数、注入上限等在 <Link className="underline" to={{ pathname: '/bots', search: `?bot=${encodeURIComponent(botId)}&tab=character` }}>Bot 管理 · 角色经历</Link> 里改；这里的开关与时间点同样写进 Bot 资料（带版本检查）。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="flex items-center gap-2">
            <Switch aria-label="启用角色经历" checked={settings.enabled} disabled={busy}
              onCheckedChange={(next) => void patch({ enabled: next }, next ? '已开启角色经历，下一句话起生效' : '已关闭角色经历')} />
            <span>{settings.enabled ? '已启用' : '未启用'}</span>
          </label>
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">当前时间点</span>
            <select aria-label="当前时间点" className={selectClass} value={settings.current_stage} disabled={busy}
              onChange={(event) => void patch({ current_stage: event.target.value }, '当前时间点已更新')}>
              <option value="">不限（能想起所有阶段）</option>
              {overview.stages.map((stage) => <option key={stage.stage_key} value={stage.stage_key}>{stage.ordinal}. {stage.title}{stage.story_time ? `（${stage.story_time}）` : ''}</option>)}
            </select>
          </label>
          <span className="text-muted-foreground">
            {current ? `只会想起「${current.title}」及之前的经历` : settings.current_stage ? '当前时间点不在时间线里' : '没有时间点限制'}
          </span>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          {(Object.keys(STATUS_LABELS) as ExperienceStatus[]).map((key) => (
            <Badge key={key} variant={key === 'active' ? 'default' : 'secondary'}>{STATUS_LABELS[key]} {number.format(counts.by_status[key] ?? 0)}</Badge>
          ))}
          <Badge variant="outline">现在能想起 {number.format(overview.recallable_active)} 条</Badge>
          {(Object.keys(CANON_LABELS) as CanonLevel[]).map((key) => counts.by_canon[key] ? <Badge key={key} variant="outline">{CANON_LABELS[key]} {number.format(counts.by_canon[key])}</Badge> : null)}
          <span className="text-muted-foreground">
            模型：{overview.model.available ? `${overview.model.primary || '—'}（${overview.model.source_label || '模型分工'}）` : '没有可用模型（在配置中心「模型分工」里给「角色经历」指定）'}
          </span>
        </div>
        {overview.warnings.length ? (
          <Alert>
            <AlertTitle>需要留意</AlertTitle>
            <AlertDescription>
              <ul className="list-disc pl-4">
                {overview.warnings.map((warning) => <li key={warning.code}>{warning.message}</li>)}
              </ul>
              {counts.active_missing_vectors ? (
                <Button type="button" size="sm" className="mt-2" disabled={busy || !overview.embedding_available} onClick={() => void embed()}>
                  <DatabaseZapIcon aria-hidden="true" />补算向量
                </Button>
              ) : null}
            </AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  )
}

type EditorState = { item: Partial<CharacterExperience>; original: CharacterExperience | null }

const EDITABLE: Array<keyof CharacterExperience> = ['content', 'story_time', 'stage_key', 'plot_stage', 'emotion', 'characters', 'canon_level', 'seq']

function diffFields(original: CharacterExperience, item: Partial<CharacterExperience>): Partial<CharacterExperience> {
  const out: Record<string, unknown> = {}
  for (const key of EDITABLE) {
    if (JSON.stringify(item[key]) !== JSON.stringify(original[key])) out[key] = item[key]
  }
  return out as Partial<CharacterExperience>
}

function ExperienceBrowser({ botId, overview, version, onChanged }: { botId: string; overview: CharacterOverview; version: number; onChanged: () => Promise<void> }) {
  const [filters, setFilters] = useState({ stage: '', status: '', canon: '', q: '' })
  const [draftQuery, setDraftQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<CharacterExperience[]>([])
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState<Map<number, number>>(new Map())
  const [targetStage, setTargetStage] = useState('')
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [busy, setBusy] = useState(false)
  const limit = 30

  useEffect(() => {
    const controller = new AbortController()
    listCharacterExperiences(botId, { ...filters, limit, offset }, controller.signal)
      .then((page) => { setRows(page.items); setTotal(page.total) })
      .catch((reason) => { if (!isRequestCancelled(reason)) toast.error(errorText(reason)) })
    return () => controller.abort()
  }, [botId, filters, offset, version])

  useEffect(() => { setSelected(new Map()); setEditor(null) }, [botId])

  function toggle(row: CharacterExperience) {
    setSelected((current) => {
      const next = new Map(current)
      if (next.has(row.id)) next.delete(row.id)
      else next.set(row.id, row.version)
      return next
    })
  }

  async function bulk(action: 'status' | 'stage', value: string) {
    const items = [...selected].map(([id, rowVersion]) => ({ id, version: rowVersion }))
    if (!items.length) return
    setBusy(true)
    try {
      if (action === 'status') {
        await setCharacterExperienceStatus(botId, items, value as ExperienceStatus)
        toast.success(`${items.length} 条已${STATUS_LABELS[value as ExperienceStatus] ?? value}`)
      } else {
        await setCharacterExperienceStage(botId, items, value)
        toast.success(`${items.length} 条已移到「${stageLabel(overview.stages, value)}」`)
      }
      setSelected(new Map())
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  async function open(id: number) {
    try {
      const detail = await getCharacterExperience(botId, id)
      setEditor({ item: { ...detail.item }, original: detail.item })
    } catch (reason) {
      toast.error(errorText(reason))
    }
  }

  async function save(nextStatus?: ExperienceStatus) {
    if (!editor) return
    setBusy(true)
    try {
      if (editor.original) {
        const fields = diffFields(editor.original, editor.item)
        if (nextStatus && nextStatus !== editor.original.status) fields.status = nextStatus
        const result = await updateCharacterExperience(botId, editor.original.id, editor.original.version, fields)
        setEditor({ item: { ...result.item }, original: result.item })
        toast.success(`已保存（版本 ${result.item.version}）`)
      } else {
        const result = await createCharacterExperience(botId, { ...editor.item, status: nextStatus ?? 'draft' })
        setEditor({ item: { ...result.item }, original: result.item })
        toast.success('已新建')
      }
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const allOnPage = rows.length > 0 && rows.every((row) => selected.has(row.id))
  const unassignedSelected = [...selected.keys()].filter((id) => rows.find((row) => row.id === id && !row.stage_key)).length

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="gap-2 border-b pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">经历（{number.format(total)}）</CardTitle>
            <Button type="button" size="sm" variant="outline" onClick={() => setEditor({ item: { content: '', canon_level: 'invented', stage_key: '', characters: [] }, original: null })}>
              <FilePlus2Icon aria-hidden="true" />手写一条
            </Button>
          </div>
          <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters((f) => ({ ...f, q: draftQuery.trim() })) }}>
            <Input aria-label="搜索经历" className="h-8 w-40 text-xs" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="正文 / 人物 / 时间" />
            <select aria-label="按阶段" className={selectClass} value={filters.stage} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, stage: event.target.value })) }}>
              <option value="">全部阶段</option>
              <option value={UNASSIGNED_STAGE}>未分阶段</option>
              {overview.stages.map((stage) => <option key={stage.stage_key} value={stage.stage_key}>{stage.ordinal}. {stage.title}</option>)}
            </select>
            <select aria-label="按状态" className={selectClass} value={filters.status} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, status: event.target.value })) }}>
              <option value="">全部状态</option>
              {(Object.keys(STATUS_LABELS) as ExperienceStatus[]).map((key) => <option key={key} value={key}>{STATUS_LABELS[key]}</option>)}
            </select>
            <select aria-label="按可信级别" className={selectClass} value={filters.canon} onChange={(event) => { setOffset(0); setFilters((f) => ({ ...f, canon: event.target.value })) }}>
              <option value="">全部级别</option>
              {(Object.keys(CANON_LABELS) as CanonLevel[]).map((key) => <option key={key} value={key}>{CANON_LABELS[key]}</option>)}
            </select>
            <Button type="submit" size="sm"><SearchIcon aria-hidden="true" />查询</Button>
          </form>
          {selected.size ? (
            <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 p-2 text-xs" data-slot="bulk-actions">
              <span>已选 {selected.size} 条</span>
              <Button type="button" size="sm" disabled={busy} onClick={() => void bulk('status', 'active')}><CheckCircle2Icon aria-hidden="true" />批量启用</Button>
              <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void bulk('status', 'archived')}><ArchiveIcon aria-hidden="true" />归档</Button>
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void bulk('status', 'draft')}><Undo2Icon aria-hidden="true" />退回草稿</Button>
              <select aria-label="移到阶段" className={selectClass} value={targetStage} onChange={(event) => setTargetStage(event.target.value)}>
                <option value="">不分阶段</option>
                {overview.stages.map((stage) => <option key={stage.stage_key} value={stage.stage_key}>{stage.ordinal}. {stage.title}</option>)}
              </select>
              <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void bulk('stage', targetStage)}>移到阶段</Button>
              {unassignedSelected && overview.settings.current_stage ? <span className="text-muted-foreground">其中 {unassignedSelected} 条没分阶段：启用后任何时间点都能想起</span> : null}
            </div>
          ) : null}
        </CardHeader>
        <CardContent className="p-2">
          <label className="flex items-center gap-2 px-2 pb-1 text-xs text-muted-foreground">
            <input type="checkbox" aria-label="全选本页" checked={allOnPage}
              onChange={() => setSelected((current) => {
                const next = new Map(current)
                if (allOnPage) rows.forEach((row) => next.delete(row.id))
                else rows.forEach((row) => next.set(row.id, row.version))
                return next
              })} />
            全选本页
          </label>
          <ul className="flex flex-col divide-y" aria-label="经历列表">
            {rows.map((row) => (
              <li key={row.id} className="flex items-start gap-2 p-2">
                <input type="checkbox" className="mt-1" aria-label={`选择经历 ${row.id}`} checked={selected.has(row.id)} onChange={() => toggle(row)} />
                <button type="button" className="flex min-w-0 flex-1 flex-col gap-0.5 text-left hover:bg-muted/50" onClick={() => void open(row.id)}>
                  <span className="flex flex-wrap items-center gap-1 text-xs">
                    <Badge variant={row.status === 'active' ? 'default' : 'secondary'}>{STATUS_LABELS[row.status]}</Badge>
                    <Badge variant="outline">{CANON_LABELS[row.canon_level] ?? row.canon_level}</Badge>
                    <span className="text-muted-foreground">{stageLabel(overview.stages, row.stage_key)}</span>
                    {row.story_time ? <span className="text-muted-foreground">· {row.story_time}</span> : null}
                    {row.emotion ? <span className="text-muted-foreground">· {row.emotion}</span> : null}
                    {row.status === 'active' && !row.has_vector ? <Badge variant="destructive">无向量</Badge> : null}
                  </span>
                  <span className="line-clamp-2 text-sm">{row.content}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between p-2 text-xs text-muted-foreground">
            <span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} / ${number.format(total)}` : '没有匹配的经历'}</span>
            <span className="flex gap-2">
              <Button type="button" size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</Button>
              <Button type="button" size="sm" variant="ghost" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</Button>
            </span>
          </div>
        </CardContent>
      </Card>
      <ExperienceEditor editor={editor} overview={overview} busy={busy} onChange={setEditor} onSave={(next) => void save(next)} />
    </div>
  )
}

function SourceRef({ item }: { item: CharacterExperience }) {
  const ref = item.source_ref ?? {}
  const text = (key: string) => (typeof ref[key] === 'string' || typeof ref[key] === 'number' ? String(ref[key]) : '')
  const rows: Array<[string, string]> = [
    ['创建方式', SOURCE_KIND_LABELS[item.source_kind] ?? item.source_kind],
    ['语料', text('corpus') || text('source')],
    ['章节', [text('chapter') ? `第 ${text('chapter')} 章` : '', text('chapter_title')].filter(Boolean).join(' ')],
    ['场景', text('scene_index') || text('scene_idx')],
    ['依据的设定段落', Array.isArray(ref.basis) ? (ref.basis as number[]).map((n) => `[${n}]`).join(' ') : ''],
    ['关键事件', text('event')],
    ['旧记忆', text('legacy_memory_id') ? `#${text('legacy_memory_id')}（${text('legacy_source')}）` : ''],
    ['书设笔记', text('book_note_id')],
    ['创建', `${when(item.created_at)} · ${item.created_by || '—'}`],
  ]
  const excerpt = text('scene_excerpt') || text('setting_excerpt')
  return (
    <div className="flex flex-col gap-1 rounded-md border bg-muted/30 p-2 text-xs" data-slot="experience-source">
      <span className="font-medium">出处</span>
      <dl className="grid grid-cols-[6rem_1fr] gap-x-2 gap-y-0.5">
        {rows.filter(([, value]) => value).map(([label, value]) => (
          <div key={label} className="contents"><dt className="text-muted-foreground">{label}</dt><dd className="break-all">{value}</dd></div>
        ))}
      </dl>
      {excerpt ? <blockquote className="max-h-40 overflow-auto whitespace-pre-wrap border-l-2 pl-2 text-muted-foreground">{excerpt}</blockquote> : null}
    </div>
  )
}

function ExperienceEditor({ editor, overview, busy, onChange, onSave }: {
  editor: EditorState | null
  overview: CharacterOverview
  busy: boolean
  onChange: (next: EditorState | null) => void
  onSave: (status?: ExperienceStatus) => void
}) {
  if (!editor) {
    return (
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3"><CardTitle className="text-sm">选择一条经历</CardTitle></CardHeader>
        <CardContent className="p-4"><p className="text-sm text-muted-foreground">从左侧选择一条查看出处、编辑或启用；也可以手写一条。</p></CardContent>
      </Card>
    )
  }
  const { item, original } = editor
  const set = <K extends keyof CharacterExperience>(key: K, value: CharacterExperience[K]) => onChange({ ...editor, item: { ...item, [key]: value } })
  const dirty = original ? Object.keys(diffFields(original, item)).length > 0 : Boolean(item.content?.trim())
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">{original ? `经历 #${original.id}（版本 ${original.version} · ${STATUS_LABELS[original.status]}）` : '手写一条经历'}</CardTitle>
        <CardDescription>第一人称「我」写。保存会带上读到的版本号；别人先改过会提示刷新。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-4">
        <Textarea aria-label="经历正文" rows={6} value={item.content ?? ''} onChange={(event) => set('content', event.target.value)} placeholder="那年夏天我……" />
        <div className="grid gap-2 md:grid-cols-2">
          <Input aria-label="角色自身时间" value={item.story_time ?? ''} onChange={(event) => set('story_time', event.target.value)} placeholder="她自己的时间，如 第1年12月初 / 七岁" />
          <Input aria-label="剧情阶段名" value={item.plot_stage ?? ''} onChange={(event) => set('plot_stage', event.target.value)} placeholder="剧情阶段名（可空）" />
          <select aria-label="所属阶段" className={selectClass} value={item.stage_key ?? ''} onChange={(event) => set('stage_key', event.target.value)}>
            <option value="">不分阶段（不受时间点限制）</option>
            {overview.stages.map((stage) => <option key={stage.stage_key} value={stage.stage_key}>{stage.ordinal}. {stage.title}</option>)}
          </select>
          <select aria-label="可信级别" className={selectClass} value={item.canon_level ?? 'canon'} onChange={(event) => set('canon_level', event.target.value as CanonLevel)}>
            {(Object.keys(CANON_LABELS) as CanonLevel[]).map((key) => <option key={key} value={key}>{CANON_LABELS[key]}</option>)}
          </select>
          <Input aria-label="情绪" list="character-emotions" value={item.emotion ?? ''} onChange={(event) => set('emotion', event.target.value)} placeholder="情绪" />
          <datalist id="character-emotions">{EMOTIONS.map((emotion) => <option key={emotion} value={emotion} />)}</datalist>
          <Input aria-label="在场人物" value={csv(item.characters)} onChange={(event) => set('characters', fromCsv(event.target.value))} placeholder="在场人物（顿号或逗号分隔）" />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" disabled={busy || !dirty || !item.content?.trim()} onClick={() => onSave()}><SaveIcon aria-hidden="true" />保存</Button>
          {original && original.status !== 'active' ? <Button type="button" size="sm" variant="outline" disabled={busy || !item.content?.trim()} onClick={() => onSave('active')}><CheckCircle2Icon aria-hidden="true" />{dirty ? '保存并启用' : '启用'}</Button> : null}
          {!original ? <Button type="button" size="sm" variant="outline" disabled={busy || !item.content?.trim()} onClick={() => onSave('active')}>新建并启用</Button> : null}
          {original && original.status !== 'archived' ? <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => onSave('archived')}><ArchiveIcon aria-hidden="true" />归档</Button> : null}
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => onChange(null)}>关闭</Button>
        </div>
        {original ? <SourceRef item={original} /> : null}
      </CardContent>
    </Card>
  )
}

function TryRecall({ botId, overview }: { botId: string; overview: CharacterOverview }) {
  const [message, setMessage] = useState('')
  const [result, setResult] = useState<RecallPreview | null>(null)
  const [busy, setBusy] = useState(false)
  async function run() {
    setBusy(true)
    try {
      setResult(await tryCharacterRecall(botId, message))
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card className="border-border/60">
      <CardHeader className="border-b pb-3">
        <CardTitle className="text-sm">试一试：她会想起什么</CardTitle>
        <CardDescription>按当前设置（时间点 {overview.settings.current_stage ? stageLabel(overview.stages, overview.settings.current_stage) : '不限'}、最多 {overview.settings.top_k} 条）预览召回，不写任何数据；没开启角色经历时也能试。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4">
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (message.trim()) void run() }}>
          <Input aria-label="群友的一句话" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="群友说的一句话，如：你小时候养过什么？" />
          <Button type="submit" size="sm" disabled={busy || !message.trim()}>预览</Button>
        </form>
        {result ? (
          result.items.length ? (
            <ul className="flex flex-col gap-2 text-sm" aria-label="召回结果">
              {result.items.map((item) => (
                <li key={item.id} className="rounded-md border p-2">
                  <p>{item.rendered_text}</p>
                  <p className="text-xs text-muted-foreground">
                    #{item.id} · 得分 {item.score.toFixed(2)}{item.similarity !== null ? ` · 相似度 ${item.similarity.toFixed(2)}` : ''}{item.keyword_hit ? ' · 关键词命中' : ''}
                  </p>
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted-foreground">她这句话什么也想不起来{result.warning === 'current_stage_missing' ? '（当前时间点不在时间线里）' : ''}。</p>
        ) : null}
        {result && !result.embedding_used ? <p className="text-xs text-muted-foreground">向量服务不可用，只按关键词预览。</p> : null}
      </CardContent>
    </Card>
  )
}

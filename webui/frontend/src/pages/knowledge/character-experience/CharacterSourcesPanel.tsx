import { useState } from 'react'
import { DownloadIcon, FileUpIcon, PlayIcon, RotateCcwIcon, SearchIcon, SquareIcon, WandSparklesIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  CANON_LABELS,
  JOB_KINDS,
  JOB_KIND_LABELS,
  STATUS_LABELS,
  cancelCharacterJob,
  exportCharacterJsonl,
  importCharacterJsonl,
  previewExpansion,
  previewExtraction,
  previewTimelineDraft,
  resumeCharacterJob,
  runLegacyMigration,
  startExpansion,
  startExtraction,
  startTimelineDraft,
  type CanonLevel,
  type CharacterJob,
  type CharacterOverview,
  type ExpansionPlan,
  type ExperienceStatus,
  type ExtractionParams,
  type ExtractionPlan,
  type ImportResult,
} from '@/api/characterExperience'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

import { JOB_STATUS_LABELS, conflictPlan, errorText, jobActive, number, selectClass, stageLabel, when } from './shared'

type PanelProps = { botId: string; overview: CharacterOverview; onChanged: () => Promise<void> }
type ModelStatus = { available: boolean; primary?: string; source_label?: string }

function ModelLine({ model }: { model: ModelStatus }) {
  return model.available
    ? <span className="text-xs text-muted-foreground">模型：{model.primary || '—'}（{model.source_label || '模型分工'}）</span>
    : <span className="text-xs text-destructive">没有可用模型：在配置中心「模型分工」里给「角色经历」指定模型</span>
}

// ─── 任务列表：进度、取消、续跑 ───

function jobSummary(job: CharacterJob): string {
  const data: Record<string, unknown> = { ...(job.progress ?? {}), ...(job.result ?? {}) }
  const num = (key: string) => (typeof data[key] === 'number' ? (data[key] as number) : undefined)
  if (job.kind === JOB_KINDS.timeline) {
    const created = Array.isArray(data.stages_created) ? data.stages_created.length : num('stages')
    return created === undefined ? '' : `起草出 ${created} 个阶段`
  }
  if (job.kind === JOB_KINDS.embed) {
    const embedded = num('embedded')
    return embedded === undefined ? '' : `补了 ${number.format(embedded)} 条向量`
  }
  const parts: string[] = []
  const total = num('total')
  if (total !== undefined) parts.push(`已处理 ${number.format(num('done') ?? num('processed') ?? 0)}/${number.format(total)}`)
  const memories = num('memories')
  if (memories !== undefined) parts.push(`新增 ${number.format(memories)} 条草稿`)
  if (num('empty')) parts.push(`没有相关经历 ${num('empty')}`)
  if (num('failed')) parts.push(`失败 ${num('failed')}`)
  return parts.join(' · ')
}

export function JobsList({ jobs, kinds, onChanged }: { jobs: CharacterJob[]; kinds: string[]; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState('')
  const shown = jobs.filter((job) => kinds.includes(job.kind))

  async function act(job: CharacterJob, action: 'cancel' | 'resume') {
    setBusy(job.run_id)
    try {
      if (action === 'cancel') {
        await cancelCharacterJob(job.run_id)
        toast.success('已请求取消：处理完手上这一批就停，已写入的草稿保留')
      } else {
        await resumeCharacterJob(job.run_id)
        toast.success('已续跑：处理过的会跳过，只处理剩下的')
      }
      await onChanged()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy('')
    }
  }

  if (!shown.length) return <p className="text-xs text-muted-foreground">还没有任务。</p>
  return (
    <ul className="flex flex-col gap-1 text-xs" aria-label="任务列表">
      {shown.map((job) => {
        const summary = jobSummary(job)
        return (
          <li key={job.run_id} className="flex flex-wrap items-center gap-2 rounded-md border p-2">
            <Badge variant={job.status === 'failed' ? 'destructive' : jobActive(job) ? 'default' : 'secondary'}>{JOB_STATUS_LABELS[job.status] ?? job.status}</Badge>
            <span className="font-medium">{JOB_KIND_LABELS[job.kind] ?? job.kind}</span>
            <span className="text-muted-foreground">{when(job.created_at)}{job.generation ? ` · 第 ${job.generation + 1} 次运行` : ''}</span>
            {summary ? <span>{summary}</span> : null}
            <span className="ml-auto flex gap-1">
              {job.status === 'pending' || job.status === 'running' ? (
                <Button type="button" size="sm" variant="outline" disabled={busy === job.run_id} onClick={() => void act(job, 'cancel')}><SquareIcon aria-hidden="true" />取消</Button>
              ) : null}
              {job.resumable ? (
                <Button type="button" size="sm" variant="outline" disabled={busy === job.run_id} onClick={() => void act(job, 'resume')}><RotateCcwIcon aria-hidden="true" />续跑</Button>
              ) : null}
            </span>
            {job.error_message ? <span className="basis-full text-destructive">{job.error_message}</span> : null}
          </li>
        )
      })}
    </ul>
  )
}

// ─── 来源一：从原著提取 ───

interface ExtractForm {
  path: string
  name: string
  aliases: string
  description: string
  chapter_from: string
  chapter_to: string
  require_full_name: boolean
  redo: boolean
  concurrency: number
}

function toParams(form: ExtractForm): ExtractionParams {
  const chapter = (value: string) => (value.trim() === '' ? null : Number(value))
  return { ...form, chapter_from: chapter(form.chapter_from), chapter_to: chapter(form.chapter_to) }
}

export function NovelExtractPanel({ botId, overview, onChanged }: PanelProps) {
  const [form, setForm] = useState<ExtractForm>(() => ({
    path: overview.sources[0]?.path ?? '',
    name: overview.bot.name,
    aliases: overview.bot.aliases.join('、'),
    description: '',
    chapter_from: '',
    chapter_to: '',
    require_full_name: true,
    redo: false,
    concurrency: 2,
  }))
  const [plan, setPlan] = useState<ExtractionPlan | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const novel = overview.scene_stats.novel ?? {}

  // 改了任何参数，之前的预览就作废：确认的永远是眼前这份计划
  function set<K extends keyof ExtractForm>(key: K, value: ExtractForm[K]) {
    setForm((current) => ({ ...current, [key]: value }))
    setPlan(null)
    setConfirming(false)
  }

  async function preview() {
    setBusy(true)
    try {
      setPlan(await previewExtraction(botId, toParams(form)))
      setConfirming(false)
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!plan) return
    setBusy(true)
    try {
      const result = await startExtraction(botId, toParams(form), plan.calls)
      toast.success(`已排队：从原著提取 ${number.format(result.calls)} 个场景，结果进草稿`)
      setPlan(null)
      setConfirming(false)
      await onChanged()
    } catch (reason) {
      const latest = conflictPlan<ExtractionPlan>(reason)
      if (latest) {
        setPlan(latest)
        setConfirming(false)
        toast.error(`计划有变化：现在预计调用模型 ${latest.calls} 次，请重新确认`)
      } else {
        toast.error(errorText(reason))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">从原著提取</CardTitle>
          <CardDescription>
            按「第N章」切章，找出她出场的场景，每个场景调用一次模型，写成她自己视角的 1–3 条回忆，全部进草稿、标为「原著」。
            任务可以取消、续跑：处理过的场景记在库里，不会重复调用。章节落在某个阶段的章节范围里就自动归入那个阶段。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4">
          {overview.sources.length ? (
            <select aria-label="小说原文" className={selectClass} value={form.path} onChange={(event) => set('path', event.target.value)}>
              {overview.sources.map((source) => <option key={source.path} value={source.path}>{source.name}（{(source.size / 1024 / 1024).toFixed(1)} MB · 修改于 {when(source.modified_at)}）</option>)}
            </select>
          ) : <p className="text-sm text-muted-foreground">没有找到小说原文：把 .txt 放进插件数据目录或 novel_docs（与书设工作台同一处）。</p>}
          <div className="grid gap-2 md:grid-cols-2">
            <Input aria-label="角色名" value={form.name} onChange={(event) => set('name', event.target.value)} placeholder="原著里的全名" />
            <Input aria-label="别名" value={form.aliases} onChange={(event) => set('aliases', event.target.value)} placeholder="别名、昵称（顿号或逗号分隔）" />
            <Input aria-label="起始章" inputMode="numeric" value={form.chapter_from} onChange={(event) => set('chapter_from', event.target.value)} placeholder="起始章（空 = 从头）" />
            <Input aria-label="结束章" inputMode="numeric" value={form.chapter_to} onChange={(event) => set('chapter_to', event.target.value)} placeholder="结束章（空 = 到最后）" />
          </div>
          <Textarea aria-label="角色简介" rows={3} value={form.description} onChange={(event) => set('description', event.target.value)} placeholder="一两句角色简介，帮模型认准她（可空）" />
          <div className="flex flex-wrap items-center gap-4 text-xs">
            <label className="flex items-center gap-1"><input type="checkbox" checked={form.require_full_name} onChange={(event) => set('require_full_name', event.target.checked)} />场景里必须出现全名</label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={form.redo} onChange={(event) => set('redo', event.target.checked)} />重新提取已处理的场景</label>
            <label className="flex items-center gap-1">并发
              <Input aria-label="并发数" type="number" min={1} max={4} className="h-7 w-14 text-xs" value={form.concurrency} onChange={(event) => set('concurrency', Math.max(1, Math.min(4, Number(event.target.value) || 1)))} />
            </label>
          </div>
          <div>
            <Button type="button" size="sm" variant="outline" disabled={busy || !form.path || !form.name.trim()} onClick={() => void preview()}><SearchIcon aria-hidden="true" />预览</Button>
          </div>
          {plan ? (
            <div className="flex flex-col gap-2 rounded-md border bg-muted/30 p-3 text-xs" data-slot="extraction-plan">
              <p>
                《{plan.source.name}》共 {number.format(plan.chapters_total)} 章；范围内 {number.format(plan.chapters_in_range)} 章，
                其中 {number.format(plan.chapters_with_character)} 章有她的场景，切出 {number.format(plan.scenes)} 个场景；之前处理过 {number.format(plan.already_processed)} 个。
              </p>
              <p className="text-sm font-medium">这次预计调用模型 {number.format(plan.calls)} 次（每个场景一次）</p>
              {Object.keys(plan.by_stage).length ? (
                <p>归入阶段：{Object.entries(plan.by_stage).map(([key, count]) => `${stageLabel(overview.stages, key)} ${number.format(count)}`).join('；')}</p>
              ) : null}
              {plan.unassigned_scenes ? <p className="text-muted-foreground">{number.format(plan.unassigned_scenes)} 个场景不在任何阶段的章节范围里，提取出来不分阶段；可以先在「时间线」里给阶段填章节范围。</p> : null}
              {plan.sample.length ? (
                <ul className="flex flex-col gap-1" aria-label="场景样例">
                  {plan.sample.map((scene) => (
                    <li key={`${scene.chapter}-${scene.preview}`} className="truncate"><span className="font-mono">第{scene.chapter}章</span> {scene.title} · {number.format(scene.chars)} 字 · {scene.preview}</li>
                  ))}
                </ul>
              ) : null}
              <ModelLine model={plan.model} />
              {plan.calls > 0 ? (
                confirming ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span>确认调用模型 {number.format(plan.calls)} 次？结果全部进草稿，审阅后再启用。</span>
                    <Button type="button" size="sm" disabled={busy || !plan.model.available} onClick={() => void start()}><PlayIcon aria-hidden="true" />确认开始</Button>
                    <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(false)}>再想想</Button>
                  </div>
                ) : (
                  <div><Button type="button" size="sm" disabled={busy || !plan.model.available} onClick={() => setConfirming(true)}>开始提取（{number.format(plan.calls)} 次调用）</Button></div>
                )
              ) : <p>没有需要处理的场景：都处理过了。要重新提取请勾选「重新提取已处理的场景」。</p>}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">提取任务</CardTitle>
          <CardDescription>
            已处理场景：有经历 {number.format(novel.done ?? 0)} · 没有相关经历 {number.format(novel.empty ?? 0)} · 失败 {number.format(novel.failed ?? 0)}（失败的续跑或下次提取时会重试）
          </CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <JobsList jobs={overview.jobs} kinds={[JOB_KINDS.extract]} onChanged={onChanged} />
        </CardContent>
      </Card>
    </div>
  )
}

// ─── 来源二：按设定编写（原创角色） ───

type TimelinePlan = { calls: number; paragraphs: number; setting_chars: number }

export function SettingComposePanel({ botId, overview, onChanged }: PanelProps) {
  const [setting, setSetting] = useState(overview.bot.persona_text)
  const [timelinePlan, setTimelinePlan] = useState<TimelinePlan | null>(null)
  const [replaceDrafts, setReplaceDrafts] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [redo, setRedo] = useState(false)
  const [expansionPlan, setExpansionPlan] = useState<ExpansionPlan | null>(null)
  const [confirming, setConfirming] = useState<'timeline' | 'expand' | null>(null)
  const [busy, setBusy] = useState(false)
  const expandable = overview.stages.filter((stage) => stage.status === 'confirmed' && stage.key_events.length)
  const draftStages = overview.stages.filter((stage) => stage.status === 'draft')

  function editSetting(value: string) {
    setSetting(value)
    setTimelinePlan(null)
    setConfirming(null)
  }

  function pick(keys: string[]) {
    setPicked(keys)
    setExpansionPlan(null)
    setConfirming(null)
  }

  async function run<T>(task: () => Promise<T>, then: (value: T) => void | Promise<void>) {
    setBusy(true)
    try {
      await then(await task())
    } catch (reason) {
      const latest = conflictPlan<ExpansionPlan>(reason)
      if (latest && 'events' in latest) {
        setExpansionPlan(latest)
        setConfirming(null)
        toast.error(`计划有变化：现在预计调用模型 ${latest.calls} 次，请重新确认`)
      } else {
        toast.error(errorText(reason))
      }
    } finally {
      setBusy(false)
    }
  }

  const draftTimeline = () => run(() => startTimelineDraft(botId, setting, replaceDrafts), async () => {
    toast.success('已排队起草时间线：完成后到「时间线」里修改、确认')
    setTimelinePlan(null)
    setConfirming(null)
    await onChanged()
  })

  const expand = () => run(() => startExpansion(botId, { stage_keys: picked, setting, redo }, expansionPlan?.calls ?? 0), async (result) => {
    toast.success(`已排队：展开 ${number.format(result.calls)} 个关键事件，结果进草稿、标为「编造」`)
    setExpansionPlan(null)
    setConfirming(null)
    await onChanged()
  })

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <div className="flex flex-col gap-4">
        <Card className="border-border/60">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-sm">第一步：按设定起草人生时间线</CardTitle>
            <CardDescription>
              给原创角色用：按设定分段编号，调用一次模型起草 3–8 个人生阶段和关键事件，每条事件记下依据的设定段落。
              起草出来的阶段是草稿，到「时间线」里修改、确认后才能展开；设定里没写的不会被当成事实。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 p-4">
            <Textarea aria-label="设定文本" rows={10} value={setting} onChange={(event) => editSetting(event.target.value)} placeholder="角色设定：出身、成长经历、重要的人和事……空行分段" />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" variant="ghost" disabled={busy || !overview.bot.persona_text} onClick={() => editSetting(overview.bot.persona_text)}>带入 Bot 资料里的人设</Button>
              <Button type="button" size="sm" variant="outline" disabled={busy || !setting.trim()}
                onClick={() => void run(() => previewTimelineDraft(botId, setting), (plan) => { setTimelinePlan(plan); setConfirming(null) })}>
                <SearchIcon aria-hidden="true" />预览起草
              </Button>
            </div>
            {timelinePlan ? (
              <div className="flex flex-col gap-2 rounded-md border bg-muted/30 p-3 text-xs" data-slot="timeline-plan">
                <p>设定 {number.format(timelinePlan.paragraphs)} 段、{number.format(timelinePlan.setting_chars)} 字；起草时间线调用模型 {timelinePlan.calls} 次。</p>
                {draftStages.length ? (
                  <label className="flex items-center gap-1"><input type="checkbox" checked={replaceDrafts} onChange={(event) => setReplaceDrafts(event.target.checked)} />替换之前起草、还没用过的草稿阶段（{draftStages.length} 个）</label>
                ) : null}
                <ModelLine model={overview.model} />
                {confirming === 'timeline' ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span>确认调用模型 {timelinePlan.calls} 次起草时间线？</span>
                    <Button type="button" size="sm" disabled={busy || !overview.model.available} onClick={() => void draftTimeline()}><PlayIcon aria-hidden="true" />确认起草</Button>
                    <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>再想想</Button>
                  </div>
                ) : (
                  <div><Button type="button" size="sm" disabled={busy || !overview.model.available} onClick={() => setConfirming('timeline')}><WandSparklesIcon aria-hidden="true" />起草时间线</Button></div>
                )}
              </div>
            ) : null}
          </CardContent>
        </Card>
        <Card className="border-border/60">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-sm">第二步：把关键事件展开成经历</CardTitle>
            <CardDescription>
              选已确认的阶段，每个关键事件调用一次模型写成 1–3 条第一人称经历，标为「编造」、记下依据的设定段落，全部进草稿。
              用的设定是上面的文本（空着就用 Bot 资料里的人设）。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 p-4">
            {draftStages.length ? <p className="text-xs text-muted-foreground">还有 {draftStages.length} 个草稿阶段没确认，确认后才能展开。</p> : null}
            {expandable.length ? (
              <>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input type="checkbox" aria-label="全选可展开的阶段" checked={picked.length === expandable.length}
                    onChange={() => pick(picked.length === expandable.length ? [] : expandable.map((stage) => stage.stage_key))} />
                  全选
                </label>
                <ul className="flex flex-col gap-1 text-sm" aria-label="可展开的阶段">
                  {expandable.map((stage) => (
                    <li key={stage.stage_key}>
                      <label className="flex items-center gap-2">
                        <input type="checkbox" aria-label={`展开 ${stage.title}`} checked={picked.includes(stage.stage_key)}
                          onChange={() => pick(picked.includes(stage.stage_key) ? picked.filter((key) => key !== stage.stage_key) : [...picked, stage.stage_key])} />
                        <span>{stage.ordinal}. {stage.title}</span>
                        <span className="text-xs text-muted-foreground">{stage.key_events.length} 个关键事件{stage.counts?.draft || stage.counts?.active ? ` · 已有经历 ${(stage.counts?.draft ?? 0) + (stage.counts?.active ?? 0)} 条` : ''}</span>
                      </label>
                    </li>
                  ))}
                </ul>
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <label className="flex items-center gap-1"><input type="checkbox" checked={redo} onChange={(event) => { setRedo(event.target.checked); setExpansionPlan(null); setConfirming(null) }} />重新展开已处理的事件</label>
                  <Button type="button" size="sm" variant="outline" disabled={busy || !picked.length}
                    onClick={() => void run(() => previewExpansion(botId, picked, redo), (plan) => { setExpansionPlan(plan); setConfirming(null) })}>
                    <SearchIcon aria-hidden="true" />预览展开
                  </Button>
                </div>
              </>
            ) : <p className="text-sm text-muted-foreground">没有可展开的阶段：先起草或手动建阶段、写上关键事件，并在「时间线」里确认。</p>}
            {expansionPlan ? (
              <div className="flex flex-col gap-2 rounded-md border bg-muted/30 p-3 text-xs" data-slot="expansion-plan">
                <p>选中 {expansionPlan.stages.length} 个阶段、{number.format(expansionPlan.events)} 个关键事件；之前展开过 {number.format(expansionPlan.already_processed)} 个。</p>
                <p className="text-sm font-medium">这次预计调用模型 {number.format(expansionPlan.calls)} 次（每个事件一次）</p>
                {expansionPlan.unconfirmed_stages.length ? <p className="text-muted-foreground">未确认、跳过：{expansionPlan.unconfirmed_stages.map((key) => stageLabel(overview.stages, key)).join('、')}</p> : null}
                <ModelLine model={expansionPlan.model} />
                {expansionPlan.calls > 0 ? (
                  confirming === 'expand' ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span>确认调用模型 {number.format(expansionPlan.calls)} 次？</span>
                      <Button type="button" size="sm" disabled={busy || !expansionPlan.model.available} onClick={() => void expand()}><PlayIcon aria-hidden="true" />确认展开</Button>
                      <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(null)}>再想想</Button>
                    </div>
                  ) : (
                    <div><Button type="button" size="sm" disabled={busy || !expansionPlan.model.available} onClick={() => setConfirming('expand')}>开始展开（{number.format(expansionPlan.calls)} 次调用）</Button></div>
                  )
                ) : <p>没有需要展开的事件：都展开过了。要重写请勾选「重新展开已处理的事件」。</p>}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3"><CardTitle className="text-sm">编写任务</CardTitle></CardHeader>
        <CardContent className="p-4">
          <JobsList jobs={overview.jobs} kinds={[JOB_KINDS.timeline, JOB_KINDS.expand]} onChanged={onChanged} />
        </CardContent>
      </Card>
    </div>
  )
}

// ─── 来源三：手写之外的导入导出，与旧数据迁移 ───

const MIGRATION_STATUS: Record<string, string> = {
  applied: '已迁移',
  already_applied: '之前已迁移',
  nothing_to_migrate: '没有旧数据',
  target_bot_missing: '目标 Bot 不存在',
  profile_update_pending: '待补写 Bot 配置',
  preview: '预览',
}

function reportLine(report: Record<string, unknown>): string {
  const count = (key: string) => (typeof report[key] === 'number' ? number.format(report[key] as number) : null)
  const parts = [`${String(report.source ?? '')} → ${String(report.bot_id ?? '')}`, MIGRATION_STATUS[String(report.status)] ?? String(report.status ?? '')]
  if (count('rows') !== null) parts.push(`旧记忆 ${count('rows')} 条`)
  if (count('would_import') !== null) parts.push(`将导入 ${count('would_import')} 条`)
  if (count('imported') !== null) parts.push(`导入 ${count('imported')} 条`)
  if (report.duplicates) parts.push(`重复 ${count('duplicates')}`)
  if (count('stages') !== null) parts.push(`${count('stages')} 个阶段`)
  if (report.lore_notes) parts.push(`书设个人经历笔记 ${count('lore_notes')} 条`)
  if (report.last_stage) parts.push(`当前时间点 → ${String(report.last_stage)}`)
  if (report.profile_updated === false) parts.push('Bot 配置还没写入（下次启动补写）')
  return parts.join(' · ')
}

function download(text: string, fileName: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/x-ndjson;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

const IMPORT_STATUS = { draft: '进草稿', active: '直接启用', as_file: '按文件里的 status' } as const
const ON_DUPLICATE = { skip: '跳过', fill: '补上空字段', overwrite: '覆盖' } as const

export function ImportExportPanel({ botId, overview, onChanged }: PanelProps) {
  const [text, setText] = useState('')
  const [fileName, setFileName] = useState('')
  const [status, setStatus] = useState<keyof typeof IMPORT_STATUS>('draft')
  const [canon, setCanon] = useState<CanonLevel>('canon')
  const [onDuplicate, setOnDuplicate] = useState<keyof typeof ON_DUPLICATE>('skip')
  const [result, setResult] = useState<ImportResult | null>(null)
  const [exportStatuses, setExportStatuses] = useState<ExperienceStatus[]>(['draft', 'active', 'archived'])
  const [reports, setReports] = useState<Array<Record<string, unknown>> | null>(null)
  const [confirmMigration, setConfirmMigration] = useState(false)
  const [busy, setBusy] = useState(false)
  const lines = text.split('\n').filter((line) => line.trim()).length

  async function guarded(task: () => Promise<void>) {
    setBusy(true)
    try {
      await task()
    } catch (reason) {
      toast.error(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const readFile = (file: File) => guarded(async () => {
    setText(await file.text())
    setFileName(file.name)
    setResult(null)
  })

  const runImport = () => guarded(async () => {
    const outcome = await importCharacterJsonl(botId, { text, status, canon_level: canon, on_duplicate: onDuplicate })
    setResult(outcome)
    toast.success(`导入完成：新增 ${outcome.inserted} 条${outcome.updated ? `，更新 ${outcome.updated} 条` : ''}${outcome.error_count ? `，${outcome.error_count} 行有错` : ''}`)
    await onChanged()
  })

  const runExport = () => guarded(async () => {
    const body = await exportCharacterJsonl(botId, exportStatuses)
    if (!body.trim()) {
      toast.error('没有可导出的经历')
      return
    }
    download(body, `character-experience-${botId}.jsonl`)
  })

  const migrate = (dryRun: boolean) => guarded(async () => {
    const outcome = await runLegacyMigration(dryRun)
    setReports(outcome.reports)
    setConfirmMigration(false)
    if (!dryRun) {
      toast.success('旧数据迁移已执行')
      await onChanged()
    }
  })

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <Card className="border-border/60">
        <CardHeader className="border-b pb-3">
          <CardTitle className="text-sm">导入 JSONL</CardTitle>
          <CardDescription>
            每行一个 JSON 对象，兼容 v5 的 bzz_memories_raw.jsonl（memory / emotion / characters / arc / stage / approx_time / chapter / title / scene_idx）
            和这里导出的文件。arc 当阶段，时间线里没有的阶段自动建；正文相同的算重复。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4">
          <label className="inline-flex w-fit cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-xs">
            <FileUpIcon aria-hidden="true" className="size-4" />{fileName ? `已读入 ${fileName}` : '选择 .jsonl 文件'}
            <input type="file" aria-label="选择 JSONL 文件" accept=".jsonl,.ndjson,.txt,application/x-ndjson,text/plain" className="hidden"
              onChange={(event) => { const file = event.target.files?.[0]; if (file) void readFile(file); event.target.value = '' }} />
          </label>
          <Textarea aria-label="JSONL 内容" rows={8} className="font-mono text-xs" value={text}
            onChange={(event) => { setText(event.target.value); setFileName(''); setResult(null) }}
            placeholder={'{"memory": "那年冬天我第一次下山……", "arc": "arc01_入门", "approx_time": "第1年冬"}'} />
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <label className="flex items-center gap-1">导入后
              <select aria-label="导入后状态" className={selectClass} value={status} onChange={(event) => setStatus(event.target.value as keyof typeof IMPORT_STATUS)}>
                {Object.entries(IMPORT_STATUS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-1">没写级别的行按
              <select aria-label="默认可信级别" className={selectClass} value={canon} onChange={(event) => setCanon(event.target.value as CanonLevel)}>
                {(Object.keys(CANON_LABELS) as CanonLevel[]).map((key) => <option key={key} value={key}>{CANON_LABELS[key]}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-1">同内容已存在时
              <select aria-label="重复时" className={selectClass} value={onDuplicate} onChange={(event) => setOnDuplicate(event.target.value as keyof typeof ON_DUPLICATE)}>
                {Object.entries(ON_DUPLICATE).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
            <Button type="button" size="sm" disabled={busy || !lines} onClick={() => void runImport()}>导入 {number.format(lines)} 行</Button>
          </div>
          {onDuplicate === 'fill' ? <p className="text-xs text-muted-foreground">「补上空字段」只补情绪、人物、时间、阶段这些原来为空的字段，不改正文与状态；适合给迁移过来的旧经历补上 bzz_memories_raw.jsonl 里的情绪与人物。</p> : null}
          {result ? (
            <div className="flex flex-col gap-1 rounded-md border bg-muted/30 p-3 text-xs" data-slot="import-result">
              <p>共 {number.format(result.lines)} 行，有效 {number.format(result.valid)}；新增 {number.format(result.inserted)}，更新 {number.format(result.updated)}，重复跳过 {number.format(result.duplicates)}；出错 {number.format(result.error_count)} 行。</p>
              {result.stages_created.length ? <p>新建阶段：{result.stages_created.join('、')}</p> : null}
              {result.errors.length ? (
                <ul className="list-disc pl-4 text-destructive" aria-label="导入错误">
                  {result.errors.slice(0, 10).map((error) => <li key={error.line}>第 {error.line} 行：{error.message}</li>)}
                </ul>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <div className="flex flex-col gap-4">
        <Card className="border-border/60">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-sm">导出 JSONL</CardTitle>
            <CardDescription>v5 字段加上阶段、可信级别、状态与出处，可以原样再导入（换一个 Bot 或另一个库）。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3 p-4 text-xs">
            {(Object.keys(STATUS_LABELS) as ExperienceStatus[]).map((key) => (
              <label key={key} className="flex items-center gap-1">
                <input type="checkbox" checked={exportStatuses.includes(key)}
                  onChange={() => setExportStatuses((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]))} />
                {STATUS_LABELS[key]}
              </label>
            ))}
            <Button type="button" size="sm" variant="outline" disabled={busy || !exportStatuses.length} onClick={() => void runExport()}><DownloadIcon aria-hidden="true" />导出</Button>
            <span className="text-muted-foreground">共 {number.format(overview.counts.total)} 条</span>
          </CardContent>
        </Card>
        <Card className="border-border/60">
          <CardHeader className="border-b pb-3">
            <CardTitle className="text-sm">旧数据迁移</CardTitle>
            <CardDescription>
              v5 把白真真的第一人称经历存在记忆表里（source=bzz_experience、没有归属 Bot），所有 Bot 都可能召回。启动时会自动迁到 Bot baizz 的角色经历一次：
              旧行不删，但普通记忆召回不再返回它们；书设里的「白真真经历」笔记保留，仍不当世界观书设注入。当时还没建这个 Bot 的，建好后可以在这里补做（对整个库执行，已迁过的会跳过）。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 p-4 text-xs">
            {overview.migrations.length ? (
              <ul className="flex flex-col gap-1" aria-label="迁移记录">
                {overview.migrations.map((item) => <li key={item.key}>{when(item.applied_at)} · {reportLine({ ...item.report, status: item.status })}</li>)}
              </ul>
            ) : <p className="text-muted-foreground">这个 Bot 没有迁移记录。</p>}
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void migrate(true)}>预览迁移</Button>
              {confirmMigration ? (
                <>
                  <span>确认对整个库执行迁移？</span>
                  <Button type="button" size="sm" disabled={busy} onClick={() => void migrate(false)}>确认执行</Button>
                  <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmMigration(false)}>取消</Button>
                </>
              ) : <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmMigration(true)}>执行迁移</Button>}
            </div>
            {reports ? (
              reports.length ? (
                <ul className="flex flex-col gap-1 rounded-md border bg-muted/30 p-2" aria-label="迁移结果">
                  {reports.map((report) => <li key={String(report.key)}>{reportLine(report)}</li>)}
                </ul>
              ) : <p className="text-muted-foreground">没有配置旧数据来源。</p>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

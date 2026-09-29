import { useState } from 'react'
import { ArrowDownIcon, ArrowUpIcon, CheckIcon, ClockIcon, PencilIcon, PlusIcon, SaveIcon, Trash2Icon } from 'lucide-react'
import { toast } from 'sonner'

import {
  confirmTimelineStages,
  deleteTimelineStage,
  reorderTimelineStages,
  saveCharacterSettings,
  saveTimelineStage,
  type CharacterOverview,
  type KeyEvent,
  type TimelineStage,
} from '@/api/characterExperience'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

import { errorText, number } from './shared'

interface StageDraft {
  stage_key: string
  title: string
  story_time: string
  description: string
  events: string
  chapter_from: string
  chapter_to: string
}

function toDraft(stage?: TimelineStage): StageDraft {
  return {
    stage_key: stage?.stage_key ?? '',
    title: stage?.title ?? '',
    story_time: stage?.story_time ?? '',
    description: stage?.description ?? '',
    events: (stage?.key_events ?? []).map((event) => event.text).join('\n'),
    chapter_from: stage?.chapter_from != null ? String(stage.chapter_from) : '',
    chapter_to: stage?.chapter_to != null ? String(stage.chapter_to) : '',
  }
}

/** 关键事件逐行编辑：原样保留的事件带回它依据的设定段落编号 */
function toEvents(text: string, previous: KeyEvent[]): KeyEvent[] {
  return text.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => ({
    text: line,
    basis: previous.find((event) => event.text === line)?.basis ?? [],
  }))
}

function toFields(draft: StageDraft, previous: KeyEvent[]) {
  const chapter = (value: string) => (value.trim() === '' ? null : Number(value))
  return {
    title: draft.title.trim(),
    story_time: draft.story_time.trim(),
    description: draft.description.trim(),
    key_events: toEvents(draft.events, previous),
    chapter_from: chapter(draft.chapter_from),
    chapter_to: chapter(draft.chapter_to),
  }
}

function StageForm({ draft, onChange, isNew }: { draft: StageDraft; onChange: (next: StageDraft) => void; isNew: boolean }) {
  const set = <K extends keyof StageDraft>(key: K, value: string) => onChange({ ...draft, [key]: value })
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {isNew ? <Input aria-label="阶段 key" value={draft.stage_key} onChange={(event) => set('stage_key', event.target.value.trim())} placeholder="阶段 key，如 s1 / arc01_入门（创建后不能改）" /> : null}
      <Input aria-label="阶段名" value={draft.title} onChange={(event) => set('title', event.target.value)} placeholder="阶段名，如 童年" />
      <Input aria-label="阶段时间" value={draft.story_time} onChange={(event) => set('story_time', event.target.value)} placeholder="她自己的时间，如 七岁到十二岁" />
      <div className="flex gap-2">
        <Input aria-label="起始章" inputMode="numeric" value={draft.chapter_from} onChange={(event) => set('chapter_from', event.target.value)} placeholder="起始章（原著提取用）" />
        <Input aria-label="结束章" inputMode="numeric" value={draft.chapter_to} onChange={(event) => set('chapter_to', event.target.value)} placeholder="结束章" />
      </div>
      <Textarea aria-label="阶段说明" className="md:col-span-2" rows={2} value={draft.description} onChange={(event) => set('description', event.target.value)} placeholder="这一阶段的概况" />
      <Textarea aria-label="关键事件（每行一件）" className="md:col-span-2" rows={4} value={draft.events} onChange={(event) => set('events', event.target.value)} placeholder={'每行一件关键事件，「按设定编写」会逐条展开成经历'} />
    </div>
  )
}

export function CharacterTimelinePanel({ botId, overview, onChanged }: { botId: string; overview: CharacterOverview; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<StageDraft>(toDraft())
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const stages = overview.stages
  const drafts = stages.filter((stage) => stage.status === 'draft')

  async function act(run: () => Promise<unknown>, success: string) {
    setBusy(true)
    try {
      await run()
      toast.success(success)
      await onChanged()
      return true
    } catch (reason) {
      toast.error(errorText(reason))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function saveEdit(stage: TimelineStage) {
    if (await act(() => saveTimelineStage(botId, stage.stage_key, stage.version, toFields(draft, stage.key_events)), `已保存「${draft.title || stage.title}」`)) setEditing(null)
  }

  async function create() {
    if (await act(() => saveTimelineStage(botId, draft.stage_key, 0, toFields(draft, [])), `已新增阶段「${draft.title || draft.stage_key}」`)) {
      setCreating(false)
      setDraft(toDraft())
    }
  }

  function move(index: number, delta: number) {
    const order = [...stages]
    const [item] = order.splice(index, 1)
    order.splice(index + delta, 0, item)
    void act(() => reorderTimelineStages(botId, order.map((stage) => ({ stage_key: stage.stage_key, version: stage.version }))), '顺序已更新')
  }

  return (
    <Card className="border-border/60">
      <CardHeader className="gap-2 border-b pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm">时间线（{stages.length} 个阶段）</CardTitle>
          <div className="flex flex-wrap gap-2">
            {drafts.length ? (
              <Button type="button" size="sm" disabled={busy}
                onClick={() => void act(() => confirmTimelineStages(botId, drafts.map((stage) => ({ stage_key: stage.stage_key, version: stage.version }))), `已确认 ${drafts.length} 个阶段`)}>
                <CheckIcon aria-hidden="true" />确认全部草稿阶段（{drafts.length}）
              </Button>
            ) : null}
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => { setCreating(true); setEditing(null); setDraft(toDraft()) }}><PlusIcon aria-hidden="true" />新增阶段</Button>
          </div>
        </div>
        <CardDescription>
          按她自己的时间先后排列。「当前时间点」之后的阶段不会被想起（避免剧透、自相矛盾）；没分阶段的经历不受限制。
          填了章节范围的阶段，从原著提取时按章节自动归入。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-4">
        {creating ? (
          <div className="flex flex-col gap-2 rounded-md border p-3">
            <StageForm draft={draft} onChange={setDraft} isNew />
            <div className="flex gap-2">
              <Button type="button" size="sm" disabled={busy || !draft.stage_key || !draft.title.trim()} onClick={() => void create()}><SaveIcon aria-hidden="true" />新增</Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setCreating(false)}>取消</Button>
            </div>
          </div>
        ) : null}
        {stages.length ? (
          <ol className="flex flex-col gap-2" aria-label="时间线阶段">
            {stages.map((stage, index) => {
              const counts = stage.counts ?? { draft: 0, active: 0, archived: 0 }
              const used = counts.draft + counts.active + counts.archived
              return (
                <li key={stage.stage_key} className={`rounded-md border p-3 ${stage.is_current ? 'border-primary' : ''}`}>
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-mono text-xs text-muted-foreground">{stage.ordinal}.</span>
                    <span className="font-medium">{stage.title}</span>
                    <span className="font-mono text-xs text-muted-foreground">{stage.stage_key}</span>
                    {stage.story_time ? <span className="text-xs text-muted-foreground">{stage.story_time}</span> : null}
                    {stage.status === 'draft' ? <Badge variant="secondary">草稿</Badge> : null}
                    {stage.is_current ? <Badge><ClockIcon aria-hidden="true" />当前时间点</Badge> : null}
                    {!stage.recallable ? <Badge variant="outline">在当前时间点之后，想不起</Badge> : null}
                    {stage.chapter_from != null || stage.chapter_to != null ? <span className="text-xs text-muted-foreground">第 {stage.chapter_from ?? '…'}–{stage.chapter_to ?? '…'} 章</span> : null}
                    <span className="text-xs text-muted-foreground">启用 {number.format(counts.active)} · 草稿 {number.format(counts.draft)}</span>
                    <span className="ml-auto flex flex-wrap gap-1">
                      <Button type="button" size="icon" variant="ghost" aria-label={`上移 ${stage.title}`} disabled={busy || index === 0} onClick={() => move(index, -1)}><ArrowUpIcon /></Button>
                      <Button type="button" size="icon" variant="ghost" aria-label={`下移 ${stage.title}`} disabled={busy || index === stages.length - 1} onClick={() => move(index, 1)}><ArrowDownIcon /></Button>
                      {!stage.is_current ? (
                        <Button type="button" size="sm" variant="outline" disabled={busy}
                          onClick={() => void act(() => saveCharacterSettings(botId, overview.bot.version, { current_stage: stage.stage_key }), `当前时间点设为「${stage.title}」`)}>
                          设为当前时间点
                        </Button>
                      ) : null}
                      {stage.status === 'draft' ? (
                        <Button type="button" size="sm" variant="outline" disabled={busy}
                          onClick={() => void act(() => confirmTimelineStages(botId, [{ stage_key: stage.stage_key, version: stage.version }]), `已确认「${stage.title}」`)}>确认</Button>
                      ) : null}
                      <Button type="button" size="icon" variant="ghost" aria-label={`编辑 ${stage.title}`} disabled={busy} onClick={() => { setEditing(stage.stage_key); setCreating(false); setDraft(toDraft(stage)) }}><PencilIcon /></Button>
                      {!used && !stage.is_current ? (
                        <Button type="button" size="icon" variant="ghost" aria-label={`删除 ${stage.title}`} disabled={busy}
                          onClick={() => void act(() => deleteTimelineStage(botId, stage.stage_key, stage.version), `已删除「${stage.title}」`)}><Trash2Icon /></Button>
                      ) : null}
                    </span>
                  </div>
                  {stage.description ? <p className="mt-1 text-xs text-muted-foreground">{stage.description}</p> : null}
                  {stage.key_events.length ? (
                    <ul className="mt-1 list-disc pl-5 text-xs">
                      {stage.key_events.map((event) => <li key={event.text}>{event.text}{event.basis.length ? <span className="text-muted-foreground">（依据设定 {event.basis.map((n) => `[${n}]`).join('')}）</span> : null}</li>)}
                    </ul>
                  ) : null}
                  {editing === stage.stage_key ? (
                    <div className="mt-2 flex flex-col gap-2 border-t pt-2">
                      <StageForm draft={draft} onChange={setDraft} isNew={false} />
                      <div className="flex gap-2">
                        <Button type="button" size="sm" disabled={busy || !draft.title.trim()} onClick={() => void saveEdit(stage)}><SaveIcon aria-hidden="true" />保存</Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>取消</Button>
                      </div>
                    </div>
                  ) : null}
                </li>
              )
            })}
          </ol>
        ) : <p className="text-sm text-muted-foreground">还没有时间线。可以手动新增阶段，或在「按设定编写」里让模型起草；导入 JSONL 时会按 arc 自动建。</p>}
        {overview.settings.current_stage ? (
          <div>
            <Button type="button" size="sm" variant="ghost" disabled={busy}
              onClick={() => void act(() => saveCharacterSettings(botId, overview.bot.version, { current_stage: '' }), '已取消时间点限制')}>
              不限时间点（能想起所有阶段）
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

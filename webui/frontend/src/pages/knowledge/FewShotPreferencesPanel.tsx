import { useCallback, useEffect, useState } from 'react'
import { PlusIcon, SaveIcon, Trash2Icon } from 'lucide-react'
import { toast } from 'sonner'

import {
  emptyPreferences,
  getBot,
  listBots,
  saveBot,
  type BotProfileDto,
  type PreferenceContext,
  type PreferenceItemDto,
  type ReactionExampleDto,
} from '@/api/bots'
import { QueryState } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

const CONTEXT_LABELS: Record<PreferenceContext, string> = { all: '所有场合', group: '只在群聊', private: '只在私聊' }

function csv(values: string[]): string {
  return values.join(', ')
}
function fromCsv(text: string): string[] {
  return text.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
}

function ContextSelect({ value, onChange }: { value: PreferenceContext; onChange: (value: PreferenceContext) => void }) {
  return (
    <Select value={value || 'all'} onValueChange={(next) => onChange(next as PreferenceContext)}>
      <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
      <SelectContent>
        {(Object.keys(CONTEXT_LABELS) as PreferenceContext[]).map((key) => (
          <SelectItem key={key} value={key}>{CONTEXT_LABELS[key]}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function PreferenceRows({ items, onChange, addLabel }: {
  items: PreferenceItemDto[]; onChange: (items: PreferenceItemDto[]) => void; addLabel: string
}) {
  const update = (index: number, patch: Partial<PreferenceItemDto>) => {
    onChange(items.map((item, current) => (current === index ? { ...item, ...patch } : item)))
  }
  return (
    <div className="flex flex-col gap-2">
      {items.map((item, index) => (
        <div key={index} className="grid items-end gap-2 rounded-md border p-2 md:grid-cols-[minmax(12rem,1.6fr)_minmax(10rem,1fr)_5rem_auto_auto]">
          <Field className="gap-1">
            <FieldLabel className="text-xs">她怎么想</FieldLabel>
            <Input value={item.text} placeholder="例如：精打细算，把资源花在刀刃上" onChange={(event) => update(index, { text: event.target.value })} />
          </Field>
          <Field className="gap-1">
            <FieldLabel className="text-xs">相关词（逗号分隔）</FieldLabel>
            <Input value={csv(item.tags)} placeholder="省钱, 灵石, 成本" onChange={(event) => update(index, { tags: fromCsv(event.target.value) })} />
          </Field>
          <Field className="gap-1">
            <FieldLabel className="text-xs">强度</FieldLabel>
            <Input inputMode="numeric" value={String(item.strength)} onChange={(event) => update(index, { strength: Math.max(1, Math.min(5, Math.round(Number(event.target.value) || 3))) })} />
          </Field>
          <ContextSelect value={item.context} onChange={(context) => update(index, { context })} />
          <Button type="button" variant="ghost" size="icon-sm" aria-label="删除这一条" onClick={() => onChange(items.filter((_row, current) => current !== index))}>
            <Trash2Icon aria-hidden="true" />
          </Button>
        </div>
      ))}
      <Button type="button" variant="outline" size="sm" className="self-start" onClick={() => onChange([...items, { text: '', tags: [], strength: 3, context: 'all' }])}>
        <PlusIcon aria-hidden="true" />{addLabel}
      </Button>
    </div>
  )
}

function ReactionRows({ items, onChange }: { items: ReactionExampleDto[]; onChange: (items: ReactionExampleDto[]) => void }) {
  const update = (index: number, patch: Partial<ReactionExampleDto>) => {
    onChange(items.map((item, current) => (current === index ? { ...item, ...patch } : item)))
  }
  return (
    <div className="flex flex-col gap-3">
      {items.map((item, index) => (
        <div key={index} className="grid gap-2 rounded-md border p-3 md:grid-cols-2">
          <Field className="gap-1 md:col-span-2">
            <FieldLabel className="text-xs">什么情景</FieldLabel>
            <Input value={item.situation} placeholder="例如：有人要花一大笔钱买没用的东西" onChange={(event) => update(index, { situation: event.target.value })} />
          </Field>
          <Field className="gap-1 md:col-span-2">
            <FieldLabel className="text-xs">她通常会怎么反应</FieldLabel>
            <Textarea rows={2} value={item.reaction} placeholder="先算成本，嘴上嫌贵，最后给一个更省钱的方案。" onChange={(event) => update(index, { reaction: event.target.value })} />
          </Field>
          <Field className="gap-1">
            <FieldLabel className="text-xs">相关词（逗号分隔）</FieldLabel>
            <Input value={csv(item.tags)} placeholder="买, 贵, 灵石" onChange={(event) => update(index, { tags: fromCsv(event.target.value) })} />
          </Field>
          <div className="flex items-end gap-2">
            <Field className="gap-1 w-24">
              <FieldLabel className="text-xs">强度</FieldLabel>
              <Input inputMode="numeric" value={String(item.strength)} onChange={(event) => update(index, { strength: Math.max(1, Math.min(5, Math.round(Number(event.target.value) || 3))) })} />
            </Field>
            <ContextSelect value={item.context} onChange={(context) => update(index, { context })} />
            <Button type="button" variant="ghost" size="icon-sm" aria-label="删除这一条" onClick={() => onChange(items.filter((_row, current) => current !== index))}>
              <Trash2Icon aria-hidden="true" />
            </Button>
          </div>
        </div>
      ))}
      <Button type="button" variant="outline" size="sm" className="self-start" onClick={() => onChange([...items, { situation: '', reaction: '', tags: [], strength: 3, context: 'all' }])}>
        <PlusIcon aria-hidden="true" />添加一条典型反应
      </Button>
    </div>
  )
}

export function FewShotPreferencesPanel({ defaultBotId = '' }: { defaultBotId?: string }) {
  const [bots, setBots] = useState<Array<{ db_id: string; name: string }>>([])
  const [botId, setBotId] = useState(defaultBotId)
  const [profile, setProfile] = useState<BotProfileDto | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState<unknown>()

  useEffect(() => {
    listBots()
      .then((payload) => {
        setBots(payload.items.map((item) => ({ db_id: item.db_id, name: item.name || item.db_id })))
        setBotId((current) => current || payload.items[0]?.db_id || '')
      })
      .catch((reason: unknown) => setError(reason))
  }, [])

  useEffect(() => {
    if (!botId) { setProfile(null); return }
    let active = true
    setLoading(true)
    setError(undefined)
    getBot(botId)
      .then((payload) => {
        if (!active) return
        const next = {
          ...payload.item,
          persona: { ...payload.item.persona, preferences: payload.item.persona.preferences ?? emptyPreferences() },
        }
        setProfile(next)
        setDirty(false)
      })
      .catch((reason: unknown) => { if (active) setError(reason) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [botId])

  const updatePreferences = useCallback((patch: Partial<ReturnType<typeof emptyPreferences>>) => {
    setProfile((current) => {
      if (!current) return current
      const preferences = { ...(current.persona.preferences ?? emptyPreferences()), ...patch }
      return { ...current, persona: { ...current.persona, preferences } }
    })
    setDirty(true)
  }, [])

  const save = async () => {
    if (!profile) return
    setSaving(true)
    try {
      const saved = await saveBot(profile.db_id, profile, profile.version, '编辑喜恶预设')
      setProfile({
        ...saved.item,
        persona: { ...saved.item.persona, preferences: saved.item.persona.preferences ?? emptyPreferences() },
      })
      setDirty(false)
      toast.success('喜恶预设已保存')
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const preferences = profile?.persona.preferences ?? emptyPreferences()
  const status = error ? 'error' : loading ? 'loading' : profile ? 'success' : 'empty'

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="max-w-2xl">
          <h2 className="text-sm font-medium">喜恶预设</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            这是她自己的喜恶，跨群也成立。只在该话题真的相关时挑 1–2 条注入，不会每轮复读；群里的说话风格在「风格样例」页。
            没有填相关词时，按文字重合度匹配。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={botId} onValueChange={setBotId}>
            <SelectTrigger className="w-44"><SelectValue placeholder="选择 Bot" /></SelectTrigger>
            <SelectContent>
              {bots.map((bot) => <SelectItem key={bot.db_id} value={bot.db_id}>{bot.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button type="button" size="sm" disabled={!dirty || saving} onClick={save}>
            <SaveIcon aria-hidden="true" />{saving ? '保存中…' : '保存'}
          </Button>
        </div>
      </div>

      <QueryState status={status} error={error} title="喜恶预设读取失败" emptyDescription="还没有可编辑的 Bot。" onRetry={() => setBotId((current) => current)}>
        {profile ? (
          <Card className="border-border/60"><CardContent className="flex flex-col gap-6 p-4">
            <section className="flex flex-col gap-2">
              <div>
                <h3 className="text-sm font-medium">喜欢</h3>
                <FieldDescription>她天然愿意靠近、会主动维护的东西。</FieldDescription>
              </div>
              <PreferenceRows items={preferences.likes} onChange={(likes) => updatePreferences({ likes })} addLabel="添加喜欢" />
            </section>
            <section className="flex flex-col gap-2">
              <div>
                <h3 className="text-sm font-medium">不喜欢</h3>
                <FieldDescription>会让她皱眉、嘴硬、想纠正的东西。</FieldDescription>
              </div>
              <PreferenceRows items={preferences.dislikes} onChange={(dislikes) => updatePreferences({ dislikes })} addLabel="添加不喜欢" />
            </section>
            <section className="flex flex-col gap-2">
              <div>
                <h3 className="text-sm font-medium">典型反应</h3>
                <FieldDescription>按「什么情景 → 她通常怎么做」写，给她脾气和分寸，不是台词模板。</FieldDescription>
              </div>
              <ReactionRows items={preferences.reaction_examples} onChange={(reaction_examples) => updatePreferences({ reaction_examples })} />
            </section>
          </CardContent></Card>
        ) : null}
      </QueryState>
    </div>
  )
}

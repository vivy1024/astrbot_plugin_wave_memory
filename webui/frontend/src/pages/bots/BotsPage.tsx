import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DownloadIcon, PlusIcon, RefreshCwIcon, SaveIcon, Trash2Icon, UploadIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  BOT_MODEL_TASKS,
  emptyBot,
  emptyCharacterExperience,
  emptyMood,
  exportBots,
  getBot,
  importBots,
  listBots,
  saveBot,
  setBotEnabled,
  type BindingHost,
  type BotBindingDto,
  type BotDetailPayload,
  type BotListPayload,
  type BotProfileDto,
  type CharacterExperienceDto,
  type MoodDto,
} from '@/api/bots'
import { isRequestCancelled } from '@/api/client'
import { listProviders, type ProviderPayload } from '@/api/config'
import { QueryState } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

const HOST_LABELS: Record<BindingHost, string> = { astrbot: 'AstrBot 账号', cortico: 'Cortico 部署', bilibili: 'B 站直播间' }
const ORIGIN_LABELS: Record<string, string> = { config: '旧配置迁移', webui: '9876 新建', import: '导入' }

function csv(values: string[]): string {
  return values.join(', ')
}
function fromCsv(text: string): string[] {
  return text.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
}
function lines(values: string[]): string {
  return values.join('\n')
}
function fromLines(text: string): string[] {
  return text.split('\n').map((item) => item.trim()).filter(Boolean)
}

function TextField({ id, label, value, onChange, hint, placeholder, disabled, list }: {
  id: string; label: string; value: string; onChange: (value: string) => void; hint?: string; placeholder?: string; disabled?: boolean; list?: string
}) {
  return (
    <Field className="gap-1">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input id={id} value={value} placeholder={placeholder} disabled={disabled} list={list} onChange={(event) => onChange(event.target.value)} />
      {hint ? <FieldDescription>{hint}</FieldDescription> : null}
    </Field>
  )
}

function AreaField({ id, label, value, onChange, hint, rows = 4 }: {
  id: string; label: string; value: string; onChange: (value: string) => void; hint?: string; rows?: number
}) {
  return (
    <Field className="gap-1">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Textarea id={id} rows={rows} value={value} onChange={(event) => onChange(event.target.value)} />
      {hint ? <FieldDescription>{hint}</FieldDescription> : null}
    </Field>
  )
}

/** 数字输入：输入过程中保留原文（如「0.」），能解析成数字时才回写。 */
function NumberField({ id, label, value, onChange, hint, min, max }: {
  id: string; label: string; value: number; onChange: (value: number) => void; hint?: string; min?: number; max?: number
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => {
    setText((current) => (Number(current) === value ? current : String(value)))
  }, [value])
  return (
    <Field className="gap-1">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input
        id={id}
        inputMode="decimal"
        value={text}
        onChange={(event) => {
          const next = event.target.value
          setText(next)
          const parsed = Number(next)
          if (next.trim() !== '' && Number.isFinite(parsed)) {
            onChange(Math.min(max ?? parsed, Math.max(min ?? parsed, parsed)))
          }
        }}
      />
      {hint ? <FieldDescription>{hint}</FieldDescription> : null}
    </Field>
  )
}

function hintLines(hints: Record<string, string>): string {
  return Object.entries(hints).map(([key, text]) => `${key}=${text}`).join('\n')
}
function fromHintLines(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const match = line.match(/^\s*([^=：:]+?)\s*[=：:]\s*(.+)$/)
    if (match) out[match[1]] = match[2].trim()
  }
  return out
}

function MoodEditor({ mood, onChange }: { mood: MoodDto; onChange: (next: MoodDto) => void }) {
  const set = <K extends keyof MoodDto>(key: K, value: MoodDto[K]) => onChange({ ...mood, [key]: value })
  // 说话倾向逐行编辑：原文保留在本地，失焦前不强行规整（否则「开心=」这样的半行会被吞掉）
  const [hintsText, setHintsText] = useState(hintLines(mood.speaking_hints))
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Field className="gap-1">
        <FieldLabel htmlFor="bot-mood-enabled">有心情</FieldLabel>
        <Switch id="bot-mood-enabled" checked={mood.enabled} onCheckedChange={(value) => set('enabled', value)} />
        <FieldDescription>一个 Bot 一份心情，跨群与私聊共享。关闭后不记情绪事件、不注入心情。</FieldDescription>
      </Field>
      <NumberField id="bot-mood-sensitivity" label="敏感度（0~3）" value={mood.sensitivity} min={0} max={3} onChange={(v) => set('sensitivity', v)}
        hint="所有事件冲击的倍数。0 = 不为外事所动，1 = 默认。" />
      <NumberField id="bot-mood-valence" label="心情基线（-1~1）" value={mood.baseline_valence} min={-1} max={1} onChange={(v) => set('baseline_valence', v)}
        hint="没发生任何事时回落到的愉悦度。正数偏乐观，负数偏忧郁。" />
      <NumberField id="bot-mood-arousal" label="精神基线（0~1）" value={mood.baseline_arousal} min={0} max={1} onChange={(v) => set('baseline_arousal', v)}
        hint="平时的激活度：越高越活泼，越低越安静。" />
      <NumberField id="bot-mood-half-life" label="恢复半衰期（分钟）" value={mood.half_life_minutes} min={0} onChange={(v) => set('half_life_minutes', v)}
        hint="事件影响每过这么久减半。0 = 用全局「心情恢复半衰期」（Lifecycle_Settings.mood_duration_hours）。" />
      <NumberField id="bot-mood-boost" label="强事件延长倍数" value={mood.strong_event_boost} min={0} max={5} onChange={(v) => set('strong_event_boost', v)}
        hint="越强烈的事影响越久：半衰期 ×（1 + 该值 × 事件强度）。" />
      <NumberField id="bot-mood-atmosphere" label="群氛围传染权重（0~1）" value={mood.atmosphere_weight} min={0} max={1} onChange={(v) => set('atmosphere_weight', v)}
        hint="群里整体气氛对她的影响，只作小权重输入。0 = 不受群氛围影响。" />
      <Field className="gap-1">
        <FieldLabel htmlFor="bot-mood-fatigue">疲劳</FieldLabel>
        <Switch id="bot-mood-fatigue" checked={mood.fatigue_enabled} onCheckedChange={(value) => set('fatigue_enabled', value)} />
        <FieldDescription>一段时间回复太多、或处于深夜时，精神下降、心情略降。</FieldDescription>
      </Field>
      <NumberField id="bot-mood-fatigue-window" label="疲劳统计窗口（分钟）" value={mood.fatigue_window_minutes} min={10} onChange={(v) => set('fatigue_window_minutes', Math.round(v))} />
      <NumberField id="bot-mood-fatigue-threshold" label="窗口内回复多少条开始累" value={mood.fatigue_reply_threshold} min={1} onChange={(v) => set('fatigue_reply_threshold', Math.round(v))}
        hint="超过开始累，两倍时最累。" />
      <NumberField id="bot-mood-night-start" label="深夜开始（时）" value={mood.night_start_hour} min={0} max={23} onChange={(v) => set('night_start_hour', Math.round(v))}
        hint="服务器本机时区。开始与结束相同表示不按深夜算疲劳。" />
      <NumberField id="bot-mood-night-end" label="深夜结束（时）" value={mood.night_end_hour} min={0} max={23} onChange={(v) => set('night_end_hour', Math.round(v))} />
      <NumberField id="bot-mood-fatigue-strength" label="疲劳强度（0~1）" value={mood.fatigue_strength} min={0} max={1} onChange={(v) => set('fatigue_strength', v)} />
      <TextField id="bot-mood-positive" label="额外正面词（逗号分隔）" value={csv(mood.positive_words)} onChange={(v) => set('positive_words', fromCsv(v))}
        hint="叠加在通用词表上，用于识别冲她来的夸奖。" />
      <TextField id="bot-mood-negative" label="额外负面词（逗号分隔）" value={csv(mood.negative_words)} onChange={(v) => set('negative_words', fromCsv(v))}
        hint="叠加在通用词表上，用于识别冲她来的损话。" />
      <div className="md:col-span-2">
        <AreaField id="bot-mood-hints" label="各心情下的说话倾向（每行「情绪词=提示」）" rows={5} value={hintsText}
          onChange={(v) => { setHintsText(v); set('speaking_hints', fromHintLines(v)) }}
          hint="情绪词如 开心 / 烦躁 / 委屈 / 疲惫，也可写类别 正面 / 负面 / 平静 / 疲惫。命中时随心情一起注入；空则不写。" />
      </div>
    </div>
  )
}

/** 角色经历：进群之前「过去的人生」的召回设置。没开启的 Bot 完全不会想起角色经历。 */
function CharacterExperienceEditor({ value, onChange, dbId, isNew }: {
  value: CharacterExperienceDto; onChange: (next: CharacterExperienceDto) => void; dbId: string; isNew: boolean
}) {
  const set = <K extends keyof CharacterExperienceDto>(key: K, next: CharacterExperienceDto[K]) => onChange({ ...value, [key]: next })
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Field className="gap-1">
        <FieldLabel htmlFor="bot-ce-enabled">启用角色经历</FieldLabel>
        <Switch id="bot-ce-enabled" checked={value.enabled} onCheckedChange={(next) => set('enabled', next)} />
        <FieldDescription>书中角色、原创角色进群之前的经历。开启后她会在话题相关时想起自己的过去；只有她自己看得到，别的 Bot 完全看不到。</FieldDescription>
      </Field>
      <TextField id="bot-ce-stage" label="当前时间点（阶段 key）" value={value.current_stage} onChange={(v) => set('current_stage', v.trim())}
        placeholder="留空 = 不限" hint="只会想起这个阶段及之前的经历（避免剧透）。在角色经历工作台的时间线里点「设为当前时间点」更方便。" />
      <NumberField id="bot-ce-topk" label="每次最多想起几条（1~10）" value={value.top_k} min={1} max={10} onChange={(v) => set('top_k', Math.round(v))} />
      <NumberField id="bot-ce-chars" label="注入上限（字）" value={value.max_chars} min={100} max={4000} onChange={(v) => set('max_chars', Math.round(v))}
        hint="一次注入的经历正文总字数上限。" />
      <NumberField id="bot-ce-score" label="最低相似度（0~1）" value={value.min_score} min={0} max={1} onChange={(v) => set('min_score', v)}
        hint="话题和经历的向量相似度低于它就不想起；关键词直接命中的放宽一些。" />
      <TextField id="bot-ce-legacy" label="接管的旧记忆来源（逗号分隔）" value={csv(value.legacy_sources)} onChange={(v) => set('legacy_sources', fromCsv(v))}
        hint="记忆表里已经迁进角色经历的旧来源值（如 v5 的 bzz_experience）。这些旧行不再走普通记忆召回，避免同一条经历出现两次。" />
      <TextField id="bot-ce-lore" label="个人经历书设类别（逗号分隔）" value={csv(value.lore_categories)} onChange={(v) => set('lore_categories', fromCsv(v))}
        hint="书设库里属于她个人经历的笔记类别；书设通道不把它们当世界观注入。" />
      <div className="md:col-span-2 text-sm text-muted-foreground">
        {isNew ? '保存 Bot 后可以在角色经历工作台里导入、提取或编写她的经历。' : (
          <Link className="underline" to={{ pathname: '/knowledge/character-experience', search: `?bot_id=${encodeURIComponent(dbId)}` }}>打开角色经历工作台</Link>
        )}
      </div>
    </div>
  )
}

function withModel(models: Record<string, string> | undefined, task: string, value: string): Record<string, string> {
  const next = { ...(models ?? {}) }
  if (value) next[task] = value
  else delete next[task]
  return next
}

/** 模型：该 Bot 的默认模型 + 按任务覆盖，只作用于以 Bot 口吻产出的任务。 */
function ModelFields({ draft, onChange }: { draft: BotProfileDto; onChange: (next: BotProfileDto) => void }) {
  const [providers, setProviders] = useState<ProviderPayload[]>([])
  useEffect(() => {
    let alive = true
    listProviders()
      .then((payload) => { if (alive) setProviders(payload.providers ?? []) })
      .catch(() => { if (alive) setProviders([]) })  // 选项只是输入提示，拿不到也能手填
    return () => { alive = false }
  }, [])
  return (
    <div className="space-y-2 rounded-lg border p-3 md:col-span-2">
      <FieldLabel>模型</FieldLabel>
      <FieldDescription>
        只作用于以这个 Bot 口吻产出的任务：内心判断、事后记账、每日日记、关系小传、信念反思、角色经历。优先级：按任务覆盖 → 默认模型 → 配置中心「模型分工」 → 所属默认；前面的不可用时依次回退。保存后立即生效。
      </FieldDescription>
      <div className="grid gap-3 md:grid-cols-2">
        <TextField id="bot-model" label="默认模型" value={draft.model} onChange={(v) => onChange({ ...draft, model: v })}
          list="bot-provider-options" placeholder="留空跟随模型分工" hint="下面没单独填的任务都先用它。" />
        {BOT_MODEL_TASKS.map((task) => (
          <TextField
            key={task.id}
            id={`bot-model-${task.id}`}
            label={`模型 · ${task.label}`}
            value={draft.models?.[task.id] ?? ''}
            onChange={(v) => onChange({ ...draft, models: withModel(draft.models, task.id, v) })}
            list="bot-provider-options"
            placeholder="留空跟随默认模型"
          />
        ))}
      </div>
      <datalist id="bot-provider-options">
        {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.model}</option>)}
      </datalist>
    </div>
  )
}

function bindingValue(binding: BotBindingDto): string {
  if (binding.host === 'astrbot') return binding.self_id ?? ''
  if (binding.host === 'cortico') return binding.deployment ?? ''
  return binding.room ?? ''
}

function withBindingValue(binding: BotBindingDto, value: string): BotBindingDto {
  if (binding.host === 'astrbot') return { ...binding, self_id: value }
  if (binding.host === 'cortico') return { ...binding, deployment: value }
  return { ...binding, room: value }
}

function BindingsEditor({ bindings, onChange }: { bindings: BotBindingDto[]; onChange: (next: BotBindingDto[]) => void }) {
  return (
    <div className="flex flex-col gap-2">
      {bindings.map((binding, index) => (
        <div key={`${binding.host}-${index}`} className="flex flex-wrap items-center gap-2">
          <select
            aria-label="绑定类型"
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={binding.host}
            onChange={(event) => onChange(bindings.map((item, i) => (i === index ? { host: event.target.value as BindingHost } : item)))}
          >
            {(Object.keys(HOST_LABELS) as BindingHost[]).map((host) => <option key={host} value={host}>{HOST_LABELS[host]}</option>)}
          </select>
          <Input
            aria-label="绑定值"
            className="w-56"
            placeholder={binding.host === 'astrbot' ? 'QQ 号' : binding.host === 'cortico' ? '部署名，如 yushu-live' : '直播间号'}
            value={bindingValue(binding)}
            onChange={(event) => onChange(bindings.map((item, i) => (i === index ? withBindingValue(item, event.target.value) : item)))}
          />
          {binding.host === 'astrbot' ? (
            <Input
              aria-label="平台实例 id"
              className="w-44"
              placeholder="平台实例 id（可选）"
              value={binding.platform ?? ''}
              onChange={(event) => onChange(bindings.map((item, i) => (i === index ? { ...item, platform: event.target.value } : item)))}
            />
          ) : null}
          <Button type="button" variant="ghost" size="icon" aria-label="删除绑定" onClick={() => onChange(bindings.filter((_, i) => i !== index))}>
            <Trash2Icon className="size-4" />
          </Button>
        </div>
      ))}
      <div>
        <Button type="button" variant="outline" size="sm" onClick={() => onChange([...bindings, { host: 'cortico', deployment: '' }])}>
          <PlusIcon className="size-4" />添加绑定
        </Button>
      </div>
    </div>
  )
}

function BotEditor({ draft, onChange, isNew, initialTab = 'identity' }: { draft: BotProfileDto; onChange: (next: BotProfileDto) => void; isNew: boolean; initialTab?: string }) {
  const set = <K extends keyof BotProfileDto>(key: K, value: BotProfileDto[K]) => onChange({ ...draft, [key]: value })
  const setPersona = <K extends keyof BotProfileDto['persona']>(key: K, value: BotProfileDto['persona'][K]) =>
    onChange({ ...draft, persona: { ...draft.persona, [key]: value } })
  return (
    <Tabs key={initialTab} defaultValue={initialTab} className="flex flex-col gap-4">
      <TabsList>
        <TabsTrigger value="identity">身份与绑定</TabsTrigger>
        <TabsTrigger value="persona">人设</TabsTrigger>
        <TabsTrigger value="behavior">行为</TabsTrigger>
        <TabsTrigger value="mood">心情</TabsTrigger>
        <TabsTrigger value="character">角色经历</TabsTrigger>
        <TabsTrigger value="advanced">通道与工具</TabsTrigger>
      </TabsList>
      <TabsContent value="identity" className="grid gap-4 md:grid-cols-2">
        <TextField id="bot-db-id" label="db_id（稳定主键）" value={draft.db_id} disabled={!isNew} onChange={(v) => set('db_id', v)}
          hint="历史数据全部按它归属，创建后不能改。只用字母、数字、下划线、短横线。" />
        <TextField id="bot-name" label="显示名" value={draft.name} onChange={(v) => set('name', v)} />
        <TextField id="bot-qq" label="AstrBot 主账号（QQ 号）" value={draft.qq_id} onChange={(v) => set('qq_id', v)}
          hint="只接 Cortico 的 Bot 可以留空。" />
        <TextField id="bot-prefix" label="规范会话前缀" value={draft.session_prefix} onChange={(v) => set('session_prefix', v)}
          hint="记忆会话 id 的前缀（如「前缀:group:群号」）。留空时沿用 AstrBot 平台实例 id；在 AstrBot 里给平台改名后，这里保持不变即可让新旧记忆接上。" />
        <TextField id="bot-aliases" label="别名（逗号分隔）" value={csv(draft.aliases)} onChange={(v) => set('aliases', fromCsv(v))} />
        <Field className="gap-1">
          <FieldLabel htmlFor="bot-enabled">启用</FieldLabel>
          <Switch id="bot-enabled" checked={draft.enabled} onCheckedChange={(value) => set('enabled', value)} />
          <FieldDescription>停用后不再接收消息、不参与注入，数据保留。</FieldDescription>
        </Field>
        <div className="md:col-span-2">
          <FieldLabel>其他身份绑定</FieldLabel>
          <FieldDescription className="mb-2">同一个 Bot 的其他 QQ 账号、Cortico 部署名、B 站直播间。Runtime API 按这些绑定找 Bot。</FieldDescription>
          <BindingsEditor bindings={draft.bindings} onChange={(next) => set('bindings', next)} />
        </div>
      </TabsContent>
      <TabsContent value="persona" className="grid gap-4 md:grid-cols-2">
        <TextField id="bot-self-terms" label="额外自称词（逗号分隔）" value={csv(draft.persona.self_terms)} onChange={(v) => setPersona('self_terms', fromCsv(v))}
          hint="名字和别名之外、指代这个 Bot 的词，用于身份安全检测。" />
        <TextField id="bot-diary" label="日记署名" value={draft.persona.diary_signature} onChange={(v) => setPersona('diary_signature', v)} hint="留空用显示名。" />
        <TextField id="bot-exp-source" label="第一人称经历来源（旧版）" value={draft.persona.experience_source} onChange={(v) => setPersona('experience_source', v)}
          hint="旧版：记忆表里这个 Bot 自己经历的 source 值。进群之前的经历请用「角色经历」标签页。" />
        <Field className="gap-1">
          <FieldLabel htmlFor="bot-guard">身份安全防护</FieldLabel>
          <Switch id="bot-guard" checked={draft.persona.identity_guard_enabled} onCheckedChange={(value) => setPersona('identity_guard_enabled', value)} />
          <FieldDescription>防认主、防猫娘化。关闭会放开防护。</FieldDescription>
        </Field>
        <TextField id="bot-lore-title" label="常驻书设标题" value={draft.persona.lore_title} onChange={(v) => setPersona('lore_title', v)} />
        <TextField id="bot-lore-corpus" label="书设语料" value={draft.persona.lore_corpus ?? ''} onChange={(v) => setPersona('lore_corpus', v)}
          placeholder="留空用部署默认" hint="书设检索与纠错自省读哪份语料。填 none 表示这个 Bot 不读书设；填其他语料 id 须与部署加载的一致，否则不读。" />
        <div className="md:col-span-2">
          <AreaField id="bot-lore" label="常驻书设（每行一条）" rows={5} value={lines(draft.persona.lore_lines)} onChange={(v) => setPersona('lore_lines', fromLines(v))}
            hint="Cortico 等外部应用调用注入接口时放在最前面的灵魂底色。" />
        </div>
        <div className="md:col-span-2">
          <AreaField id="bot-guard-rules" label="追加身份规则（每行一条）" value={lines(draft.persona.identity_guard_rules)} onChange={(v) => setPersona('identity_guard_rules', fromLines(v))} />
        </div>
      </TabsContent>
      <TabsContent value="behavior" className="grid gap-4 md:grid-cols-2">
        <TextField id="bot-interest" label="兴趣关键词（逗号分隔）" value={csv(draft.interest_keywords)} onChange={(v) => set('interest_keywords', fromCsv(v))} />
        <TextField id="bot-exclude" label="排除的记忆来源（逗号分隔）" value={csv(draft.exclude_sources)} onChange={(v) => set('exclude_sources', fromCsv(v))} />
        <Field className="gap-1">
          <FieldLabel htmlFor="bot-proactive">主动插话</FieldLabel>
          <Switch id="bot-proactive" checked={draft.proactive_enabled} onCheckedChange={(value) => set('proactive_enabled', value)} />
        </Field>
        <TextField id="bot-interval" label="主动插话最小间隔（秒）" value={String(draft.proactive_interval_seconds)} onChange={(v) => set('proactive_interval_seconds', Number(v) || 0)} />
        <TextField id="bot-max" label="每小时最多主动插话" value={String(draft.proactive_max_per_hour)} onChange={(v) => set('proactive_max_per_hour', Number(v) || 0)} />
        <div className="md:col-span-2">
          <AreaField id="bot-meta" label="MetaThinking 提示词" rows={6} value={draft.meta_prompt} onChange={(v) => set('meta_prompt', v)} hint="留空用默认模板。" />
        </div>
        <ModelFields draft={draft} onChange={onChange} />
      </TabsContent>
      <TabsContent value="mood">
        <MoodEditor mood={draft.mood ?? emptyMood()} onChange={(next) => set('mood', next)} />
      </TabsContent>
      <TabsContent value="character">
        <CharacterExperienceEditor value={draft.character_experience ?? emptyCharacterExperience()} onChange={(next) => set('character_experience', next)} dbId={draft.db_id} isNew={isNew} />
      </TabsContent>
      <TabsContent value="advanced" className="grid gap-4 md:grid-cols-2">
        <TextField id="bot-tools-allow" label="只开放这些工具（逗号分隔）" value={csv(draft.tools_allow)} onChange={(v) => set('tools_allow', fromCsv(v))} hint="留空表示全部开放。" />
        <TextField id="bot-tools-deny" label="关闭这些工具（逗号分隔）" value={csv(draft.tools_deny)} onChange={(v) => set('tools_deny', fromCsv(v))} />
        <div className="md:col-span-2">
          <AreaField
            id="bot-channels"
            label="注入通道覆盖（JSON）"
            rows={6}
            value={JSON.stringify(draft.channels, null, 2)}
            onChange={(v) => { try { set('channels', JSON.parse(v || '{}')) } catch { /* 等用户输完再解析 */ } }}
            hint={'叠加在全局通道配置之上，如 {"jargon": {"enabled": false}}。'}
          />
        </div>
      </TabsContent>
    </Tabs>
  )
}

export function BotsPage() {
  const [payload, setPayload] = useState<BotListPayload | null>(null)
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [error, setError] = useState<string>('')
  // ?bot=<db_id>&tab=<标签>：配置中心 / 配置来源跳来时直接打开对应 Bot 与标签页
  const [searchParams] = useSearchParams()
  const focusBot = searchParams.get('bot')
  const focusTab = searchParams.get('tab') ?? 'identity'
  const [selected, setSelected] = useState<string | null>(focusBot)
  useEffect(() => {
    if (focusBot) setSelected(focusBot)
  }, [focusBot])
  const [detail, setDetail] = useState<BotDetailPayload | null>(null)
  const [draft, setDraft] = useState<BotProfileDto | null>(null)
  const [saving, setSaving] = useState(false)
  const isNew = draft !== null && draft.version === 0

  const load = useCallback(async (signal?: AbortSignal) => {
    setStatus('loading')
    try {
      const data = await listBots(signal)
      setPayload(data)
      setStatus('success')
    } catch (err) {
      if (isRequestCancelled(err)) return
      setError(err instanceof Error ? err.message : String(err))
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    const controller = new AbortController()
    getBot(selected, controller.signal)
      .then((data) => { setDetail(data); setDraft(data.item) })
      .catch((err) => { if (!isRequestCancelled(err)) toast.error(err instanceof Error ? err.message : String(err)) })
    return () => controller.abort()
  }, [selected])

  const dirty = useMemo(() => Boolean(draft && detail && JSON.stringify(draft) !== JSON.stringify(detail.item)) || isNew, [draft, detail, isNew])

  async function handleSave() {
    if (!draft) return
    setSaving(true)
    try {
      const result = await saveBot(draft.db_id, draft, draft.version)
      toast.success(`已保存 ${result.item.name}，已热生效（版本 ${result.item.version}）`)
      await load()
      setSelected(result.item.db_id)
      setDraft(result.item)
      setDetail((prev) => (prev ? { ...prev, item: result.item } : prev))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleToggle(bot: BotProfileDto) {
    try {
      await setBotEnabled(bot.db_id, !bot.enabled, bot.version)
      toast.success(bot.enabled ? `已停用 ${bot.name}` : `已启用 ${bot.name}`)
      await load()
      if (selected === bot.db_id) setSelected(null)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function handleExport() {
    const data = await exportBots()
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'wavememory-bots.json'
    link.click()
    URL.revokeObjectURL(url)
  }

  async function handleImport(file: File) {
    try {
      const parsed = JSON.parse(await file.text())
      const items = Array.isArray(parsed) ? parsed : parsed.items
      const result = await importBots(items)
      if (result.imported.length) toast.success(`已导入 ${result.imported.join('、')}`)
      for (const [id, message] of Object.entries(result.errors)) toast.error(`${id}：${message}`)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  const registry = payload?.status
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" onClick={() => { setSelected(null); setDetail(null); setDraft(emptyBot()) }}><PlusIcon className="size-4" />新建 Bot</Button>
        <Button type="button" variant="outline" onClick={() => void load()}><RefreshCwIcon className="size-4" />刷新</Button>
        <Button type="button" variant="outline" onClick={() => void handleExport()}><DownloadIcon className="size-4" />导出</Button>
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm">
          <UploadIcon className="size-4" />导入
          <input type="file" accept="application/json" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleImport(file); event.target.value = '' }} />
        </label>
      </div>
      {registry && !registry.attached ? (
        <Alert variant="destructive"><AlertTitle>Bot 注册表没有接上数据库</AlertTitle><AlertDescription>现在用的是 AstrBot 静态配置里的旧槽位，暂时不能在这里修改。查看启动日志中的 BotRegistry 错误。</AlertDescription></Alert>
      ) : null}
      {registry && Object.keys(registry.invalid_rows).length ? (
        <Alert variant="destructive"><AlertTitle>有 Bot 数据损坏，未加载</AlertTitle><AlertDescription>{Object.entries(registry.invalid_rows).map(([id, msg]) => `${id}：${msg}`).join('；')}</AlertDescription></Alert>
      ) : null}
      <QueryState status={status} error={error} onRetry={() => void load()}>
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <Card>
            <CardHeader>
              <CardTitle>Bot 列表</CardTitle>
              <CardDescription>{registry ? `启用 ${registry.enabled} / 共 ${registry.total} 个 · 来源：${registry.source === 'database' ? '数据库' : '静态配置'}` : ''}</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {(payload?.items ?? []).map((bot) => (
                <div key={bot.db_id} className={`flex items-center justify-between gap-2 rounded-md border p-2 ${selected === bot.db_id ? 'border-primary' : ''}`}>
                  <button type="button" className="flex flex-1 flex-col items-start text-left" onClick={() => setSelected(bot.db_id)}>
                    <span className="font-medium">{bot.name} <span className="font-mono text-xs text-muted-foreground">{bot.db_id}</span></span>
                    <span className="text-xs text-muted-foreground">
                      {bot.self_ids?.length ? `QQ ${bot.self_ids.join('/')}` : '无 QQ 账号'}
                      {bot.bindings.filter((b) => b.host !== 'astrbot').map((b) => ` · ${HOST_LABELS[b.host]} ${bindingValue(b)}`).join('')}
                    </span>
                    <span className="mt-1 flex gap-1">
                      <Badge variant={bot.enabled ? 'default' : 'secondary'}>{bot.enabled ? '启用' : '已停用'}</Badge>
                      <Badge variant="outline">{ORIGIN_LABELS[bot.origin] ?? bot.origin}</Badge>
                    </span>
                  </button>
                  <Switch aria-label={`启用 ${bot.name}`} checked={bot.enabled} onCheckedChange={() => void handleToggle(bot)} />
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-start justify-between gap-2">
              <div>
                <CardTitle>{draft ? (isNew ? '新建 Bot' : `${draft.name}（版本 ${draft.version}）`) : '选择一个 Bot'}</CardTitle>
                <CardDescription>保存后立即热生效，不需要重启 AstrBot。</CardDescription>
              </div>
              {draft ? <Button type="button" disabled={!dirty || saving} onClick={() => void handleSave()}><SaveIcon className="size-4" />{saving ? '保存中…' : '保存'}</Button> : null}
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {draft ? <BotEditor draft={draft} onChange={setDraft} isNew={isNew} initialTab={focusBot && selected === focusBot ? focusTab : 'identity'} /> : <p className="text-sm text-muted-foreground">从左侧选择 Bot 查看和编辑，或新建一个。</p>}
              {detail && !isNew ? (
                <div className="text-sm text-muted-foreground">
                  <p>名下数据：{Object.entries(detail.counts).map(([k, v]) => `${k} ${v}`).join('，') || '—'}</p>
                  {detail.history.length ? (
                    <p>最近修改：{detail.history.slice(0, 5).map((h) => `v${h.version} ${new Date(h.changed_at * 1000).toLocaleString()} ${h.changed_by}${h.reason ? `（${h.reason}）` : ''}`).join('；')}</p>
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </QueryState>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { DownloadIcon, PlusIcon, RefreshCwIcon, SaveIcon, Trash2Icon, UploadIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  emptyBot,
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
} from '@/api/bots'
import { isRequestCancelled } from '@/api/client'
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

function TextField({ id, label, value, onChange, hint, placeholder, disabled }: {
  id: string; label: string; value: string; onChange: (value: string) => void; hint?: string; placeholder?: string; disabled?: boolean
}) {
  return (
    <Field className="gap-1">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input id={id} value={value} placeholder={placeholder} disabled={disabled} onChange={(event) => onChange(event.target.value)} />
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
        <TextField id="bot-exp-source" label="第一人称经历来源" value={draft.persona.experience_source} onChange={(v) => setPersona('experience_source', v)}
          hint="这个 Bot 自己经历的记忆 source 值。" />
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
        <TextField id="bot-model" label="模型" value={draft.model} onChange={(v) => set('model', v)} hint="留空用全局默认。" />
        <Field className="gap-1">
          <FieldLabel htmlFor="bot-proactive">主动插话</FieldLabel>
          <Switch id="bot-proactive" checked={draft.proactive_enabled} onCheckedChange={(value) => set('proactive_enabled', value)} />
        </Field>
        <TextField id="bot-interval" label="主动插话最小间隔（秒）" value={String(draft.proactive_interval_seconds)} onChange={(v) => set('proactive_interval_seconds', Number(v) || 0)} />
        <TextField id="bot-max" label="每小时最多主动插话" value={String(draft.proactive_max_per_hour)} onChange={(v) => set('proactive_max_per_hour', Number(v) || 0)} />
        <div className="md:col-span-2">
          <AreaField id="bot-meta" label="MetaThinking 提示词" rows={6} value={draft.meta_prompt} onChange={(v) => set('meta_prompt', v)} hint="留空用默认模板。" />
        </div>
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

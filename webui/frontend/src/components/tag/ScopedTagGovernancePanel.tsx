import { useCallback, useEffect, useMemo, useState } from 'react'
import { CheckIcon, EyeIcon, Loader2Icon, RefreshCwIcon, ShieldAlertIcon } from 'lucide-react'
import { toast } from 'sonner'

import { getScopeOptions, scopeOptionsFor } from '@/api/options'
import {
  compensateScopedTagSuggestion,
  createScopedTagSuggestion,
  getScopedTags,
  getScopedTagSuggestions,
  previewScopedTagSuggestion,
  resolveScopedTagSuggestion,
  resolveScopedTagSuggestionBatch,
  type GovernanceAction,
  type GovernancePreview,
  type ScopedGovernanceScope,
  type ScopedTagItem,
  type ScopedTagSuggestion,
} from '@/api/tags'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { humanizeApiError } from '@/lib/reason-label'

const actionLabels: Record<GovernanceAction, string> = { merge: '合并', retype: '重分类', alias: '增加别名', deactivate: '停用' }

export function ScopedTagGovernancePanel({ initialScope }: { initialScope?: ScopedGovernanceScope | null } = {}) {
  const [botId, setBotId] = useState(initialScope?.bot_id ?? '')
  const [sessionId, setSessionId] = useState(initialScope?.session_id ?? '')
  const [options, setOptions] = useState<{ bot: Array<{ value: string; label: string }>; sessions: Array<{ value: string; label: string; description?: string; botId: string }> }>({ bot: [], sessions: [] })
  const scope = useMemo<ScopedGovernanceScope | null>(() => botId && sessionId ? { bot_id: botId, session_id: sessionId, visibility: 'group' } : null, [botId, sessionId])
  const [tags, setTags] = useState<ScopedTagItem[]>([])
  const [suggestions, setSuggestions] = useState<ScopedTagSuggestion[]>([])
  const [approvedSuggestions, setApprovedSuggestions] = useState<ScopedTagSuggestion[]>([])
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState<GovernanceAction>('merge')
  const [selectedRefs, setSelectedRefs] = useState<string[]>([])
  const [targetRef, setTargetRef] = useState('')
  const [targetType, setTargetType] = useState('')
  const [aliases, setAliases] = useState('')
  const [reason, setReason] = useState('')
  const [previews, setPreviews] = useState<Record<string, GovernancePreview>>({})
  const [busy, setBusy] = useState(false)

  // 继承页面作用域：别处带 bot/session 跳进来时预填，用户仍可在此改选。
  useEffect(() => {
    if (initialScope?.bot_id) setBotId(initialScope.bot_id)
    if (initialScope?.session_id) setSessionId(initialScope.session_id)
  }, [initialScope?.bot_id, initialScope?.session_id])

  const loadOptions = useCallback(async () => {
    const payload = await getScopeOptions()
    const bots = scopeOptionsFor(payload, ['bot']).map((item) => ({ value: item.value, label: item.label }))
    const sessions = payload.sessions.map((item) => ({ value: item.id, label: item.label || item.conversation_id, description: `${item.bot_id} · ${item.kind}`, botId: item.bot_id }))
    setOptions({ bot: bots, sessions })
    // 已有继承或已选作用域时不再自动挑第一个 Bot，避免覆盖用户/来路选择。
    const hasScopedEntry = Boolean(initialScope?.bot_id)
    setBotId((current) => current || (!hasScopedEntry && bots[0] ? bots[0].value : ''))
    setSessionId((current) => {
      if (current) return current
      const ownerBot = hasScopedEntry ? initialScope?.bot_id : bots[0]?.value
      const firstSession = sessions.find((item) => item.botId === ownerBot)
      return !hasScopedEntry && firstSession ? firstSession.value : ''
    })
  }, [initialScope?.bot_id, initialScope?.session_id])

  const load = useCallback(async () => {
    if (!scope) return
    setLoading(true)
    try {
      const [tagPayload, suggestionPayload, approvedPayload] = await Promise.all([
        getScopedTags(scope),
        getScopedTagSuggestions(scope),
        getScopedTagSuggestions(scope, 'approved'),
      ])
      setTags(tagPayload.items)
      setSuggestions(suggestionPayload.items)
      setApprovedSuggestions(approvedPayload.items)
      setPreviews({})
    } catch (failure) {
      toast.error(humanizeApiError(failure, '当前群标签治理数据加载失败'))
    } finally {
      setLoading(false)
    }
  }, [scope])

  useEffect(() => { void loadOptions() }, [loadOptions])
  useEffect(() => { void load() }, [load])

  const visibleSessions = options.sessions.filter((item) => item.botId === botId)
  const selectedTags = tags.filter((item) => selectedRefs.includes(item.ref))
  const canCreate = Boolean(scope && reason.trim() && selectedRefs.length > 0 && (action !== 'merge' || targetRef))

  function toggleTag(ref: string) {
    setSelectedRefs((current) => current.includes(ref) ? current.filter((item) => item !== ref) : [...current, ref])
  }

  async function createSuggestion() {
    if (!scope || !canCreate) return
    setBusy(true)
    try {
      await createScopedTagSuggestion(scope, { action, tag_refs: selectedRefs, target_tag_ref: targetRef || undefined, target_type: targetType || undefined, aliases: aliases.split(',').map((value) => value.trim()).filter(Boolean), reason: reason.trim() })
      toast.success('治理建议已创建；必须先预检才能审批')
      setReason('')
      setSelectedRefs([])
      setTargetRef('')
      await load()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '治理建议创建失败'))
    } finally {
      setBusy(false)
    }
  }

  async function preview(item: ScopedTagSuggestion) {
    if (!scope) return
    try {
      const next = await previewScopedTagSuggestion(scope, item.ref, item.revision)
      setPreviews((current) => ({ ...current, [item.ref]: next }))
    } catch (failure) {
      toast.error(humanizeApiError(failure, '治理预检失败'))
    }
  }

  function askReason(title: string, minLength: number): string | null {
    const entered = window.prompt(title)?.trim() ?? ''
    if (!entered) return null
    if (entered.length < minLength) { toast.warning(`理由至少 ${minLength} 个字`); return null }
    return entered
  }

  async function resolve(item: ScopedTagSuggestion, decision: 'approve' | 'reject') {
    if (!scope || !previews[item.ref]) { toast.warning('请先完成该建议的预检'); return }
    const approvalReason = askReason(decision === 'approve' ? '批准并应用该建议的理由（会写入审计）' : '拒绝该建议的理由（会写入审计）', 4)
    if (approvalReason === null) return
    setBusy(true)
    try {
      await resolveScopedTagSuggestion(scope, { suggestion_ref: item.ref, revision: item.revision, decision, preflight_token: previews[item.ref].preflight_token, reason: approvalReason })
      toast.success(decision === 'approve' ? '治理建议已批准并应用' : '治理建议已拒绝')
      await load()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '治理建议处理失败'))
    } finally {
      setBusy(false)
    }
  }

  async function compensate(item: ScopedTagSuggestion) {
    if (!scope) return
    const compensateReason = askReason('申请补偿（撤销该已批准治理）的理由（会写入审计，不删除原记录）', 4)
    if (compensateReason === null) return
    setBusy(true)
    try {
      await compensateScopedTagSuggestion(scope, { suggestion_ref: item.ref, revision: item.revision, reason: compensateReason })
      toast.success('治理补偿已提交并记录审计')
      await load()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '治理补偿失败'))
    } finally {
      setBusy(false)
    }
  }

  async function previewAll() {
    if (!scope || !suggestions.length) return
    setBusy(true)
    try {
      const entries = await Promise.all(suggestions.map(async (item) => [item.ref, await previewScopedTagSuggestion(scope, item.ref, item.revision)] as const))
      setPreviews(Object.fromEntries(entries))
      toast.success(`已完成当前页 ${entries.length} 条建议的全量预检`)
    } catch (failure) {
      toast.error(humanizeApiError(failure, '批量预检失败；没有提交任何审批'))
    } finally {
      setBusy(false)
    }
  }

  async function resolveAll(decision: 'approve' | 'reject') {
    if (!scope || suggestions.some((item) => !previews[item.ref])) { toast.warning('请先完成当前页全部预检'); return }
    const batchReason = askReason(decision === 'approve' ? `批量批准 ${suggestions.length} 条建议的理由（会写入审计）` : `批量拒绝 ${suggestions.length} 条建议的理由（会写入审计）`, 4)
    if (batchReason === null) return
    setBusy(true)
    try {
      await resolveScopedTagSuggestionBatch(scope, suggestions.map((item) => ({ suggestion_ref: item.ref, revision: item.revision, preflight_token: previews[item.ref].preflight_token })), decision, batchReason)
      toast.success(decision === 'approve' ? '当前页建议已全量批准并应用' : '当前页建议已全量拒绝')
      await load()
    } catch (failure) {
      toast.error(humanizeApiError(failure, '批量审批失败；服务端已保证全量校验'))
    } finally {
      setBusy(false)
    }
  }

  return <Card data-slot="scoped-tag-governance" className="border-primary/20">
    <CardHeader className="gap-3 border-b">
      <CardTitle className="flex items-center gap-2 text-base"><ShieldAlertIcon aria-hidden="true" />当前群标签治理与审批</CardTitle>
      <CardDescription>只操作当前 Bot 和群里的标签。所有应用必须先预检。</CardDescription>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field><FieldLabel htmlFor="governance-bot">Bot</FieldLabel><select id="governance-bot" className="h-8 rounded-md border bg-background px-2 text-sm" value={botId} onChange={(event) => { setBotId(event.target.value); setSessionId('') }}><option value="">选择 Bot…</option>{options.bot.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>
        <Field><FieldLabel htmlFor="governance-session">群 / 会话</FieldLabel><select id="governance-session" className="h-8 rounded-md border bg-background px-2 text-sm" value={sessionId} onChange={(event) => setSessionId(event.target.value)}><option value="">选择群…</option>{visibleSessions.map((item) => <option key={item.value} value={item.value}>{item.label} · {item.description}</option>)}</select></Field>
      </div>
    </CardHeader>
    <CardContent className="flex flex-col gap-5 p-4">
      {!scope ? <Alert><AlertTitle>请先选择 Bot 和群聊</AlertTitle><AlertDescription>请在上方选择有效的 Bot 和目标群聊以进行标签治理。</AlertDescription></Alert> : null}
      {scope ? <>
        <div className="grid gap-4 rounded-lg border bg-muted/10 p-4 lg:grid-cols-[1fr_1fr]">
          <div className="flex flex-col gap-3"><div><h3 className="font-medium">创建治理建议</h3><p className="text-xs text-muted-foreground">建议创建本身会留痕；真正改标签仍需先预检再批准。</p></div><div className="grid gap-2 sm:grid-cols-2"><Field><FieldLabel htmlFor="governance-action">动作</FieldLabel><select id="governance-action" className="h-8 rounded-md border bg-background px-2 text-sm" value={action} onChange={(event) => setAction(event.target.value as GovernanceAction)}>{Object.entries(actionLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field><Field><FieldLabel htmlFor="governance-type">目标类型</FieldLabel><Input id="governance-type" name="governance-type" autoComplete="off" placeholder="重分类时填写…" value={targetType} onChange={(event) => setTargetType(event.target.value)} /></Field></div><Field><FieldLabel htmlFor="governance-aliases">别名</FieldLabel><Input id="governance-aliases" name="governance-aliases" autoComplete="off" placeholder="多个别名用逗号分隔…" value={aliases} onChange={(event) => setAliases(event.target.value)} /></Field><Field><FieldLabel htmlFor="governance-reason">理由 / 证据说明</FieldLabel><Textarea id="governance-reason" name="governance-reason" autoComplete="off" maxLength={1000} placeholder="说明为什么要调整这个标签…" value={reason} onChange={(event) => setReason(event.target.value)} /><FieldDescription>理由会随建议一起保存并留痕。</FieldDescription></Field><Button type="button" disabled={busy || !canCreate} onClick={() => void createSuggestion()}>{busy ? <Loader2Icon className="animate-spin" /> : null}创建待审建议</Button></div>
          <div className="flex min-w-0 flex-col gap-2"><span className="text-sm font-medium">当前群的标签</span><p className="text-xs text-muted-foreground">合并需要选择至少两个标签，并指定目标；其他动作选择一个。</p><div className="max-h-64 overflow-auto rounded-md border p-2">{loading ? <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground"><Loader2Icon className="animate-spin" />读取当前群标签…</div> : tags.length ? tags.map((tag) => <label key={tag.ref} className="flex min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted"><input type="checkbox" checked={selectedRefs.includes(tag.ref)} onChange={() => toggleTag(tag.ref)} /><span className="min-w-0 flex-1 truncate">{tag.name}</span><Badge variant={tag.status === 'active' ? 'outline' : 'secondary'}>{tag.type}</Badge>{action === 'merge' ? <input aria-label={`选择 ${tag.name} 为合并目标`} type="radio" name="merge-target" checked={targetRef === tag.ref} onChange={() => setTargetRef(tag.ref)} /> : null}</label>) : <p className="p-3 text-xs text-muted-foreground">当前群没有标签。</p>}</div><p className="text-xs text-muted-foreground">已选 {selectedTags.length} 个；只操作当前群的标签。</p></div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-medium">待审建议</h3><p className="text-xs text-muted-foreground" aria-live="polite">{suggestions.length} 条待审建议 · {Object.keys(previews).length} 条已预检</p></div><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" disabled={busy || !suggestions.length} onClick={() => void previewAll()}><EyeIcon data-icon="inline-start" />预检当前页</Button><Button type="button" size="sm" disabled={busy || !suggestions.length} onClick={() => void resolveAll('approve')}><CheckIcon data-icon="inline-start" />批量批准</Button><Button type="button" size="sm" variant="outline" disabled={busy || !suggestions.length} onClick={() => void resolveAll('reject')}>批量拒绝</Button><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void load()}><RefreshCwIcon data-icon="inline-start" />刷新</Button></div></div>
        {suggestions.length ? <div className="grid gap-3">{suggestions.map((item) => { const itemPreview = previews[item.ref]; return <article key={item.ref} className="flex min-w-0 flex-col gap-3 rounded-lg border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-2"><Badge variant={item.action === 'deactivate' ? 'destructive' : 'secondary'}>{actionLabels[item.action]}</Badge><span className="font-mono text-xs text-muted-foreground">版本 {item.revision}</span></div><Badge variant="outline">{item.status}</Badge></div><p className="break-words text-sm">{item.reason}</p><div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-3"><span>影响记忆：{itemPreview ? itemPreview.preview.impact.memory_count : '未预检'}</span><span>关系边：{itemPreview ? itemPreview.preview.impact.relation_count : '未预检'}</span><span>目标标签：{item.target_name || item.target_tag_id || '当前选择'}</span><span className="break-words">相关标签：{itemPreview?.preview.impact.related_tags?.join('、') || '未预检'}</span><span>索引：{itemPreview?.preview.impact.index_refresh === 'outbox_pending' ? '待索引刷新' : '未预检'}</span></div><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void preview(item)}><EyeIcon data-icon="inline-start" />预检</Button><Button type="button" size="sm" disabled={busy || !itemPreview} onClick={() => void resolve(item, 'approve')}><CheckIcon data-icon="inline-start" />批准并应用</Button><Button type="button" size="sm" variant="outline" disabled={busy || !itemPreview} onClick={() => void resolve(item, 'reject')}>拒绝</Button></div></article> })}</div> : <p className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">当前群没有待审建议。</p>}
        {approvedSuggestions.length ? <div className="flex flex-col gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-4"><div><h3 className="font-medium">已批准治理操作</h3><p className="text-xs text-muted-foreground">补偿会创建新的审计记录；不会删除原批准记录。服务端会在状态变化时拒绝部分恢复。</p></div>{approvedSuggestions.map((item) => <article key={`approved:${item.ref}`} className="flex flex-wrap items-center justify-between gap-3 rounded-md border bg-background p-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Badge variant="secondary">{actionLabels[item.action]}</Badge><Badge variant="outline">已批准</Badge><span className="font-mono text-xs text-muted-foreground">版本 {item.revision}</span></div><p className="mt-1 break-words text-sm">{item.reason}</p></div><Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void compensate(item)}>申请补偿</Button></article>)}</div> : null}
      </> : null}
    </CardContent>
  </Card>
}

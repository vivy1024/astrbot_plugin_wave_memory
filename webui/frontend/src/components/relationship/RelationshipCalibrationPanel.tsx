import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'

import { listMemories, type MemoryItem } from '@/api/memories'
import { calibrateRelationship, type PeopleQuery, type RelationshipItem } from '@/api/people'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

const DIMENSIONS = [
  ['familiarity', '熟悉度'],
  ['trust', '信任'],
  ['fun', '趣味'],
  ['hostility', '敌意'],
  ['depth', '深度'],
] as const
const ACTIONS = [
  ['adjust', '相对调整'],
  ['override', '绝对覆盖'],
  ['clear_override', '取消覆盖'],
  ['restore_auto', '恢复自动推断'],
] as const
const QUICK_ADJUSTMENTS = [5, -5] as const

function displayValue(value: number | null | undefined, fallback = '—'): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback
}

function senderFromQuery(query: PeopleQuery, subjectPrincipalId: string): string {
  if (query.user_id?.trim()) return query.user_id.trim()
  const prefix = ':user:'
  const index = subjectPrincipalId.lastIndexOf(prefix)
  if (index >= 0) return subjectPrincipalId.slice(index + prefix.length)
  return ''
}

type RelationshipCalibrationTarget = Pick<RelationshipItem, 'subject_principal_id' | 'revision' | 'values' | 'object_ref' | 'calibration'>

export function RelationshipCalibrationPanel({ item, query, onChanged }: { item: RelationshipCalibrationTarget; query: PeopleQuery; onChanged?: () => void }) {
  const [action, setAction] = useState<(typeof ACTIONS)[number][0]>('adjust')
  const [dimension, setDimension] = useState<(typeof DIMENSIONS)[number][0]>('trust')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [evidence, setEvidence] = useState('')
  const [evidenceMemories, setEvidenceMemories] = useState<MemoryItem[]>([])
  const [evidenceSearch, setEvidenceSearch] = useState('')
  const [selectedEvidenceId, setSelectedEvidenceId] = useState('')
  const [busy, setBusy] = useState(false)
  const [evidenceError, setEvidenceError] = useState<string | null>(null)
  const values = item.values ?? {}
  const available = Boolean(item.object_ref?.ref && item.revision !== null && item.calibration.available)
  const senderId = senderFromQuery(query, item.subject_principal_id)

  useEffect(() => {
    if (!available) {
      setEvidenceMemories([])
      setSelectedEvidenceId('')
      setEvidenceError(null)
      return
    }
    let active = true
    setEvidenceError(null)
    void listMemories({
      bot_id: query.bot_id,
      session_id: query.session_id,
      visibility: query.visibility,
      sender: senderId || undefined,
      search: evidenceSearch.trim() || undefined,
      limit: 50,
      offset: 0,
    })
      .then((payload) => { if (active) setEvidenceMemories(payload.items) })
      .catch((reason: unknown) => {
        if (!active) return
        setEvidenceMemories([])
        setEvidenceError(reason instanceof Error ? reason.message : '当前群友记忆读取失败')
      })
    return () => { active = false }
  }, [available, evidenceSearch, query.bot_id, query.session_id, query.visibility, senderId])

  const visibleMemories = useMemo(() => {
    const keyword = evidenceSearch.trim().toLowerCase()
    if (!keyword) return evidenceMemories
    return evidenceMemories.filter((memory) => {
      return String(memory.id).includes(keyword) || (memory.content || '').toLowerCase().includes(keyword)
    })
  }, [evidenceMemories, evidenceSearch])

  function applyQuickAmount(nextAction: 'adjust' | 'override', value: number) {
    setAction(nextAction)
    setAmount(String(value))
  }

  async function submit() {
    if (!available || !item.object_ref || item.revision === null) return
    if (!reason.trim()) { toast.warning('请填写人工校准理由'); return }
    if (!selectedEvidenceId) { toast.warning('请选择当前群友在本群里的真实群聊记忆'); return }
    const numeric = amount.trim() ? Number(amount) : undefined
    if ((action === 'adjust' || action === 'override') && (numeric === undefined || !Number.isFinite(numeric))) {
      toast.warning('请输入有效的关系数值')
      return
    }
    setBusy(true)
    try {
      const [platformId, , ...conversationParts] = query.session_id.split(':')
      const evidencePayload = [{
        kind: 'memory',
        id: selectedEvidenceId,
        summary: evidence.trim() || undefined,
        source_scope: { bot_id: query.bot_id, visibility: query.visibility, session: { id: query.session_id, platform_id: platformId, kind: 'group', conversation_id: conversationParts.join(':') }, subject_principal_id: item.subject_principal_id },
      }]
      await calibrateRelationship(query, {
        object_ref: item.object_ref.ref,
        revision: item.revision,
        action,
        dimension,
        ...(action === 'adjust' ? { delta: numeric } : action === 'override' ? { value: numeric } : {}),
        reason: reason.trim(),
        evidence: evidencePayload,
      })
      toast.success('关系人工校准已提交并记录审计')
      setReason('')
      setEvidence('')
      setSelectedEvidenceId('')
      setAmount('')
      onChanged?.()
    } catch (failure) {
      toast.error(failure instanceof Error ? failure.message : '关系人工校准失败')
    } finally {
      setBusy(false)
    }
  }

  return <Card className="border-pink-500/20">
    <CardHeader className="pb-3"><CardTitle className="text-sm">人工关系校准</CardTitle><CardDescription>自动学习的值还会继续变；你改的部分默认不衰减。提交需要理由和一条真实记忆。</CardDescription></CardHeader>
    <CardContent className="flex flex-col gap-4">
      <div className="grid gap-2 sm:grid-cols-2">
        {DIMENSIONS.map(([key, label]) => {
          const dimData = values[key]
          const isInitialized = dimData && (typeof dimData.automatic_value === 'number' || typeof dimData.effective_value === 'number')
          return (
            <div key={key} className={`rounded-md border p-2.5 text-xs transition-colors ${isInitialized ? 'bg-muted/15 border-border' : 'bg-muted/5 border-dashed border-muted'}`}>
              <div className="flex items-center justify-between font-medium">
                <span>{label}</span>
                {isInitialized ? (
                  <span className="font-mono font-semibold text-foreground text-sm">
                    {displayValue(dimData?.effective_value, '0')}
                  </span>
                ) : (
                  <span className="text-[10px] text-muted-foreground font-normal">未建立</span>
                )}
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 text-muted-foreground">
                <dt>自动学习</dt>
                <dd className="text-right font-mono">{displayValue(dimData?.automatic_value, isInitialized ? '0' : '—')}</dd>
                <dt>人工调整</dt>
                <dd className="text-right font-mono">{dimData?.manual_adjustment ? (dimData.manual_adjustment > 0 ? `+${dimData.manual_adjustment}` : String(dimData.manual_adjustment)) : '—'}</dd>
                <dt>人工覆盖</dt>
                <dd className="text-right font-mono">{displayValue(dimData?.manual_override, '—')}</dd>
                <dt className="font-medium text-foreground">最终生效</dt>
                <dd className="text-right font-mono font-semibold text-foreground">{displayValue(dimData?.effective_value, isInitialized ? '0' : '—')}</dd>
              </dl>
            </div>
          )
        })}
      </div>
      {!available ? <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">当前人物在本群还没有正式关系记录，不能改好感。</p> : <>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field>
            <FieldLabel htmlFor="relationship-action">动作</FieldLabel>
            <select id="relationship-action" className="h-8 rounded-md border bg-background px-2 text-sm" value={action} disabled={busy} onChange={(event) => setAction(event.target.value as typeof action)}>{ACTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
          </Field>
          <Field>
            <FieldLabel htmlFor="relationship-dimension">关系维度</FieldLabel>
            <select id="relationship-dimension" className="h-8 rounded-md border bg-background px-2 text-sm" value={dimension} disabled={busy} onChange={(event) => setDimension(event.target.value as typeof dimension)}>{DIMENSIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
          </Field>
        </div>
        {action === 'adjust' || action === 'override' ? (
          <Field>
            <FieldLabel htmlFor="relationship-amount">数值</FieldLabel>
            <Input id="relationship-amount" inputMode="decimal" value={amount} disabled={busy} onChange={(event) => setAmount(event.target.value)} placeholder={action === 'adjust' ? '相对变化，例如 2 或 -1' : '绝对值，必须在该维度范围内'} />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {QUICK_ADJUSTMENTS.map((value) => (
                <Button key={value} type="button" size="sm" variant="outline" className="h-7 px-2 text-xs" disabled={busy} onClick={() => applyQuickAmount('adjust', value)}>
                  {value > 0 ? `+${value}` : String(value)}
                </Button>
              ))}
              <Button type="button" size="sm" variant="outline" className="h-7 px-2 text-xs" disabled={busy} onClick={() => applyQuickAmount('override', 0)}>
                该维归零
              </Button>
            </div>
            <FieldDescription>快捷按钮只填入数值，仍需理由和一条记忆才能提交。</FieldDescription>
          </Field>
        ) : null}
        <Field><FieldLabel htmlFor="relationship-reason">理由</FieldLabel><Textarea id="relationship-reason" maxLength={1000} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} placeholder="说明为什么需要人工校准…" /></Field>
        <Field>
          <FieldLabel htmlFor="relationship-evidence-search">筛选当前群友记忆</FieldLabel>
          <Input id="relationship-evidence-search" value={evidenceSearch} disabled={busy} onChange={(event) => setEvidenceSearch(event.target.value)} placeholder="按记忆 ID 或内容筛选…" />
          <FieldLabel htmlFor="relationship-evidence-memory" className="mt-2">记忆证据</FieldLabel>
          <select id="relationship-evidence-memory" className="h-8 rounded-md border bg-background px-2 text-sm" value={selectedEvidenceId} disabled={busy} onChange={(event) => setSelectedEvidenceId(event.target.value)}>
            <option value="">{visibleMemories.length ? '请选择当前群友的一条记忆…' : '当前群友在本群没有可引用记忆'}</option>
            {visibleMemories.map((memory) => <option key={memory.id} value={String(memory.id)}>{memory.id} · {(memory.content || '').slice(0, 72)}</option>)}
          </select>
          <FieldDescription>
            {evidenceError ?? (senderId ? `只列出这个人发过的记忆，提交时会再核对是否属于本群。` : '无法确认发送者，已回退为当前群的记忆列表。')}
          </FieldDescription>
        </Field>
        <Field><FieldLabel htmlFor="relationship-evidence">补充证据说明（可选）</FieldLabel><Textarea id="relationship-evidence" value={evidence} disabled={busy} onChange={(event) => setEvidence(event.target.value)} placeholder="补充说明这条真实消息为什么支持校准…" /></Field>
        <Button type="button" disabled={busy} onClick={() => void submit()}>{busy ? '提交中…' : '提交人工校准'}</Button>
      </>}
    </CardContent>
  </Card>
}

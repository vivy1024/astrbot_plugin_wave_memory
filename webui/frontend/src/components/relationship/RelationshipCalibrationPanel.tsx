import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import { listMemories, type MemoryItem, type MemoriesResponse } from '@/api/memories'
import { calibrateRelationship, type PeopleQuery, type RelationshipItem } from '@/api/people'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { formatDisplayNumber, formatSignedDisplayNumber } from '@/lib/format-number'
import { humanizeApiError } from '@/lib/reason-label'

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
  return formatDisplayNumber(value, fallback)
}

function senderFromQuery(query: PeopleQuery, subjectPrincipalId: string): string {
  if (query.user_id?.trim()) return query.user_id.trim()
  const prefix = ':user:'
  const index = subjectPrincipalId.lastIndexOf(prefix)
  if (index >= 0) return subjectPrincipalId.slice(index + prefix.length)
  return ''
}

type RelationshipCalibrationTarget = Pick<RelationshipItem, 'subject_principal_id' | 'revision' | 'values' | 'object_ref' | 'calibration'>

const EVIDENCE_PAGE_SIZE = 100 as const

type EvidencePage = MemoriesResponse['page']

function apiErrorCode(reason: unknown): string | null {
  if (!(reason instanceof Error) || !('payload' in reason)) return null
  const payload = (reason as Error & { payload?: unknown }).payload
  if (typeof payload !== 'object' || payload === null || !('error' in payload)) return null
  const error = (payload as { error?: unknown }).error
  if (typeof error !== 'object' || error === null || !('code' in error)) return null
  const code = (error as { code?: unknown }).code
  return typeof code === 'string' && code ? code : null
}

function calibrationFailureMessage(reason: unknown): string {
  const code = apiErrorCode(reason)
  const labels: Record<string, string> = {
    relationship_evidence_required: '没有提交证据，请重新选择一条当前群记忆',
    relationship_evidence_scope_mismatch: '证据不属于当前 Bot、当前群或当前关系对象',
    relationship_evidence_not_found: '所选记忆已不存在或不属于当前作用域',
    relationship_evidence_unavailable: '所选记忆尚未成为可用正式记忆',
    relationship_evidence_quarantined: '所选记忆已被隔离，不能作为校准证据',
    relationship_evidence_hash_mismatch: '所选记忆内容已变化，请重新选择最新证据',
    relationship_evidence_invalid: '证据格式不符合服务端契约',
  }
  if (code) return `${labels[code] ?? '关系人工校准失败'}（${code}）`
  return humanizeApiError(reason, '关系人工校准失败')
}

export function RelationshipCalibrationPanel({ item, query, onChanged }: { item: RelationshipCalibrationTarget; query: PeopleQuery; onChanged?: () => void }) {
  const [action, setAction] = useState<(typeof ACTIONS)[number][0]>('adjust')
  const [dimension, setDimension] = useState<(typeof DIMENSIONS)[number][0]>('trust')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [evidence, setEvidence] = useState('')
  const [evidenceMemories, setEvidenceMemories] = useState<MemoryItem[]>([])
  const [evidenceSearch, setEvidenceSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [selectedEvidenceId, setSelectedEvidenceId] = useState('')
  const [evidencePage, setEvidencePage] = useState<EvidencePage | null>(null)
  const [evidenceLoading, setEvidenceLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [evidenceError, setEvidenceError] = useState<string | null>(null)
  const evidenceRequestRef = useRef(0)
  const values = item.values ?? {}
  const available = Boolean(item.object_ref?.ref && item.revision !== null && item.calibration.available)
  const senderId = senderFromQuery(query, item.subject_principal_id)

  const loadEvidencePage = useCallback(async (offset: number, append: boolean) => {
    const requestId = ++evidenceRequestRef.current
    setEvidenceLoading(true)
    setEvidenceError(null)
    try {
      const payload = await listMemories({
        bot_id: query.bot_id,
        session_id: query.session_id,
        visibility: query.visibility,
        sender_id: senderId || undefined,
        search: debouncedSearch || undefined,
        limit: EVIDENCE_PAGE_SIZE,
        offset,
      })
      if (requestId !== evidenceRequestRef.current) return
      setEvidencePage(payload.page)
      setEvidenceMemories((current) => {
        const combined = append ? [...current, ...payload.items] : payload.items
        const seen = new Set<number>()
        return combined.filter((memory) => {
          if (seen.has(memory.id)) return false
          seen.add(memory.id)
          return true
        })
      })
      // 方案 B：列表保持服务端时间倒序，首次加载自动预选最新一条，用户仍可改选。
      if (!append && payload.items.length > 0) {
        setSelectedEvidenceId(String(payload.items[0].id))
      }
    } catch (reason: unknown) {
      if (requestId !== evidenceRequestRef.current) return
      setEvidenceError(humanizeApiError(reason, '当前群友记忆读取失败'))
      if (!append) setEvidenceMemories([])
    } finally {
      if (requestId === evidenceRequestRef.current) setEvidenceLoading(false)
    }
  }, [debouncedSearch, query.bot_id, query.session_id, query.visibility, senderId])

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedSearch(evidenceSearch.trim()), 300)
    return () => window.clearTimeout(handle)
  }, [evidenceSearch])

  useEffect(() => {
    evidenceRequestRef.current += 1
    setEvidencePage(null)
    setEvidenceMemories([])
    setSelectedEvidenceId('')
    setEvidenceError(null)
    if (!available) return
    void loadEvidencePage(0, false)
  }, [available, debouncedSearch, loadEvidencePage])

  const visibleMemories = evidenceMemories
  const evidenceHasMore = evidencePage?.has_more === true
  const evidenceTotal = evidencePage?.total_status === 'exact' ? evidencePage.total : null

  async function loadMoreEvidence() {
    if (evidenceLoading || !evidenceHasMore) return
    const nextOffset = (evidencePage?.offset ?? 0) + (evidencePage?.limit ?? EVIDENCE_PAGE_SIZE)
    await loadEvidencePage(nextOffset, true)
  }

  function applyQuickAmount(nextAction: 'adjust' | 'override', value: number) {
    setAction(nextAction)
    setAmount(String(value))
  }

  async function submit() {
    if (!available || !item.object_ref || item.revision === null) return
    if (!reason.trim()) { toast.warning('请填写人工校准理由'); return }
    if (!selectedEvidenceId) { toast.warning('请选择当前群友在本群里的真实群聊记忆'); return }
    const selectedMemory = evidenceMemories.find((memory) => String(memory.id) === selectedEvidenceId)
    if (!selectedMemory) {
      setSelectedEvidenceId('')
      toast.warning('所选证据已不在当前作用域，请重新选择最新记忆')
      return
    }
    const numeric = amount.trim() ? Number(amount) : undefined
    if ((action === 'adjust' || action === 'override') && (numeric === undefined || !Number.isFinite(numeric))) {
      toast.warning('请输入有效的关系数值')
      return
    }
    const [platformId, sessionKind, ...conversationParts] = query.session_id.split(':')
    if (!platformId || sessionKind !== query.visibility || !conversationParts.length) {
      toast.error('当前会话 Scope 无效，无法提交关系校准')
      return
    }
    const evidenceSummary = evidence.trim()
    setBusy(true)
    try {
      const evidencePayload = [{
        kind: 'memory',
        id: String(selectedMemory.id),
        ...(evidenceSummary ? { summary: evidenceSummary } : {}),
        source_scope: {
          bot_id: query.bot_id,
          visibility: query.visibility,
          session: {
            id: query.session_id,
            platform_id: platformId,
            kind: sessionKind,
            conversation_id: conversationParts.join(':'),
          },
          subject_principal_id: item.subject_principal_id,
        },
      }]
      if (!evidencePayload.length) {
        throw new Error('relationship_evidence_required')
      }
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
      toast.error(calibrationFailureMessage(failure))
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
                <dd className="text-right font-mono">{typeof dimData?.manual_adjustment === 'number' && Number.isFinite(dimData.manual_adjustment) && dimData.manual_adjustment !== 0 ? formatSignedDisplayNumber(dimData.manual_adjustment) : '—'}</dd>
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
          <select id="relationship-evidence-memory" className="h-8 rounded-md border bg-background px-2 text-sm" value={selectedEvidenceId} disabled={busy || evidenceLoading} onChange={(event) => setSelectedEvidenceId(event.target.value)}>
            <option value="">{evidenceLoading && !visibleMemories.length ? '正在加载当前群友记忆…' : visibleMemories.length ? '请选择当前群友的一条记忆…' : '当前群友在本群没有可引用记忆'}</option>
            {visibleMemories.map((memory) => <option key={memory.id} value={String(memory.id)}>{memory.id} · {(memory.content || '').slice(0, 72)}</option>)}
          </select>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>{evidenceTotal !== null ? `已加载 ${visibleMemories.length} / ${evidenceTotal} 条` : `已加载 ${visibleMemories.length} 条`}</span>
            {evidenceHasMore ? <Button type="button" size="sm" variant="outline" className="h-7 px-2 text-xs" disabled={busy || evidenceLoading} onClick={() => void loadMoreEvidence()}>{evidenceLoading ? '加载中…' : '加载更多'}</Button> : null}
          </div>
          <FieldDescription>
            {evidenceError ?? (senderId ? `只列出这个人发过的记忆，支持服务端搜索和分页；提交时会再核对是否属于本群。` : '无法确认发送者，已回退为当前群的记忆列表。')}
          </FieldDescription>
        </Field>
        <Field><FieldLabel htmlFor="relationship-evidence">补充证据说明（可选）</FieldLabel><Textarea id="relationship-evidence" value={evidence} disabled={busy} onChange={(event) => setEvidence(event.target.value)} placeholder="补充说明这条真实消息为什么支持校准…" /></Field>
        <Button type="button" disabled={busy} onClick={() => void submit()}>{busy ? '提交中…' : '提交人工校准'}</Button>
      </>}
    </CardContent>
  </Card>
}

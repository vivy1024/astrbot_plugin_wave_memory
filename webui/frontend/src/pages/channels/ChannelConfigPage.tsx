import { useEffect, useMemo, useState } from 'react'
import { humanizeApiError } from '@/lib/reason-label'
import { Link } from 'react-router-dom'
import { ArrowRightIcon, Loader2Icon, RotateCcwIcon, SaveIcon, ShieldCheckIcon, WandSparklesIcon } from 'lucide-react'
import { toast } from 'sonner'

import {
  applyChannelConfig,
  getChannelConfig,
  resetChannelConfigDefaults,
  safeValidation,
  validateChannelConfig,
  type ChannelConfigData,
  type ChannelDescriptor,
  type ChannelValidationPayload,
} from '@/api/channels'
import { useUnsavedChangesGuard } from '@/app/unsaved-changes'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { validateNumericDraft, type NumericConstraints } from '@/lib/numeric-draft'
import { ChannelConfigTable, type ChannelNumericField } from '@/pages/channels/ChannelConfigTable'
import { ChannelDiffCard } from '@/pages/channels/ChannelDiffCard'
import { channelPatchFingerprint, hasFreshChannelPreflight, serializeChannelPatch } from '@/pages/channels/channel-config-state'
const numericFields = ['priority', 'top_k', 'max_items', 'token_budget', 'timeout_ms', 'min_score'] as const
const rootNumericKey = 'root.recent_dedup_minutes'

const fieldHelp = [
  'enabled：是否参与注入',
  'priority：通道执行/拼接优先级',
  'top_k：检索类通道候选数',
  'max_items：非检索类通道最多注入条目',
  'token_budget：单通道预算',
  'timeout_ms：单通道超时',
  'min_score：检索命中最低分',
]

function numericDraftsFrom(data: ChannelConfigData): Record<string, string> {
  const values: Record<string, string> = {
    [rootNumericKey]: data.recent_dedup_minutes == null ? '' : String(data.recent_dedup_minutes),
  }
  Object.entries(data.channels ?? {}).forEach(([name, channel]) => {
    numericFields.forEach((field) => {
      const value = channel[field]
      values[`${name}.${field}`] = value == null ? '' : String(value)
    })
  })
  return values
}

function finiteLimit(limits: Record<string, number>, key: string): number | undefined {
  const value = limits[key]
  return Number.isFinite(value) ? value : undefined
}

export function ChannelConfigPage() {
  const [draft, setDraft] = useState<ChannelConfigData | null>(null)
  const [original, setOriginal] = useState<ChannelConfigData | null>(null)
  const [numericDrafts, setNumericDrafts] = useState<Record<string, string>>({})
  const [numericErrors, setNumericErrors] = useState<Record<string, string>>({})
  const [runtime, setRuntime] = useState<Record<string, unknown>>({})
  const [descriptors, setDescriptors] = useState<ChannelDescriptor[]>([])
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [revision, setRevision] = useState('')
  const [effectiveSince, setEffectiveSince] = useState<number | null>(null)
  const [verificationUrl, setVerificationUrl] = useState('/observatory')
  const [validation, setValidation] = useState<ChannelValidationPayload | null>(null)
  const [validatedFingerprint, setValidatedFingerprint] = useState<string | null>(null)
  const [resetDialogOpen, setResetDialogOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const currentPatch = useMemo(() => draft ? serializeChannelPatch(draft) : null, [draft])
  const hasNumericErrors = Object.keys(numericErrors).length > 0
  const isDirty = Boolean(
    draft
    && original
    && (channelPatchFingerprint(serializeChannelPatch(draft)) !== channelPatchFingerprint(serializeChannelPatch(original)) || hasNumericErrors),
  )
  useUnsavedChangesGuard(isDirty, '您当前有未保存的通道配置修改，离开后这些草稿将丢失。')

  function invalidateValidation() {
    setValidation(null)
    setValidatedFingerprint(null)
  }

  function updateDraft(next: ChannelConfigData) {
    invalidateValidation()
    setDraft(next)
  }

  function updateNumericError(key: string, message: string | null) {
    setNumericErrors((current) => {
      if (!message && !(key in current)) return current
      const next = { ...current }
      if (message) next[key] = message
      else delete next[key]
      return next
    })
  }

  function updateRootNumeric(raw: string) {
    const constraints: NumericConstraints = {
      label: '近期去重分钟',
      integer: true,
      min: finiteLimit(limits, 'recent_dedup_minutes_min'),
      max: finiteLimit(limits, 'recent_dedup_minutes_max'),
    }
    const result = validateNumericDraft(raw, constraints)
    setNumericDrafts((current) => ({ ...current, [rootNumericKey]: raw }))
    updateNumericError(rootNumericKey, result.error)
    invalidateValidation()
    if (result.value !== null) setDraft((current) => current ? { ...current, recent_dedup_minutes: result.value as number } : current)
  }

  function updateChannelNumeric(name: string, field: ChannelNumericField, raw: string, constraints: NumericConstraints) {
    const key = `${name}.${field}`
    const result = validateNumericDraft(raw, constraints)
    setNumericDrafts((current) => ({ ...current, [key]: raw }))
    updateNumericError(key, result.error)
    invalidateValidation()
    if (result.value === null) return
    setDraft((current) => current ? {
      ...current,
      channels: {
        ...(current.channels ?? {}),
        [name]: { ...(current.channels?.[name] ?? {}), [field]: result.value },
      },
    } : current)
  }

  function replaceEffectiveConfig(next: ChannelConfigData) {
    const cloned = JSON.parse(JSON.stringify(next)) as ChannelConfigData
    setDraft(next)
    setOriginal(cloned)
    setNumericDrafts(numericDraftsFrom(next))
    setNumericErrors({})
  }

  async function load() {
    setLoading(true)
    setError('')
    try {
      const payload = await getChannelConfig()
      if (!payload.current) throw new Error('服务端未返回当前有效通道配置')
      replaceEffectiveConfig(payload.current)
      setRuntime(payload.runtime ?? {})
      setDescriptors(payload.descriptors ?? [])
      setLimits(payload.limits ?? {})
      setRevision(payload.revision ?? '')
      setEffectiveSince(payload.effective_since ?? null)
      setVerificationUrl(payload.verification_url ?? '/observatory')
      invalidateValidation()
    } catch (err) {
      setError(humanizeApiError(err, '通道配置加载失败'))
    } finally {
      setLoading(false)
    }
  }

  async function preview() {
    if (!draft || !currentPatch) return
    if (hasNumericErrors) {
      toast.error('请先修正通道配置中的数值错误')
      return
    }
    const fingerprint = channelPatchFingerprint(currentPatch)
    setSaving(true)
    try {
      const result = safeValidation(await validateChannelConfig(currentPatch))
      setValidation(result)
      setValidatedFingerprint(result.ok && result.preflight_token ? fingerprint : null)
      if (result.ok) toast.success('通道配置校验通过')
      else toast.error(result.errors.join('；') || '通道配置校验失败')
    } catch (err) {
      const result = safeValidation({ ok: false, errors: [humanizeApiError(err, '通道配置校验失败')], diff: [] })
      setValidation(result)
      setValidatedFingerprint(null)
      toast.error(result.errors[0])
    } finally {
      setSaving(false)
    }
  }

  async function apply() {
    if (!draft || !currentPatch) return
    if (hasNumericErrors || !hasFreshChannelPreflight(validation, validatedFingerprint, currentPatch)) {
      invalidateValidation()
      toast.error('当前配置与最近一次校验预览不一致，请重新校验后再应用')
      return
    }
    setSaving(true)
    try {
      const result = safeValidation(await applyChannelConfig(currentPatch, validation?.preflight_token ?? ''))
      setValidation(result)
      setValidatedFingerprint(null)
      if (result.ok && result.operation?.status === 'succeeded' && result.effective) {
        replaceEffectiveConfig(result.effective)
        setRevision(String(result.revision ?? ''))
        setEffectiveSince(typeof result.effective_since === 'number' ? result.effective_since : null)
        setVerificationUrl(result.verification_url ?? '/observatory')
        toast.success(result.message ?? '通道配置已应用并完成运行时回读')
      } else {
        toast.error(result.errors.join('；') || '通道配置应用失败')
      }
    } catch (err) {
      const message = humanizeApiError(err, '通道配置应用失败')
      setValidation(safeValidation({ ok: false, errors: [message], diff: [] }))
      setValidatedFingerprint(null)
      toast.error(message)
    } finally {
      setSaving(false)
    }
  }

  async function resetDefaults() {
    setResetDialogOpen(false)
    setSaving(true)
    try {
      const result = safeValidation(await resetChannelConfigDefaults())
      setValidation(result)
      setValidatedFingerprint(null)
      if (result.ok && result.operation?.status === 'succeeded' && result.effective) {
        replaceEffectiveConfig(result.effective)
        setRevision(String(result.revision ?? ''))
        setEffectiveSince(typeof result.effective_since === 'number' ? result.effective_since : null)
        setVerificationUrl(result.verification_url ?? '/observatory')
        toast.success(result.message ?? '已恢复默认通道配置')
      } else {
        toast.error(result.errors.join('；') || '恢复默认失败')
      }
    } catch (err) {
      const message = humanizeApiError(err, '恢复默认失败')
      setValidation(safeValidation({ ok: false, errors: [message], diff: [] }))
      setValidatedFingerprint(null)
      toast.error(message)
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => { void load() }, [])

  if (loading) return <div className="flex flex-col gap-4"><Skeleton className="h-32 w-full" /><Skeleton className="h-96 w-full" /></div>
  if (error) return <Alert variant="destructive"><AlertTitle>通道配置加载失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>
  if (!draft || !currentPatch) return <p className="text-sm text-muted-foreground">暂无通道配置。</p>

  const validationShape = safeValidation(validation)
  const preflightFresh = hasFreshChannelPreflight(validation, validatedFingerprint, currentPatch)
  const rootError = numericErrors[rootNumericKey]

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>通道配置</CardTitle>
          <CardDescription>实时调整各记忆注入通道的启用状态、检索条数与延迟预算。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <FieldGroup className="grid gap-4 md:grid-cols-3">
            <Field>
              <FieldLabel htmlFor="runtime-mode">运行模式</FieldLabel>
              <div id="runtime-mode" className="flex min-h-10 flex-col justify-center rounded-md border px-3 py-2 text-sm">
                <span>{String(runtime.mode ?? draft.mode ?? 'unknown')}</span>
                <span className="text-muted-foreground">配置版本：{revision || '未记录'} · 生效时间：{effectiveSince ? new Date(effectiveSince * 1000).toLocaleString('zh-CN') : '未记录'}</span>
              </div>
            </Field>
            <Field>
              <FieldLabel htmlFor="dedup-minutes">近期去重分钟</FieldLabel>
              <Input
                id="dedup-minutes"
                aria-invalid={Boolean(rootError)}
                inputMode="numeric"
                min={finiteLimit(limits, 'recent_dedup_minutes_min')}
                max={finiteLimit(limits, 'recent_dedup_minutes_max')}
                step={1}
                type="number"
                value={numericDrafts[rootNumericKey] ?? ''}
                onChange={(event) => updateRootNumeric(event.target.value)}
              />
              {rootError ? <span className="text-xs text-destructive" role="alert">{rootError}</span> : null}
            </Field>
            <Field>
              <FieldLabel>注入记录开关</FieldLabel>
              <div className="flex h-10 items-center justify-between gap-3 rounded-md border px-3">
                <span className="text-sm text-muted-foreground">{draft.trace_enabled ? '已开启' : '已关闭'}</span>
                <Switch checked={Boolean(draft.trace_enabled)} onCheckedChange={(checked) => updateDraft({ ...draft, trace_enabled: checked })} />
              </div>
            </Field>
          </FieldGroup>
          <Alert><ShieldCheckIcon /><AlertTitle>运行提示</AlertTitle><AlertDescription>安全通道默认常驻开启，保障对话上下文安全与过滤机制正常运作。</AlertDescription></Alert>
          <div className="flex flex-wrap items-center gap-3">
            <Button disabled={saving || hasNumericErrors} onClick={() => void preview()}>{saving ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : <WandSparklesIcon data-icon="inline-start" />}校验预览</Button>
            <Button disabled={saving || !preflightFresh || hasNumericErrors} onClick={() => void apply()}>{saving ? <Loader2Icon className="animate-spin" data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}应用配置</Button>
            <Button disabled={saving} variant="outline" onClick={() => setResetDialogOpen(true)}><RotateCcwIcon data-icon="inline-start" />恢复默认</Button>
            {validation ? <Badge variant={validationShape.ok ? 'secondary' : 'destructive'}>{validationShape.ok ? '校验通过' : '校验失败'}</Badge> : null}
            {isDirty ? <Badge variant="secondary" className="animate-pulse"><WandSparklesIcon className="mr-1 size-3" />配置已修改（未保存）</Badge> : null}
            <Button asChild variant="outline"><Link to={verificationUrl}>去注入观测台验证最近 trace<ArrowRightIcon data-icon="inline-end" /></Link></Button>
          </div>
        </CardContent>
      </Card>

      <details className="rounded-xl border border-border/70 bg-muted/10 px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">参数说明</summary>
        <ul className="mt-3 grid gap-x-8 gap-y-2 text-muted-foreground md:grid-cols-2 xl:grid-cols-3">{fieldHelp.map((item) => <li key={item}>{item}</li>)}</ul>
      </details>

      <ChannelConfigTable draft={draft} descriptors={descriptors} limits={limits} numericDrafts={numericDrafts} numericErrors={numericErrors} onDraftChange={updateDraft} onNumericChange={updateChannelNumeric} />
      <ChannelDiffCard diff={validationShape.diff} validation={validation} />

      <Dialog open={resetDialogOpen} onOpenChange={setResetDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>恢复默认通道配置？</DialogTitle>
            <DialogDescription>将所有注入通道的条数、预算与优先级重置为出厂推荐值。</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setResetDialogOpen(false)}>取消</Button>
            <Button type="button" variant="destructive" onClick={() => void resetDefaults()}>确认恢复默认</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

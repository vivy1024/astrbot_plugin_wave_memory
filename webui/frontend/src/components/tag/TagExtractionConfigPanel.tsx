import { useEffect, useState } from 'react'
import { AlertCircleIcon, Loader2Icon, SaveIcon } from 'lucide-react'
import { toast } from 'sonner'

import { getFullConfig, saveFullConfig, type WaveConfigPayload } from '@/api/config'
import {
  clampTagBatchSize,
  defaultTagExecutionOptions,
  tagWritePolicyLabels,
  type TagExecutionOptions,
  type TagWritePolicy,
} from '@/api/tags'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface TagExtractionConfigPanelProps {
  title?: string
  description?: string
  config?: WaveConfigPayload | null
  onConfigChange?: (config: WaveConfigPayload) => void
  options?: TagExecutionOptions
  onOptionsChange?: (options: Required<TagExecutionOptions>) => void
  disabled?: boolean
  showExtractToggle?: boolean
  showSkipShort?: boolean
}

function isTagWritePolicy(value: unknown): value is TagWritePolicy {
  return value === 'missing_only' || value === 'append' || value === 'replace'
}

function readTags(config: WaveConfigPayload | null): Record<string, unknown> {
  return config?.tags && typeof config.tags === 'object' ? config.tags : {}
}

function policyFromConfig(config: WaveConfigPayload | null): TagWritePolicy {
  const policy = readTags(config).tag_write_policy
  return isTagWritePolicy(policy) ? policy : defaultTagExecutionOptions.tag_write_policy
}

export function TagExtractionConfigPanel({
  title = '基础标签提取配置',
  description = '三处标签提取入口共用同一套模型、向量维度和默认执行参数。',
  config,
  onConfigChange,
  options,
  onOptionsChange,
  disabled = false,
  showExtractToggle = false,
  showSkipShort = false,
}: TagExtractionConfigPanelProps) {
  const [localConfig, setLocalConfig] = useState<WaveConfigPayload | null>(config ?? null)
  const [loading, setLoading] = useState(config === undefined)
  const [saving, setSaving] = useState(false)
  const [extractTags, setExtractTags] = useState(options?.extract_tags ?? defaultTagExecutionOptions.extract_tags)
  const [tagBatchSize, setTagBatchSize] = useState(options?.tag_batch_size ?? defaultTagExecutionOptions.tag_batch_size)
  const [tagWritePolicy, setTagWritePolicy] = useState<TagWritePolicy>(options?.tag_write_policy ?? defaultTagExecutionOptions.tag_write_policy)
  const [skipShortMinLength, setSkipShortMinLength] = useState(options?.skip_short_min_length ?? defaultTagExecutionOptions.skip_short_min_length)

  function applyLoadedConfig(nextConfig: WaveConfigPayload | null) {
    setLocalConfig(nextConfig)
    if (nextConfig && options?.tag_batch_size === undefined) {
      setTagBatchSize(clampTagBatchSize(readTags(nextConfig).tag_batch_size))
    }
    if (nextConfig && options?.tag_write_policy === undefined) {
      setTagWritePolicy(policyFromConfig(nextConfig))
    }
  }

  useEffect(() => {
    if (config !== undefined) {
      applyLoadedConfig(config)
      setLoading(false)
      return
    }

    let alive = true
    setLoading(true)
    getFullConfig()
      .then((payload) => {
        if (!alive) return
        applyLoadedConfig(payload ?? null)
      })
      .catch((err) => {
        if (!alive) return
        toast.error(err instanceof Error ? err.message : '标签配置加载失败')
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [config])

  useEffect(() => {
    if (!options) return
    if (options.extract_tags !== undefined) setExtractTags(options.extract_tags)
    if (options.tag_batch_size !== undefined) setTagBatchSize(clampTagBatchSize(options.tag_batch_size))
    if (isTagWritePolicy(options.tag_write_policy)) setTagWritePolicy(options.tag_write_policy)
    if (options.skip_short_min_length !== undefined) {
      setSkipShortMinLength(Math.max(0, Math.round(Number(options.skip_short_min_length) || 0)))
    }
  }, [options])

  useEffect(() => {
    onOptionsChange?.({
      extract_tags: extractTags,
      tag_batch_size: tagBatchSize,
      tag_write_policy: tagWritePolicy,
      skip_short_min_length: skipShortMinLength,
    })
  }, [extractTags, tagBatchSize, tagWritePolicy, skipShortMinLength, onOptionsChange])

  function updateConfig(patch: Partial<WaveConfigPayload>) {
    const next = { ...(localConfig ?? {}), ...patch }
    setLocalConfig(next)
    onConfigChange?.(next)
  }

  async function handleSave() {
    if (!localConfig) return
    setSaving(true)
    try {
      await saveFullConfig({
        embedding_provider_id: localConfig.embedding_provider_id,
        embedding_dimension: localConfig.embedding_dimension,
        tag_llm_provider_id: localConfig.tag_llm_provider_id,
        tags: {
          ...readTags(localConfig),
          tag_batch_size: tagBatchSize,
          tag_write_policy: tagWritePolicy,
        },
      })
      toast.success('标签提取配置保存成功；模型与向量维度需要重启后生效')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '标签提取配置保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="border-border/60">
      <CardHeader>
        <CardTitle className="text-sm font-semibold">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Alert>
          <AlertCircleIcon />
          <AlertTitle>Provider / 向量维度类配置需要重启</AlertTitle>
          <AlertDescription>
            当前面板读取 /api/config，并通过 /api/config/full 保存；Embedding Provider、embedding_dimension 与 tag_llm_provider_id 保存后需要重启 AstrBot 生效。
          </AlertDescription>
        </Alert>

        <FieldGroup className="grid gap-4 sm:grid-cols-3">
          <Field>
            <FieldLabel>Embedding Provider</FieldLabel>
            <Input
              value={String(localConfig?.embedding_provider_id ?? '')}
              disabled={disabled || loading || saving}
              placeholder="未配置"
              onChange={(event) => updateConfig({ embedding_provider_id: event.target.value })}
            />
          </Field>
          <Field>
            <FieldLabel>向量输出维度</FieldLabel>
            <Input
              type="number"
              value={Number(localConfig?.embedding_dimension ?? 1024)}
              disabled={disabled || loading || saving}
              onChange={(event) => updateConfig({ embedding_dimension: Number(event.target.value) || 1024 })}
            />
          </Field>
          <Field>
            <FieldLabel>标签提取分析模型</FieldLabel>
            <Input
              value={String(localConfig?.tag_llm_provider_id ?? '')}
              disabled={disabled || loading || saving}
              placeholder="未配置"
              onChange={(event) => updateConfig({ tag_llm_provider_id: event.target.value })}
            />
          </Field>
        </FieldGroup>

        <FieldGroup className="grid gap-4 sm:grid-cols-3">
          {showExtractToggle ? (
            <Field className="rounded-lg border bg-muted/10 px-3 py-2">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input
                  type="checkbox"
                  checked={extractTags}
                  disabled={disabled || saving}
                  onChange={(event) => setExtractTags(event.target.checked)}
                />
                <span>执行 extract_tags</span>
              </label>
              <FieldDescription>关闭后只保存/导入数据，不调用标签提取模型。</FieldDescription>
            </Field>
          ) : null}
          <Field>
            <FieldLabel>默认 tag_batch_size</FieldLabel>
            <Input
              type="number"
              min={1}
              max={50}
              value={tagBatchSize}
              disabled={disabled || saving}
              onChange={(event) => setTagBatchSize(clampTagBatchSize(event.target.value))}
            />
            <FieldDescription>范围 1-50，三处执行入口统一使用。</FieldDescription>
          </Field>
          <Field>
            <FieldLabel>默认 tag_write_policy</FieldLabel>
            <Select value={tagWritePolicy} onValueChange={(value) => setTagWritePolicy(isTagWritePolicy(value) ? value : 'missing_only')} disabled={disabled || saving}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="missing_only">{tagWritePolicyLabels.missing_only}</SelectItem>
                <SelectItem value="append">{tagWritePolicyLabels.append}</SelectItem>
                <SelectItem value="replace">{tagWritePolicyLabels.replace}</SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription>维护中心全库补跑默认只允许 missing_only。</FieldDescription>
          </Field>
          {showSkipShort ? (
            <Field>
              <FieldLabel>skip_short_min_length</FieldLabel>
              <Input
                type="number"
                min={0}
                max={200}
                value={skipShortMinLength}
                disabled={disabled || saving}
                onChange={(event) => setSkipShortMinLength(Math.max(0, Math.round(Number(event.target.value) || 0)))}
              />
              <FieldDescription>短文本跳过阈值，默认 10。</FieldDescription>
            </Field>
          ) : null}
        </FieldGroup>
      </CardContent>
      <CardFooter className="justify-end">
        <Button type="button" disabled={disabled || loading || saving || !localConfig} onClick={() => void handleSave()}>
          {saving ? <Loader2Icon data-icon="inline-start" className="animate-spin" /> : <SaveIcon data-icon="inline-start" />}
          保存标签配置
        </Button>
      </CardFooter>
    </Card>
  )
}

export default TagExtractionConfigPanel

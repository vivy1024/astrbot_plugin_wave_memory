import { useMemo, useState } from 'react'
import {
  BotIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  CircleDashedIcon,
  InfoIcon,
  SparklesIcon,
  SlidersHorizontalIcon,
} from 'lucide-react'
import { toast } from 'sonner'

import { saveFullConfig, type FullConfigSavePayload } from '@/api/config'
import { useConfirmAction } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { displayValue, sameValue, type ConfigEntry } from '@/pages/config/config-catalog'
import { locateConfig, type ConfigLocation } from '@/pages/config/config-location'

export const ONBOARDING_COLLAPSED_KEY = 'wavememory.config.onboarding_collapsed'

/**
 * 新手精简视图保留的核心配置项集合（约二十余项）：
 * 聚焦模型、回复、心智、记忆召回、常用学习开关与 WebUI 安全，
 * 自动隐藏 hot:spike.* / hot:geodesic.* / hot:residual.* / Memory_Index_Settings.* /
 * Memory_Budget_Settings.* / Performance_Settings.* / Trace_Settings.* 等底层内核与高阶调优参数。
 */
export const BEGINNER_CORE_KEYS: ReadonlySet<string> = new Set([
  // 模型与向量基础
  'embedding_provider_id',
  'tag_llm_provider_id',
  'llm_fallback_provider_ids',
  'MetaThinking_Settings.default_model',
  // 记忆召回核心
  'Query_Settings.enable_auto_inject',
  'Query_Settings.inject_top_k',
  'Query_Settings.min_similarity',
  'Cross_Group_Settings.cross_group_enabled',
  // 回复与人格核心
  'MetaThinking_Settings.enabled',
  'Inject_Settings.skip_recent_minutes',
  'Jargon_Settings.enabled',
  'FewShot_Settings.enabled',
  // 心智与社交核心
  'Lifecycle_Settings.enable_mood',
  'Social_Settings.abuse_trigger_count',
  // 标签与常用学习开关
  'Tag_Settings.tag_extraction_enabled',
  'Learning_Settings.post_reply_bookkeeping_enabled',
  'Learning_Settings.daily_diary_enabled',
  'Learning_Settings.auto_approve_enabled',
  'Learning_Settings.expression_learning_enabled',
  'Learning_Settings.expression_auto_approve_enabled',
  'Learning_Settings.reply_feedback_enabled',
  'Learning_Settings.relationship_portrait_enabled',
  'Learning_Settings.belief_reflection_enabled',
  'Study_Settings.self_reflect_enabled',
  // WebUI 安全与基础备份
  'WebUI_Settings.webui_host',
  'WebUI_Settings.webui_port',
  'WebUI_Settings.webui_password',
  'Runtime_Settings.runtime_mode',
  'backup_max_count',
])

export interface OnboardingPresetsPanelProps {
  entries: ConfigEntry[]
  onLocate: (location: ConfigLocation) => void
  onApplied: () => Promise<void> | void
}

interface PresetFieldSpec {
  key: string
  label: string
  value: boolean
}

interface ScenarioPreset {
  id: 'light' | 'companion' | 'digital_life'
  badge: string
  recommended?: boolean
  title: string
  shortTitle: string
  summary: string
  highlights: string[]
  botFollowupTip?: string
  fields: PresetFieldSpec[]
  payload: FullConfigSavePayload
}

const SCENARIO_PRESETS: ScenarioPreset[] = [
  {
    id: 'light',
    badge: '省流入门',
    title: '预设 A · 轻量纯记忆检索',
    shortTitle: '轻量纯记忆检索',
    summary: '适合刚接好向量模型、希望先跑通基础上下文记忆召回且尽量节省大模型 Token 的用户。',
    highlights: [
      '开启对话自动记忆注入（enable_auto_inject）',
      '关闭每日日记、信念反思、关系小传等后台衍生调用',
    ],
    fields: [
      { key: 'Query_Settings.enable_auto_inject', label: '开启自动记忆注入能力', value: true },
      { key: 'Learning_Settings.daily_diary_enabled', label: '每日日记', value: false },
      { key: 'Learning_Settings.belief_reflection_enabled', label: '信念反思', value: false },
      { key: 'Learning_Settings.relationship_portrait_enabled', label: '关系小传', value: false },
    ],
    payload: {
      Query_Settings: {
        enable_auto_inject: true,
      },
      Learning_Settings: {
        daily_diary_enabled: false,
        belief_reflection_enabled: false,
        relationship_portrait_enabled: false,
      },
    },
  },
  {
    id: 'companion',
    badge: '推荐日常',
    recommended: true,
    title: '预设 B · 拟人群聊伴侣',
    shortTitle: '拟人群聊伴侣',
    summary: '适合日常活跃群聊陪伴：自动提取标签、对话后记账印象与事实，每晚沉淀群日记并自动审核高频黑话与表达。',
    highlights: [
      '开启自动记忆注入 + 标签特征提取',
      '开启事后记账与每晚第一人称群日记',
      '开启事实/黑话与群友表达候选自动审核',
    ],
    fields: [
      { key: 'Query_Settings.enable_auto_inject', label: '开启自动记忆注入能力', value: true },
      { key: 'Tag_Settings.tag_extraction_enabled', label: '启用标签特征分析', value: true },
      { key: 'Learning_Settings.post_reply_bookkeeping_enabled', label: '事后记账', value: true },
      { key: 'Learning_Settings.daily_diary_enabled', label: '每日日记', value: true },
      { key: 'Learning_Settings.expression_auto_approve_enabled', label: '表达候选自动审核', value: true },
      { key: 'Learning_Settings.auto_approve_enabled', label: '自动审核事实 / 黑话', value: true },
    ],
    payload: {
      Query_Settings: {
        enable_auto_inject: true,
      },
      Tag_Settings: {
        tag_extraction_enabled: true,
      },
      Learning_Settings: {
        post_reply_bookkeeping_enabled: true,
        daily_diary_enabled: true,
        expression_auto_approve_enabled: true,
        auto_approve_enabled: true,
      },
    },
  },
  {
    id: 'digital_life',
    badge: '深度养成',
    title: '预设 C · 跨端数字生命全开',
    shortTitle: '跨端数字生命全开',
    summary: '在拟人伴侣基础上激活完整自我演化闭环：从日记反思个人看法、撰写群友关系小传、追踪回复反馈并学习情景表达。',
    highlights: [
      '包含「拟人群聊伴侣」全部记忆、记账与日记能力',
      '开启信念反思、群友关系小传、回复效果反馈与群友表达学习',
    ],
    botFollowupTip: '套用后可前往「Bot 管理」按角色单独开启「主动对话」与「激素身体」。',
    fields: [
      { key: 'Query_Settings.enable_auto_inject', label: '开启自动记忆注入能力', value: true },
      { key: 'Tag_Settings.tag_extraction_enabled', label: '启用标签特征分析', value: true },
      { key: 'Learning_Settings.post_reply_bookkeeping_enabled', label: '事后记账', value: true },
      { key: 'Learning_Settings.daily_diary_enabled', label: '每日日记', value: true },
      { key: 'Learning_Settings.expression_auto_approve_enabled', label: '表达候选自动审核', value: true },
      { key: 'Learning_Settings.auto_approve_enabled', label: '自动审核事实 / 黑话', value: true },
      { key: 'Learning_Settings.belief_reflection_enabled', label: '信念反思', value: true },
      { key: 'Learning_Settings.relationship_portrait_enabled', label: '关系小传', value: true },
      { key: 'Learning_Settings.reply_feedback_enabled', label: '从事后记账记录回复效果', value: true },
      { key: 'Learning_Settings.expression_learning_enabled', label: '学习群友的情景表达', value: true },
    ],
    payload: {
      Query_Settings: {
        enable_auto_inject: true,
      },
      Tag_Settings: {
        tag_extraction_enabled: true,
      },
      Learning_Settings: {
        post_reply_bookkeeping_enabled: true,
        daily_diary_enabled: true,
        expression_auto_approve_enabled: true,
        auto_approve_enabled: true,
        belief_reflection_enabled: true,
        relationship_portrait_enabled: true,
        reply_feedback_enabled: true,
        expression_learning_enabled: true,
      },
    },
  },
]

function readCollapsedPreference(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(ONBOARDING_COLLAPSED_KEY) === 'true'
  } catch {
    return false
  }
}

function writeCollapsedPreference(collapsed: boolean): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(ONBOARDING_COLLAPSED_KEY, collapsed ? 'true' : 'false')
  } catch {
    // 忽略受限存储环境异常
  }
}

function trimString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function readProviderState(entriesByKey: ReadonlyMap<string, ConfigEntry>, key: string): {
  value: string
  pendingRestart: boolean
} {
  const entry = entriesByKey.get(key)
  if (!entry) return { value: '', pendingRestart: false }
  const effective = trimString(entry.effectiveValue)
  if (effective) return { value: effective, pendingRestart: false }
  const saved = trimString(entry.savedValue)
  if (saved) return { value: saved, pendingRestart: true }
  return { value: '', pendingRestart: false }
}

function detectMatchedPreset(entriesByKey: ReadonlyMap<string, ConfigEntry>): ScenarioPreset | null {
  if (!entriesByKey.size) return null
  // 按从严到宽检测（预设 C 是预设 B 的超集）
  const ordered: ScenarioPreset[] = [SCENARIO_PRESETS[2], SCENARIO_PRESETS[1], SCENARIO_PRESETS[0]]
  for (const preset of ordered) {
    const presentFields = preset.fields.filter((field) => entriesByKey.has(field.key))
    if (presentFields.length !== preset.fields.length) continue
    const allMatch = presentFields.every((field) => {
      const entry = entriesByKey.get(field.key)
      const current = entry?.effectiveValue ?? entry?.savedValue
      return sameValue(current, field.value)
    })
    if (allMatch) {
      // 预设 B 需排除已开启深度养成关键项的情况
      if (preset.id === 'companion') {
        const beliefOn = entriesByKey.get('Learning_Settings.belief_reflection_enabled')?.effectiveValue === true
        const portraitOn = entriesByKey.get('Learning_Settings.relationship_portrait_enabled')?.effectiveValue === true
        if (beliefOn || portraitOn) continue
      }
      return preset
    }
  }
  return null
}

const BOTS_LOCATION: ConfigLocation = {
  target: 'bots',
  pathname: '/bots',
  params: {},
  label: 'Bot 管理',
}

export function OnboardingPresetsPanel({ entries, onLocate, onApplied }: OnboardingPresetsPanelProps) {
  const [collapsed, setCollapsed] = useState<boolean>(readCollapsedPreference)
  const { request: requestConfirm, dialog: confirmDialog, running: savingPreset } = useConfirmAction()

  const entriesByKey = useMemo(() => new Map(entries.map((entry) => [entry.key, entry])), [entries])

  const embedding = useMemo(() => readProviderState(entriesByKey, 'embedding_provider_id'), [entriesByKey])
  const tagLlm = useMemo(() => readProviderState(entriesByKey, 'tag_llm_provider_id'), [entriesByKey])
  const metaDefaultModel = useMemo(
    () => readProviderState(entriesByKey, 'MetaThinking_Settings.default_model'),
    [entriesByKey],
  )

  const hasEmbedding = Boolean(embedding.value)
  const hasLlm = Boolean(tagLlm.value || metaDefaultModel.value)
  const step1Ready = hasEmbedding && hasLlm
  const botEntryCount = useMemo(() => entries.filter((entry) => entry.kind === 'bot').length, [entries])
  const matchedPreset = useMemo(() => detectMatchedPreset(entriesByKey), [entriesByKey])

  function handleOpenChange(nextOpen: boolean) {
    const nextCollapsed = !nextOpen
    setCollapsed(nextCollapsed)
    writeCollapsedPreference(nextCollapsed)
  }

  function handleApplyPreset(preset: ScenarioPreset) {
    const previewRows = preset.fields.map((field) => {
      const existing = entriesByKey.get(field.key)
      const currentVal = existing ? existing.effectiveValue : undefined
      return {
        ...field,
        title: existing?.title || field.label,
        before: existing ? displayValue(field.key, currentVal) : '默认/未读取',
        after: displayValue(field.key, field.value),
        unchanged: existing ? sameValue(currentVal, field.value) : false,
        restartRequired: existing?.restartRequired === true || existing?.applyMode === 'restart',
      }
    })
    const hasRestartField = previewRows.some((row) => row.restartRequired && !row.unchanged)

    requestConfirm({
      title: `应用「${preset.title}」？`,
      description: (
        <div className="flex flex-col gap-2.5 text-xs">
          <p className="text-muted-foreground">{preset.summary}</p>
          <ul className="flex max-h-64 flex-col gap-1.5 overflow-auto text-foreground" aria-label="预设变更预览">
            {previewRows.map((row) => (
              <li key={row.key} className="rounded-md border px-2.5 py-1.5">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <span className="font-medium">{row.title}</span>
                  <div className="flex items-center gap-1">
                    {row.unchanged ? <Badge variant="outline">已一致</Badge> : <Badge variant="secondary">将更新</Badge>}
                    {row.restartRequired ? <Badge variant="outline">需重启</Badge> : null}
                  </div>
                </div>
                <div className="mt-0.5 flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-muted-foreground">
                  <span>{row.key}</span>
                  <span className="text-foreground">{row.before} → {row.after}</span>
                </div>
              </li>
            ))}
          </ul>
          {hasRestartField ? (
            <p className="text-muted-foreground">提示：标记「需重启」的衍生任务开关保存后需重启一次 AstrBot 完全生效。</p>
          ) : null}
          {preset.botFollowupTip ? (
            <p className="font-medium text-foreground">{preset.botFollowupTip}</p>
          ) : null}
        </div>
      ),
      label: '确认应用',
      run: async () => {
        const response = await saveFullConfig(preset.payload)
        if (!response.ok) {
          throw new Error(response.errors?.join('；') || response.error || '场景预设保存失败')
        }
        toast.success(
          response.restart_required
            ? `已应用「${preset.shortTitle}」；部分开关需重启 AstrBot 后生效`
            : (response.message || `已应用「${preset.shortTitle}」`),
        )
        await onApplied()
      },
    })
  }

  return (
    <>
      <Collapsible
        open={!collapsed}
        onOpenChange={handleOpenChange}
        className="rounded-xl border bg-card"
        data-testid="onboarding-presets-panel"
      >
        <CollapsibleTrigger
          className="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
          aria-label={collapsed ? '展开新手向导与场景预设' : '收起新手向导与场景预设'}
        >
          <span className="flex min-w-0 items-center gap-2">
            <ChevronRightIcon
              className={cn('size-4 shrink-0 text-muted-foreground transition-transform', !collapsed && 'rotate-90')}
              aria-hidden="true"
            />
            <SparklesIcon className="size-4 shrink-0 text-primary" aria-hidden="true" />
            <span className="text-sm font-semibold">新手快速上手向导与场景预设</span>
          </span>
          <span className="flex flex-wrap items-center gap-1.5">
            {step1Ready ? (
              <Badge variant="default">基础模型已就绪</Badge>
            ) : (
              <Badge variant="secondary">待配置基础模型</Badge>
            )}
            {matchedPreset ? <Badge variant="outline">当前：{matchedPreset.shortTitle}</Badge> : null}
            <span className="text-xs text-muted-foreground">{collapsed ? '点击展开' : '收起向导'}</span>
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <div className="space-y-4 border-t px-4 py-4">
            <Alert>
              <InfoIcon />
              <AlertTitle>统一在 9876 控制台管理配置</AlertTitle>
              <AlertDescription>
                建议统一在当前 9876 控制台管理系统与 Bot 配置——AstrBot 自带插件表单（6185）仅用于填写初始端口与基础模型，保存旧表单可能覆盖新增开关。
              </AlertDescription>
            </Alert>

            <div className="grid gap-3 md:grid-cols-3" aria-label="新手 3 步就绪检查单">
              {/* 第 1 步：基础向量与大模型 */}
              <div className="flex flex-col justify-between gap-3 rounded-lg border p-3" data-testid="onboarding-step-models">
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-muted-foreground">第 1 步</span>
                    {step1Ready ? (
                      <Badge variant="default">
                        <CheckCircle2Icon />已就绪
                      </Badge>
                    ) : (
                      <Badge variant="secondary">
                        <CircleDashedIcon />待完成
                      </Badge>
                    )}
                  </div>
                  <div className="text-sm font-medium">基础向量与大模型</div>
                  <div className="space-y-1 text-xs text-muted-foreground">
                    <div>
                      向量模型：
                      {hasEmbedding ? (
                        <span className="font-mono text-foreground">
                          {embedding.value}
                          {embedding.pendingRestart ? '（待重启生效）' : ''}
                        </span>
                      ) : (
                        <span className="text-destructive">未配置 embedding_provider_id</span>
                      )}
                    </div>
                    <div>
                      后台大模型：
                      {hasLlm ? (
                        <span className="font-mono text-foreground">
                          {tagLlm.value || metaDefaultModel.value}
                          {(tagLlm.value ? tagLlm.pendingRestart : metaDefaultModel.pendingRestart) ? '（待重启生效）' : ''}
                        </span>
                      ) : (
                        <span className="text-destructive">未配置标签或默认心智模型</span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {!hasEmbedding ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="default"
                      onClick={() => onLocate(locateConfig('embedding_provider_id', { applyMode: 'restart' }))}
                    >
                      去配置向量模型
                    </Button>
                  ) : null}
                  {!hasLlm ? (
                    <Button
                      type="button"
                      size="sm"
                      variant={hasEmbedding ? 'default' : 'outline'}
                      onClick={() => onLocate(locateConfig('tag_llm_provider_id', { applyMode: 'restart' }))}
                    >
                      去配置大模型
                    </Button>
                  ) : null}
                  {step1Ready ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onLocate(locateConfig('embedding_provider_id', { applyMode: 'restart' }))}
                    >
                      查看模型设置
                    </Button>
                  ) : null}
                </div>
              </div>

              {/* 第 2 步：Bot 身份档案 */}
              <div className="flex flex-col justify-between gap-3 rounded-lg border p-3" data-testid="onboarding-step-bots">
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-muted-foreground">第 2 步</span>
                    <Badge variant="outline">
                      <BotIcon />角色档案
                    </Badge>
                  </div>
                  <div className="text-sm font-medium">Bot 身份档案</div>
                  <p className="text-xs text-muted-foreground">
                    前往 Bot 管理维护羽书或各角色的身份绑定、人设偏好、主动对话与激素作息。
                    {botEntryCount > 0 ? ` 当前已存在 ${botEntryCount} 项角色级覆盖。` : ''}
                  </p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <Button type="button" size="sm" variant="outline" onClick={() => onLocate(BOTS_LOCATION)}>
                    去管理 Bot 身份
                  </Button>
                </div>
              </div>

              {/* 第 3 步：选择运行模式预设 */}
              <div className="flex flex-col justify-between gap-3 rounded-lg border p-3" data-testid="onboarding-step-presets">
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-muted-foreground">第 3 步</span>
                    <Badge variant={matchedPreset ? 'default' : 'outline'}>
                      <SlidersHorizontalIcon />{matchedPreset ? '已匹配预设' : '一键套用'}
                    </Badge>
                  </div>
                  <div className="text-sm font-medium">选择运行模式预设</div>
                  <p className="text-xs text-muted-foreground">
                    {matchedPreset
                      ? `当前全局开关匹配「${matchedPreset.title}」，也可随时切换下方任意场景卡片。`
                      : '根据算力预算与养成深度，在下方三张场景卡片中一键套用推荐开关组合。'}
                  </p>
                </div>
                <div className="text-xs text-muted-foreground">
                  仅修改对应功能开关，不会改动已填好的模型、密码或通道参数。
                </div>
              </div>
            </div>

            <div className="grid gap-3 md:grid-cols-3" aria-label="三大场景预设">
              {SCENARIO_PRESETS.map((preset) => {
                const isMatched = matchedPreset?.id === preset.id
                return (
                  <Card
                    key={preset.id}
                    size="sm"
                    className={cn('justify-between', isMatched && 'ring-2 ring-primary/50')}
                    data-testid={`preset-card-${preset.id}`}
                  >
                    <CardHeader>
                      <div className="flex flex-wrap items-center justify-between gap-1.5">
                        <Badge variant={preset.recommended ? 'default' : 'secondary'}>{preset.badge}</Badge>
                        {isMatched ? <Badge variant="outline">当前匹配</Badge> : null}
                      </div>
                      <CardTitle className="mt-1">{preset.title}</CardTitle>
                      <CardDescription className="text-xs">{preset.summary}</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2 text-xs">
                      <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
                        {preset.highlights.map((line) => (
                          <li key={line}>{line}</li>
                        ))}
                      </ul>
                      {preset.botFollowupTip ? (
                        <div className="rounded-md bg-muted/60 px-2.5 py-1.5 text-muted-foreground">
                          {preset.botFollowupTip}{' '}
                          <button
                            type="button"
                            className="font-medium text-foreground underline underline-offset-2 hover:text-primary"
                            onClick={() => onLocate(BOTS_LOCATION)}
                          >
                            前往 Bot 管理
                          </button>
                        </div>
                      ) : null}
                    </CardContent>
                    <CardFooter className="justify-between gap-2">
                      <span className="text-xs text-muted-foreground">涉及 {preset.fields.length} 项开关</span>
                      <Button
                        type="button"
                        size="sm"
                        variant={preset.recommended ? 'default' : 'outline'}
                        disabled={savingPreset}
                        aria-label={`应用 ${preset.title}`}
                        onClick={() => handleApplyPreset(preset)}
                      >
                        应用此预设
                      </Button>
                    </CardFooter>
                  </Card>
                )
              })}
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
      {confirmDialog}
    </>
  )
}

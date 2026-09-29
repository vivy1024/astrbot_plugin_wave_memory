import { useCallback, useEffect, useState } from 'react'
import { HistoryIcon, PencilIcon, RefreshCwIcon, SparklesIcon } from 'lucide-react'

import type { ApiError } from '@/api/client'
import {
  getRelationshipPortrait,
  getRelationshipPortraitHistory,
  keepRelationshipPortrait,
  regenerateRelationshipPortrait,
  saveRelationshipPortrait,
  type RelationshipPortraitHistoryItem,
  type RelationshipPortraitQuery,
  type RelationshipPortraitView,
} from '@/api/people'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { humanizeApiError } from '@/lib/reason-label'

const ERROR_MESSAGES: Record<string, string> = {
  portrait_version_conflict: '小传刚被修改过（别处保存或后台刚生成），已刷新到最新版本，请确认后再操作',
  portrait_edited_by_admin: '管理员改过的小传不会被自动覆盖',
  portrait_service_unavailable: '没有可用的模型，暂时无法生成关系小传',
  portrait_store_unavailable: '关系小传存储还没准备好',
  portrait_too_few_events: '公开场合的记录太少，还写不出小传',
  portrait_llm_failed: '模型调用失败，请稍后重试',
  portrait_empty_output: '模型没有写出内容，请稍后重试',
  portrait_not_found: '还没有关系小传',
  portrait_content_required: '小传正文不能为空',
  identity_contamination: '小传里有会被身份安全规则拦下的内容，请改写',
  version_required: '缺少版本号，请刷新后重试',
}

/** 最近一次生成失败的原因（last_error 以错误码开头）。 */
const LAST_ERROR_LABELS: Record<string, string> = {
  identity_contamination: '写出的内容疑似身份污染，没有保存',
  empty_output: '模型没有写出内容',
  llm_error: '模型调用失败',
}

function errorCode(error: unknown): string {
  const payload = (error as ApiError | undefined)?.payload as { error?: { code?: unknown } } | undefined
  const code = payload?.error?.code
  return typeof code === 'string' ? code : ''
}

function portraitError(error: unknown, fallback: string): string {
  const code = errorCode(error)
  if (code === 'portrait_too_long') return humanizeApiError(error, '小传超过字数上限')
  return ERROR_MESSAGES[code] ?? humanizeApiError(error, fallback)
}

function lastErrorLabel(raw: string): string {
  const code = raw.split(':')[0]?.trim() ?? ''
  return LAST_ERROR_LABELS[code] ?? '生成失败'
}

function formatTime(value: number | null | undefined): string {
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toLocaleString('zh-CN') : ''
}

function statusLabel(status: string): string {
  return status === 'edited' ? '管理员改过' : '模型生成'
}

export function RelationshipPortraitPanel({ query, userId }: { query: RelationshipPortraitQuery; userId: string }) {
  const { bot_id: botId, session_id: sessionId } = query
  const [view, setView] = useState<RelationshipPortraitView | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [reload, setReload] = useState(0)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<'' | 'save' | 'regenerate' | 'keep'>('')
  const [actionError, setActionError] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState<RelationshipPortraitHistoryItem[] | null>(null)
  const [historyError, setHistoryError] = useState('')

  useEffect(() => {
    if (!botId || !sessionId || !userId) return
    const controller = new AbortController()
    setLoading(true)
    setLoadError('')
    getRelationshipPortrait({ bot_id: botId, session_id: sessionId, visibility: 'group', user_id: userId }, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setView(data) })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setView(null)
        setLoadError(portraitError(reason, '关系小传读取失败'))
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [botId, reload, sessionId, userId])

  const loadHistory = useCallback(async () => {
    setHistoryError('')
    try {
      const data = await getRelationshipPortraitHistory({ bot_id: botId, session_id: sessionId, visibility: 'group', user_id: userId })
      setHistory(data.items ?? [])
    } catch (reason) {
      setHistory(null)
      setHistoryError(portraitError(reason, '历史版本读取失败'))
    }
  }, [botId, sessionId, userId])

  useEffect(() => {
    if (historyOpen) void loadHistory()
  }, [historyOpen, loadHistory, view?.version])

  const item = view?.item ?? null
  const settings = view?.settings
  const version = view?.version ?? 0
  const maxChars = settings?.max_chars ?? 300
  const scopeQuery: RelationshipPortraitQuery = { bot_id: botId, session_id: sessionId, visibility: 'group' }

  async function run(kind: 'save' | 'regenerate' | 'keep', action: () => Promise<{ item: RelationshipPortraitView }>, fallback: string) {
    setBusy(kind)
    setActionError('')
    try {
      const result = await action()
      setView(result.item)
      if (kind === 'save') setEditing(false)
    } catch (reason) {
      setActionError(portraitError(reason, fallback))
      if (errorCode(reason) === 'portrait_version_conflict') setReload((value) => value + 1)
    } finally {
      setBusy('')
    }
  }

  function startEdit() {
    setDraft(item?.content ?? '')
    setActionError('')
    setEditing(true)
  }

  function handleSave() {
    const content = draft.trim()
    if (!content) {
      setActionError('小传正文不能为空')
      return
    }
    if (content.length > maxChars) {
      setActionError(`小传最多 ${maxChars} 字，当前 ${content.length} 字`)
      return
    }
    void run('save', () => saveRelationshipPortrait(scopeQuery, { user_id: userId, version, content }), '保存关系小传失败')
  }

  function handleRegenerate() {
    if (item?.status === 'edited' && !window.confirm('会覆盖管理员改过的小传（旧版本留档可回看），确定重新生成？')) return
    void run('regenerate', () => regenerateRelationshipPortrait(scopeQuery, { user_id: userId, version }), '重新生成失败')
  }

  function handleKeep() {
    void run('keep', () => keepRelationshipPortrait(scopeQuery, { user_id: userId, version }), '保留失败')
  }

  const lastError = view?.last_error ?? ''
  const canGenerate = Boolean(settings?.service_available) && !busy

  return (
    <div className="rounded-lg border border-sky-500/25 bg-sky-500/[0.05] p-3.5" data-slot="relationship-portrait">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="border-sky-500/40 text-sky-700 dark:text-sky-300">关系小传</Badge>
        {item ? <Badge variant="secondary">{statusLabel(item.status)}</Badge> : null}
        {item?.pending_update ? <Badge className="bg-amber-500 text-white">有新记录待更新</Badge> : null}
        <span className="text-xs text-muted-foreground">Bot 以自己的视角写的这个人，只用公开场合的记录；聊天时关系段以它为主</span>
        <Button type="button" size="sm" variant="ghost" className="ml-auto h-6 px-1.5 text-xs" aria-label="刷新关系小传" onClick={() => setReload((value) => value + 1)}>
          <RefreshCwIcon className="size-3" aria-hidden="true" />
        </Button>
      </div>

      {settings && !settings.injection_enabled ? (
        <p className="mb-2 text-xs text-muted-foreground">
          自动生成与注入还没开启（系统配置 → 记账与主动回复 → 关系小传）；可以先在这里手动生成、编辑预览，开启前聊天仍用印象时间线摘要。
        </p>
      ) : null}

      {loading && !view ? (
        <p className="text-xs text-muted-foreground">正在读取关系小传…</p>
      ) : loadError ? (
        <p className="text-xs text-destructive">{loadError}</p>
      ) : editing ? (
        <div className="grid gap-2">
          <Textarea aria-label="编辑关系小传" value={draft} onChange={(event) => setDraft(event.target.value)} rows={5} className="text-sm" />
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={draft.trim().length > maxChars ? 'text-destructive' : 'text-muted-foreground'}>{draft.trim().length} / {maxChars} 字</span>
            <span className="text-muted-foreground">保存后后台不再自动覆盖，有新记录时会提示</span>
            <div className="ml-auto flex gap-1.5">
              <Button type="button" size="sm" variant="ghost" disabled={busy === 'save'} onClick={() => { setEditing(false); setActionError('') }}>取消</Button>
              <Button type="button" size="sm" disabled={busy === 'save'} onClick={handleSave}>{busy === 'save' ? '保存中…' : '保存'}</Button>
            </div>
          </div>
        </div>
      ) : item ? (
        <div className="grid gap-1.5">
          <p className="whitespace-pre-wrap break-words text-sm text-foreground/90">{item.content}</p>
          <p className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
            <span>更新于 {formatTime(item.generated_at ?? item.updated_at) || '未记录'}</span>
            <span>模型 {item.provider || '未记录'}</span>
            <span>依据截至事件#{item.source_max_event_id}（共 {item.source_event_count} 条）</span>
            <span>第 {item.version} 版</span>
            {item.status === 'edited' && item.edited_by ? <span>编辑者 {item.edited_by}</span> : null}
            {view && view.timeline.new_since_portrait > 0 ? <span>之后新增 {view.timeline.new_since_portrait} 条公开记录</span> : null}
          </p>
        </div>
      ) : view ? (
        <p className="text-sm text-muted-foreground">
          还没有关系小传。公开场合的记录 {view.timeline.non_private_total} 条（少于 {settings?.min_events ?? 3} 条不写，聊天时沿用印象时间线摘要）。
        </p>
      ) : null}

      {lastError && !editing ? (
        <p className="mt-2 text-xs text-destructive" title={lastError}>
          最近一次生成失败：{lastErrorLabel(lastError)}{view?.last_error_at ? `（${formatTime(view.last_error_at)}）` : ''}
        </p>
      ) : null}
      {actionError ? <p className="mt-2 text-xs text-destructive" role="alert">{actionError}</p> : null}

      {view && !editing ? (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          <Button type="button" size="sm" variant="outline" disabled={Boolean(busy)} onClick={startEdit}>
            <PencilIcon data-icon="inline-start" aria-hidden="true" />{item ? '编辑' : '手写一版'}
          </Button>
          <Button
            type="button" size="sm" variant="outline" disabled={!canGenerate} onClick={handleRegenerate}
            title={settings?.service_available ? undefined : '没有可用的模型'}
          >
            <SparklesIcon data-icon="inline-start" aria-hidden="true" />{busy === 'regenerate' ? '生成中…' : item ? '重新生成' : '生成'}
          </Button>
          {item?.pending_update ? (
            <Button type="button" size="sm" variant="outline" disabled={Boolean(busy)} onClick={handleKeep}>
              {busy === 'keep' ? '处理中…' : '保留当前'}
            </Button>
          ) : null}
          {view.history_count > 0 ? (
            <Button type="button" size="sm" variant="ghost" onClick={() => setHistoryOpen((open) => !open)} aria-expanded={historyOpen}>
              <HistoryIcon data-icon="inline-start" aria-hidden="true" />{historyOpen ? '收起历史版本' : `历史版本（${view.history_count}）`}
            </Button>
          ) : null}
        </div>
      ) : null}

      {historyOpen ? (
        <div className="mt-2.5 border-t border-sky-500/20 pt-2.5">
          {historyError ? (
            <p className="text-xs text-destructive">{historyError}</p>
          ) : !history ? (
            <p className="text-xs text-muted-foreground">正在读取历史版本…</p>
          ) : !history.length ? (
            <p className="text-xs text-muted-foreground">没有历史版本。</p>
          ) : (
            <ol className="flex flex-col gap-2" aria-label="关系小传历史版本">
              {history.map((entry) => (
                <li key={entry.id} className="rounded-md bg-background/60 px-2.5 py-2">
                  <p className="mb-1 flex flex-wrap gap-x-2 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground/80">第 {entry.version} 版</span>
                    <span>{statusLabel(entry.status)}</span>
                    <span>{formatTime(entry.generated_at) || '未记录时间'}</span>
                    {entry.status === 'edited' ? (entry.edited_by ? <span>编辑者 {entry.edited_by}</span> : null) : <span>模型 {entry.provider || '未记录'}</span>}
                  </p>
                  <p className="whitespace-pre-wrap break-words text-sm">{entry.content}</p>
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </div>
  )
}

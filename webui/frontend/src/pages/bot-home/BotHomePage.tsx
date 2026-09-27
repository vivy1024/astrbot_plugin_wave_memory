import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  ActivityIcon,
  ArrowRightIcon,
  BookOpenIcon,
  BotIcon,
  BrainIcon,
  HeartHandshakeIcon,
  HeartIcon,
  MessageSquareQuoteIcon,
  RefreshCwIcon,
  SparklesIcon,
} from 'lucide-react'

import {
  getBotHome,
  type BotHomeHighlight,
  type BotHomeLearnedBucket,
  type BotHomeLearnedKind,
  type BotHomePayload,
  type BotHomePlace,
  type BotHomeRelationship,
  type BotHomeReply,
  type BotHomeSectionBase,
} from '@/api/botHome'
import { isRequestCancelled } from '@/api/client'
import { useGlobalScope } from '@/app/global-scope'
import { QueryState } from '@/components/shared/QueryState'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { sanitizeDisplayName } from '@/lib/display-name'
import { formatDisplayNumber, formatSignedDisplayNumber } from '@/lib/format-number'
import { humanizeReason } from '@/lib/reason-label'
import { cn } from '@/lib/utils'

// ─────────────────────────── 深链接 ───────────────────────────

type LinkPlace = Pick<BotHomePlace, 'session_id' | 'visibility'> | null | undefined

/** 与 navigation-search 的共享作用域键一致：bot_id / session_id / visibility，再加页面自己的参数。 */
export function botHomeHref(
  pathname: string,
  botId: string,
  place?: LinkPlace,
  extra: Record<string, string | number | null | undefined> = {},
): string {
  const params = new URLSearchParams()
  if (botId) params.set('bot_id', botId)
  if (place?.session_id) {
    params.set('session_id', place.session_id)
    params.set('visibility', place.visibility || 'group')
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const query = params.toString()
  return query ? `${pathname}?${query}` : pathname
}

// ─────────────────────────── 文案 ───────────────────────────

const WINDOW_OPTIONS = [
  { days: 1, label: '今天' },
  { days: 3, label: '近 3 天' },
  { days: 7, label: '近 7 天' },
] as const

function windowLabel(days: number): string {
  return WINDOW_OPTIONS.find((item) => item.days === days)?.label ?? `近 ${days} 天`
}

function parseDays(value: string | null): number {
  const days = Number(value)
  return WINDOW_OPTIONS.some((item) => item.days === days) ? days : 1
}

const numberFormat = new Intl.NumberFormat('zh-CN')

function formatCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? numberFormat.format(value) : '—'
}

function formatTime(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '时间未知'
  const date = new Date(seconds * 1000)
  const now = new Date()
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (date.toDateString() === now.toDateString()) return time
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}

export function describeMood(valence: number | null | undefined, arousal: number | null | undefined): string {
  const v = typeof valence === 'number' && Number.isFinite(valence) ? valence : null
  const a = typeof arousal === 'number' && Number.isFinite(arousal) ? arousal : null
  if (v === null && a === null) return '心情未知'
  const feeling = v === null ? '' : v >= 0.5 ? '很开心' : v >= 0.15 ? '愉快' : v > -0.15 ? '平静' : v > -0.5 ? '有点低落' : '低落'
  const energy = a === null ? '' : a >= 0.6 ? '活跃' : a >= 0.3 ? '平稳' : '安静'
  return [feeling, energy].filter(Boolean).join(' · ')
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待审',
  pending_review: '待审',
  active: '已通过',
  approved: '已通过',
  conflict: '有冲突',
  rejected: '已驳回',
  archived: '已归档',
  recorded: '已记录',
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? humanizeReason(status, status || '未标注')
}

function statusBadgeClass(status: string): string {
  if (status === 'pending' || status === 'pending_review') return 'border-primary/30 bg-primary/10 text-primary'
  if (status === 'active' || status === 'approved') return 'border-border bg-secondary text-secondary-foreground'
  if (status === 'conflict' || status === 'rejected') return 'border-destructive/30 bg-destructive/10 text-destructive'
  return 'border-border text-muted-foreground'
}

const LEARNED_META: Record<BotHomeLearnedKind, { title: string; path: string; unit: string }> = {
  facts: { title: '事实', path: '/facts', unit: '条' },
  beliefs: { title: '信念', path: '/beliefs', unit: '条' },
  jargon: { title: '黑话候选', path: '/jargon', unit: '个' },
  experiences: { title: '经历片段', path: '/knowledge/experiences', unit: '段' },
}

const DIMENSION_LABELS: Record<string, string> = {
  familiarity: '熟悉',
  trust: '信任',
  fun: '有趣',
  depth: '深度',
  hostility: '敌意',
}

const RELATIONSHIP_STATE_LABELS: Record<string, string> = {
  intimate: '亲密',
  friendly: '友好',
  neutral: '普通',
  cold: '冷淡',
  hostile: '敌对',
}

const CHANNEL_LABELS: Record<string, string> = {
  memory: '记忆',
  fts5: '原词检索',
  facts: '事实',
  persona: '人格画像',
  soul_state: '心智状态',
  holyman_persona: '人设语料',
  book_lore: '书设',
  belief: '信念',
  jargon: '黑话',
  fewshot: '风格范例',
  affinity: '好感',
  safety: '安全',
}

const HIGHLIGHT_REASON: Record<BotHomeHighlight['reason'], string> = {
  self: '自己说的',
  core: '和她有关',
  important: '被标重要',
}

// ─────────────────────────── 通用片段 ───────────────────────────

/** 昵称可能带双向控制符（QQ 群名片常见），用 bdi 隔离，避免把整行文字方向带歪。 */
function Name({ children }: { children: string }) {
  return <bdi className="font-medium text-foreground">{sanitizeDisplayName(children) || children}</bdi>
}

function SectionLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
      <Link to={to}>
        {children}
        <ArrowRightIcon data-icon="inline-end" aria-hidden="true" />
      </Link>
    </Button>
  )
}

function SectionGate({
  section,
  isEmpty,
  emptyTitle,
  emptyDescription,
  onRetry,
  children,
}: {
  section: BotHomeSectionBase | undefined
  isEmpty: boolean
  emptyTitle: string
  emptyDescription?: string
  onRetry: () => void
  children: ReactNode
}) {
  if (!section || section.status === 'error') {
    return (
      <QueryState
        status="error"
        title="这一块暂时读不到"
        description={humanizeReason(section?.reason_code, '服务端没有返回这一块的数据。')}
        onRetry={onRetry}
      />
    )
  }
  if (section.status === 'unavailable') {
    return <QueryState status="unknown" title="数据源未就绪" description={humanizeReason(section.reason_code, '对应的数据表或服务尚未启用。')} />
  }
  if (isEmpty) return <QueryState status="empty" emptyTitle={emptyTitle} emptyDescription={emptyDescription} />
  return <>{children}</>
}

function MeterBar({ value, max, className }: { value: number; max: number; className?: string }) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden="true">
      <div className={cn('h-full rounded-full bg-primary/70', className)} style={{ width: `${Math.round(ratio * 100)}%` }} />
    </div>
  )
}

function PlaceTag({ place }: { place: BotHomePlace }) {
  return <span className="truncate text-xs text-muted-foreground" title={place.session_id ?? undefined}>{place.label}</span>
}

// ─────────────────────────── 顶部概括 ───────────────────────────

export function summarizeBotHome(data: BotHomePayload, days: number): string {
  const name = data.bot.name || data.bot.db_id
  const memories = data.memories
  if (memories.status !== 'ready') return `${name} ${windowLabel(days)}的记忆暂时读不到`
  const total = memories.total ?? 0
  if (total === 0) return `${name} ${windowLabel(days)}还没有记住新的东西`
  const top = memories.top_groups?.[0]
  // 拿不到群名时 label 是群号，补个「群」字读起来才像一句话。
  const where = top ? (/^\d+$/.test(top.label) ? `群 ${top.label}` : top.label) : ''
  return `${name} ${windowLabel(days)}记住了 ${formatCount(total)} 条${where ? `，最活跃的是 ${where}` : ''}`
}

function learnedTotal(data: BotHomePayload): number | null {
  if (data.learned.status !== 'ready') return null
  return (['facts', 'beliefs', 'jargon', 'experiences'] as const)
    .reduce((sum, key) => sum + (data.learned[key]?.total ?? 0), 0)
}

function SummaryCard({
  data,
  days,
  onDaysChange,
  onRefresh,
  refreshing,
}: {
  data: BotHomePayload
  days: number
  onDaysChange: (days: number) => void
  onRefresh: () => void
  refreshing: boolean
}) {
  const facts: string[] = []
  const changed = data.relationships.status === 'ready' ? data.relationships.people_changed ?? 0 : null
  if (changed) facts.push(`和 ${changed} 个人的关系有变化`)
  const learned = learnedTotal(data)
  if (learned) facts.push(`新学到 ${learned} 条`)
  const replies = data.replies.status === 'ready' ? data.replies.items?.length ?? 0 : 0
  if (replies) facts.push(`最近 ${replies} 次回复有记录`)
  return (
    <Card className="bg-gradient-to-br from-primary/10 via-card to-card lg:col-span-2">
      <CardHeader>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <BotIcon className="size-4 text-primary" aria-hidden="true" />
          <span>Bot 主页 · {data.bot.db_id}</span>
        </div>
        <CardTitle className="text-xl leading-snug font-semibold tracking-tight sm:text-2xl" data-testid="bot-home-summary">
          {summarizeBotHome(data, days)}
        </CardTitle>
        {facts.length ? <CardDescription>{facts.join('，')}。</CardDescription> : null}
        <CardAction>
          <Button type="button" variant="outline" size="icon-sm" onClick={onRefresh} disabled={refreshing} aria-label="刷新">
            <RefreshCwIcon className={cn(refreshing && 'motion-safe:animate-spin')} />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            { label: '新记忆', value: data.memories.status === 'ready' ? formatCount(data.memories.total) : '—' },
            { label: '关系有变化', value: changed === null ? '—' : `${formatCount(changed)} 人` },
            { label: '新学到', value: learned === null ? '—' : `${formatCount(learned)} 条` },
            { label: '当前关切', value: data.soul.status === 'ready' ? `${formatCount(data.soul.concerns?.length ?? 0)} 件` : '—' },
          ].map((item) => (
            <div key={item.label} className="rounded-lg border bg-background/60 px-3 py-2">
              <dt className="text-xs text-muted-foreground">{item.label}</dt>
              <dd className="mt-0.5 text-lg font-semibold tabular-nums text-foreground">{item.value}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1" role="group" aria-label="时间窗口">
          {WINDOW_OPTIONS.map((item) => (
            <Button
              key={item.days}
              type="button"
              size="sm"
              variant={item.days === days ? 'secondary' : 'ghost'}
              aria-pressed={item.days === days}
              onClick={() => onDaysChange(item.days)}
            >
              {item.label}
            </Button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          更新于 {formatTime(data.meta.generated_at)} · 聚合耗时 {Math.round(data.meta.elapsed_ms)} ms
        </p>
        </div>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 心情 ───────────────────────────

function MoodCard({ data, botId, onRetry }: { data: BotHomePayload; botId: string; onRetry: () => void }) {
  const soul = data.soul
  const mood = soul.mood ?? null
  const concerns = soul.concerns ?? []
  const soulPlace = mood ?? (soul.scope ? { session_id: soul.scope.session_id, visibility: soul.scope.visibility } : null)
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><HeartIcon className="size-4 text-primary" aria-hidden="true" />心情与关切</CardTitle>
        <CardAction><SectionLink to={botHomeHref('/soul', botId, soulPlace)}>心智状态</SectionLink></CardAction>
      </CardHeader>
      <CardContent>
        <SectionGate section={soul} isEmpty={!mood && concerns.length === 0} emptyTitle="还没有心情记录" emptyDescription="她在群里说过话之后，这里会出现心情与关切。" onRetry={onRetry}>
          <div className="flex flex-col gap-4">
            {mood ? (
              <div className="flex flex-col gap-2">
                <p className="text-lg font-semibold text-foreground" data-testid="bot-home-mood">{describeMood(mood.valence, mood.arousal)}</p>
                {mood.cause ? <p className="text-sm text-muted-foreground">{mood.cause}</p> : null}
                <div className="grid grid-cols-2 gap-3 text-xs text-muted-foreground">
                  <div className="flex flex-col gap-1">
                    <span>愉悦度 {formatSignedDisplayNumber(mood.valence)}</span>
                    <MeterBar value={(mood.valence ?? 0) + 1} max={2} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <span>激活度 {formatDisplayNumber(mood.arousal)}</span>
                    <MeterBar value={mood.arousal ?? 0} max={1} />
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">{formatTime(mood.observed_at)} · 在 {mood.label}</p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">还没有心情记录。</p>
            )}
            <div className="flex flex-col gap-2">
              <p className="text-xs font-medium text-muted-foreground">主要关切</p>
              {concerns.length ? (
                <ul className="flex flex-col gap-2">
                  {concerns.map((concern) => (
                    <li key={concern.id} className="flex flex-col gap-1">
                      <Link to={botHomeHref('/soul', botId, concern)} className="text-sm text-foreground hover:text-primary hover:underline">
                        {concern.topic}
                      </Link>
                      <div className="flex items-center gap-2">
                        <MeterBar value={concern.intensity ?? 0} max={1} />
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">强度 {formatDisplayNumber(concern.intensity)}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">眼下没有挂心的事。</p>
              )}
            </div>
          </div>
        </SectionGate>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 今天记住的 ───────────────────────────

function MemoriesCard({ data, botId, days, onRetry }: { data: BotHomePayload; botId: string; days: number; onRetry: () => void }) {
  const memories = data.memories
  const groups = memories.top_groups ?? []
  const speakers = memories.top_speakers ?? []
  const highlights = memories.highlights ?? []
  const maxGroup = groups[0]?.count ?? 0
  const bySource = memories.by_source ?? {}
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><BrainIcon className="size-4 text-primary" aria-hidden="true" />{windowLabel(days)}记住的</CardTitle>
        <CardDescription>
          共 {formatCount(memories.total)} 条 · 和她有关 {formatCount(bySource.core ?? 0)} · 闲聊 {formatCount(bySource.chat ?? 0)} · 噪声 {formatCount(bySource.noise ?? 0)}
        </CardDescription>
        <CardAction><SectionLink to={botHomeHref('/memories', botId, groups[0])}>记忆</SectionLink></CardAction>
      </CardHeader>
      <CardContent>
        <SectionGate section={memories} isEmpty={!memories.total} emptyTitle={`${windowLabel(days)}还没有新记忆`} onRetry={onRetry}>
          <div className="flex flex-col gap-5">
            <div className="grid gap-5 sm:grid-cols-2">
              <div className="flex flex-col gap-2">
                <p className="text-xs font-medium text-muted-foreground">按群分布</p>
                <ul className="flex flex-col gap-2">
                  {groups.map((group) => (
                    <li key={group.session_id ?? group.label} className="flex flex-col gap-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <Link to={botHomeHref('/memories', botId, group)} className="truncate text-sm text-foreground hover:text-primary hover:underline" title={group.session_id ?? undefined}>
                          {group.label}
                        </Link>
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{formatCount(group.count)}</span>
                      </div>
                      <MeterBar value={group.count} max={maxGroup} />
                    </li>
                  ))}
                </ul>
              </div>
              <div className="flex flex-col gap-2">
                <p className="text-xs font-medium text-muted-foreground">最活跃的人</p>
                <ol className="flex flex-col gap-1.5">
                  {speakers.map((speaker, index) => (
                    <li key={speaker.sender_id} className="flex items-center justify-between gap-2 text-sm">
                      <Link to={botHomeHref('/people', botId, speaker, { search: speaker.sender_id })} className="flex min-w-0 items-center gap-2 hover:text-primary">
                        <span className="w-4 shrink-0 text-xs text-muted-foreground tabular-nums">{index + 1}</span>
                        <span className="truncate"><Name>{speaker.display_name || speaker.sender_id}</Name></span>
                      </Link>
                      <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{formatCount(speaker.count)} 条</span>
                    </li>
                  ))}
                  {speakers.length === 0 ? <li className="text-sm text-muted-foreground">没有发言记录。</li> : null}
                </ol>
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <p className="text-xs font-medium text-muted-foreground">值得记住的话</p>
              {highlights.length ? (
                <ul className="flex flex-col divide-y divide-border">
                  {highlights.map((item) => (
                    <li key={item.id} className="py-2 first:pt-0 last:pb-0">
                      <Link to={botHomeHref('/memories', botId, item, { memory_id: item.id })} className="group flex flex-col gap-1">
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Badge variant="outline" className={cn(item.reason === 'self' && 'border-primary/30 bg-primary/10 text-primary')}>{HIGHLIGHT_REASON[item.reason] ?? item.reason}</Badge>
                          {item.sender_name ? <span className="truncate"><Name>{item.sender_name}</Name></span> : null}
                          <span className="shrink-0">{formatTime(item.timestamp)}</span>
                          <PlaceTag place={item} />
                        </div>
                        <p className="line-clamp-2 text-sm text-foreground/90 group-hover:text-primary">{item.content}</p>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">这段时间没有她自己的话或被标重要的记忆。</p>
              )}
            </div>
          </div>
        </SectionGate>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 关系变化 ───────────────────────────

function DimensionDeltas({ dimensions }: { dimensions: Record<string, number> }) {
  const entries = Object.entries(dimensions).filter(([, value]) => value !== 0)
  if (!entries.length) return null
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([key, value]) => {
        const worse = key === 'hostility' ? value > 0 : value < 0
        return (
          <Badge key={key} variant="outline" className={cn('tabular-nums', worse ? 'border-destructive/30 text-destructive' : 'border-primary/30 text-primary')}>
            {DIMENSION_LABELS[key] ?? key} {formatSignedDisplayNumber(value)}
          </Badge>
        )
      })}
    </div>
  )
}

function AffinityShift({ item }: { item: BotHomeRelationship }) {
  if (item.affinity_after === null) return <span className="text-xs text-muted-foreground">好感未知</span>
  const before = item.affinity_before ?? item.affinity_after
  const diff = item.affinity_after - before
  return (
    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
      好感 {before} → <span className={cn('font-semibold', diff > 0 ? 'text-primary' : diff < 0 ? 'text-destructive' : 'text-foreground')}>{item.affinity_after}</span>
    </span>
  )
}

function RelationshipsCard({ data, botId, days, onRetry }: { data: BotHomePayload; botId: string; days: number; onRetry: () => void }) {
  const section = data.relationships
  const items = section.items ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><HeartHandshakeIcon className="size-4 text-primary" aria-hidden="true" />关系变化</CardTitle>
        <CardDescription>
          {section.status === 'ready' ? `${windowLabel(days)}和 ${formatCount(section.people_changed ?? 0)} 个人有互动变化，按好感变动排序` : '按好感变动排序'}
        </CardDescription>
        <CardAction><SectionLink to={botHomeHref('/people', botId, items[0] ?? data.memories.top_groups?.[0])}>人物</SectionLink></CardAction>
      </CardHeader>
      <CardContent>
        <SectionGate section={section} isEmpty={items.length === 0} emptyTitle="关系没有变化" emptyDescription="这段时间没有记录到正式的好感事件。" onRetry={onRetry}>
          <ul className="flex flex-col divide-y divide-border">
            {items.map((item) => (
              <li key={`${item.session_id}:${item.subject_principal_id}`} className="flex flex-col gap-1.5 py-2.5 first:pt-0 last:pb-0">
                <div className="flex items-center justify-between gap-2">
                  <Link to={botHomeHref('/people', botId, item, { search: item.user_id })} className="flex min-w-0 items-center gap-2 text-sm hover:text-primary">
                    <span className="truncate"><Name>{item.display_name || item.user_id}</Name></span>
                    {item.state ? <Badge variant="outline" className="text-muted-foreground">{RELATIONSHIP_STATE_LABELS[item.state] ?? item.state}</Badge> : null}
                  </Link>
                  <AffinityShift item={item} />
                </div>
                <DimensionDeltas dimensions={item.dimensions} />
                {item.latest_reason ? <p className="line-clamp-2 text-xs text-muted-foreground">最近：{item.latest_reason}</p> : null}
                <p className="text-xs text-muted-foreground">{item.event_count} 次事件 · {formatTime(item.latest_at)} · {item.label}</p>
              </li>
            ))}
          </ul>
        </SectionGate>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 新学到的 ───────────────────────────

function LearnedBucket({ kind, bucket, botId, fallbackPlace }: { kind: BotHomeLearnedKind; bucket: BotHomeLearnedBucket | undefined; botId: string; fallbackPlace: LinkPlace }) {
  const meta = LEARNED_META[kind]
  const samples = bucket?.samples ?? []
  const place = samples[0] ?? fallbackPlace
  const statuses = Object.entries(bucket?.by_status ?? {})
  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/30 p-3" data-testid={`learned-${kind}`}>
      <div className="flex items-baseline justify-between gap-2">
        <Link to={botHomeHref(meta.path, botId, place)} className="text-sm font-medium text-foreground hover:text-primary hover:underline">
          {meta.title}
        </Link>
        <span className="text-lg font-semibold tabular-nums text-foreground">
          {bucket?.status === 'ready' ? formatCount(bucket.total) : '—'}
          <span className="ml-0.5 text-xs font-normal text-muted-foreground">{meta.unit}</span>
        </span>
      </div>
      {bucket && bucket.status !== 'ready' ? (
        <p className="text-xs text-muted-foreground">{bucket.status === 'unavailable' ? '数据源未就绪' : '读取失败'}</p>
      ) : null}
      {statuses.length ? (
        <div className="flex flex-wrap gap-1">
          {statuses.map(([status, count]) => (
            <Link key={status} to={botHomeHref(meta.path, botId, place, kind === 'experiences' ? {} : { status })}>
              <Badge variant="outline" className={statusBadgeClass(status)}>{statusLabel(status)} {count}</Badge>
            </Link>
          ))}
        </div>
      ) : null}
      {samples.length ? (
        <ul className="flex flex-col gap-1.5">
          {samples.map((sample) => (
            <li key={sample.id}>
              <Link to={botHomeHref(meta.path, botId, sample)} className="group flex items-start gap-2">
                <Badge variant="outline" className={cn('mt-0.5', statusBadgeClass(sample.status))}>{statusLabel(sample.status)}</Badge>
                <span className="line-clamp-2 text-xs text-foreground/90 group-hover:text-primary">{sample.text}</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : bucket?.status === 'ready' ? (
        <p className="text-xs text-muted-foreground">没有新增。</p>
      ) : null}
    </div>
  )
}

function LearnedCard({ data, botId, days, onRetry }: { data: BotHomePayload; botId: string; days: number; onRetry: () => void }) {
  const section = data.learned
  const total = learnedTotal(data) ?? 0
  const fallbackPlace = data.memories.top_groups?.[0]
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><SparklesIcon className="size-4 text-primary" aria-hidden="true" />新学到的</CardTitle>
        <CardDescription>{windowLabel(days)}新增 {formatCount(total)} 条，待审的点进去就能审核</CardDescription>
      </CardHeader>
      <CardContent>
        <SectionGate section={section} isEmpty={false} emptyTitle="没有新学到的" onRetry={onRetry}>
          <div className="grid gap-3 sm:grid-cols-2">
            {(['facts', 'beliefs', 'jargon', 'experiences'] as const).map((kind) => (
              <LearnedBucket key={kind} kind={kind} bucket={section[kind]} botId={botId} fallbackPlace={fallbackPlace} />
            ))}
          </div>
        </SectionGate>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 最近的回复 ───────────────────────────

function ReplyRow({ item, botId }: { item: BotHomeReply; botId: string }) {
  const failed = item.status !== 'ok'
  return (
    <li className="py-2.5 first:pt-0 last:pb-0">
      <Link to={botHomeHref('/observatory', botId, item, { trace_id: item.trace_id })} className="group flex flex-col gap-1.5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="shrink-0">{formatTime(item.timestamp)}</span>
          <PlaceTag place={item} />
          <span className="ml-auto shrink-0 tabular-nums">{Math.round(item.latency_ms)} ms</span>
          {failed ? <Badge variant="outline" className="border-destructive/30 text-destructive">{item.status}</Badge> : null}
        </div>
        <p className="line-clamp-1 text-sm text-foreground/90 group-hover:text-primary">
          {item.sender_name ? <><Name>{item.sender_name}</Name>：</> : null}{item.message_preview || '（无消息预览）'}
        </p>
        <div className="flex flex-wrap gap-1">
          {item.hit_channels.length ? item.hit_channels.map((channel) => (
            <Badge key={channel.channel} variant="secondary" className="tabular-nums">
              {CHANNEL_LABELS[channel.channel] ?? channel.channel} {channel.item_count}
            </Badge>
          )) : <span className="text-xs text-muted-foreground">没有命中任何通道</span>}
        </div>
      </Link>
    </li>
  )
}

function RepliesCard({ data, botId, onRetry }: { data: BotHomePayload; botId: string; onRetry: () => void }) {
  const section = data.replies
  const items = section.items ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><MessageSquareQuoteIcon className="size-4 text-primary" aria-hidden="true" />最近的回复</CardTitle>
        <CardDescription>每次回复前注入了哪些记忆通道，点开看完整溯源</CardDescription>
        <CardAction><SectionLink to={botHomeHref('/observatory', botId)}>观测台</SectionLink></CardAction>
      </CardHeader>
      <CardContent>
        <SectionGate section={section} isEmpty={items.length === 0} emptyTitle="还没有注入记录" emptyDescription="她回复过消息之后，这里会出现最近几次的注入 trace。" onRetry={onRetry}>
          <ul className="flex flex-col divide-y divide-border">
            {items.map((item) => <ReplyRow key={item.trace_id} item={item} botId={botId} />)}
          </ul>
        </SectionGate>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────── 页面 ───────────────────────────

type LoadState =
  | { status: 'idle' }
  | { status: 'loading'; data?: BotHomePayload }
  | { status: 'success'; data: BotHomePayload }
  | { status: 'error'; error: unknown; data?: BotHomePayload }

export function BotHomePage() {
  const { botId, status: scopeStatus, optionsError } = useGlobalScope()
  const [searchParams, setSearchParams] = useSearchParams()
  const days = parseDays(searchParams.get('days'))
  const [state, setState] = useState<LoadState>({ status: 'idle' })
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (!botId) {
      setState({ status: 'idle' })
      return
    }
    const controller = new AbortController()
    setState((current) => ({ status: 'loading', data: 'data' in current && current.data?.bot.db_id === botId ? current.data : undefined }))
    getBotHome({ botId, days, signal: controller.signal })
      .then((data) => setState({ status: 'success', data }))
      .catch((error: unknown) => {
        if (isRequestCancelled(error)) return
        setState((current) => ({ status: 'error', error, data: 'data' in current ? current.data : undefined }))
      })
    return () => controller.abort()
  }, [botId, days, reloadKey])

  const reload = useCallback(() => setReloadKey((key) => key + 1), [])
  const changeDays = useCallback((next: number) => {
    setSearchParams((current) => {
      const params = new URLSearchParams(current)
      if (next === 1) params.delete('days')
      else params.set('days', String(next))
      return params
    }, { replace: true })
  }, [setSearchParams])

  const data = 'data' in state ? state.data : undefined
  const header = useMemo(() => (
    <div className="flex items-center gap-2">
      <ActivityIcon className="size-5 text-primary" aria-hidden="true" />
      <h1 className="text-lg font-semibold tracking-tight text-foreground">Bot 主页</h1>
    </div>
  ), [])

  if (!botId) {
    if (scopeStatus === 'resolving') {
      return <div className="flex flex-col gap-4">{header}<QueryState status="loading" title="正在确定 Bot" /></div>
    }
    if (scopeStatus === 'error') {
      return <div className="flex flex-col gap-4">{header}<QueryState status="error" title="Bot 列表加载失败" error={optionsError || undefined} /></div>
    }
    return (
      <div className="flex flex-col gap-4">
        {header}
        <QueryState status="empty" emptyTitle="先选一个 Bot" emptyDescription="在顶栏的作用域选择器里选择 Bot，这里会显示她今天记住了什么、心情如何、和谁的关系变了。" />
      </div>
    )
  }

  if (!data) {
    if (state.status === 'error') {
      return <div className="flex flex-col gap-4">{header}<QueryState status="error" title="Bot 主页加载失败" error={state.error} onRetry={reload} /></div>
    }
    return <div className="flex flex-col gap-4">{header}<QueryState status="loading" title="正在汇总 Bot 主页" loadingRows={6} /></div>
  }

  return (
    <div className="flex flex-col gap-4" data-testid="bot-home-page">
      {state.status === 'error' ? <QueryState status="error" title="刷新失败，下面是上一次的数据" error={state.error} onRetry={reload} /> : null}
      <div className="grid gap-4 lg:grid-cols-3">
        <SummaryCard data={data} days={days} onDaysChange={changeDays} onRefresh={reload} refreshing={state.status === 'loading'} />
        <MoodCard data={data} botId={botId} onRetry={reload} />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <MemoriesCard data={data} botId={botId} days={days} onRetry={reload} />
        <RelationshipsCard data={data} botId={botId} days={days} onRetry={reload} />
        <LearnedCard data={data} botId={botId} days={days} onRetry={reload} />
        <RepliesCard data={data} botId={botId} onRetry={reload} />
      </div>
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <BookOpenIcon className="size-3.5" aria-hidden="true" />
        窗口：{formatTime(data.window.from_ts)} – {formatTime(data.window.to_ts)}。数据只读汇总，点任一条目进入对应页面查看或审核。
      </p>
    </div>
  )
}

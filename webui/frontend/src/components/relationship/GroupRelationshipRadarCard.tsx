import { useEffect, useMemo, useState } from 'react'
import { AlertCircleIcon, RadarIcon } from 'lucide-react'
import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart } from 'recharts'

import type { RelationshipItem } from '@/api/people'
import type { RelationshipHistoryItem } from '@/api/soul'
import type { ObjectRefDescriptor, ObjectRefScopeQuery } from '@/components/shared/types'
import { ObjectDeepLink } from '@/components/shared/ObjectDeepLink'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { formatDisplayNumber } from '@/lib/format-number'
import {
  RADAR_DIMENSIONS,
  relationshipRadarPoints,
  type RelationshipRadarDimension,
} from './RelationshipRadarCard'

const SERIES_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
]

export interface GroupRelationshipRadarCardProps {
  relationships: RelationshipItem[]
  scopeQuery?: ObjectRefScopeQuery
  relationshipLoading?: boolean
  relationshipError?: string | null
  onRetryRelationships?: () => void
  history?: RelationshipHistoryItem[]
  historyLoading?: boolean
  historyError?: string | null
  selectedSubjectId?: string | null
  selectedDimension?: RelationshipRadarDimension | null
  onSelectedSubjectIdChange?: (subjectPrincipalId: string) => void
  onSelectedDimensionChange?: (dimension: RelationshipRadarDimension) => void
  onRetryHistory?: () => void
  maxPeople?: number
}

interface RelationshipSeries {
  item: RelationshipItem
  seriesKey: string
  color: string
  points: ReturnType<typeof relationshipRadarPoints>
}

function formatTime(seconds: unknown): string {
  const value = Number(seconds)
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN') : '未记录'
}

function displayName(item: RelationshipItem): string {
  return item.person.display_name || item.person.nickname || item.person.user_id
}

function sourceText(item: RelationshipHistoryItem): string {
  if (item.source_memory_id !== null && item.source_memory_id !== undefined) return `memory:${item.source_memory_id}`
  if (item.source_episode_id !== null && item.source_episode_id !== undefined) return `episode:${item.source_episode_id}`
  for (const evidence of item.evidence ?? []) {
    if (!evidence || typeof evidence !== 'object') continue
    const value = evidence as unknown as Record<string, unknown>
    if (value.type === 'memory' || value.kind === 'memory') return `memory:${String(value.id ?? '未知')}`
    if (value.type === 'episode' || value.kind === 'episode') return `episode:${String(value.id ?? '未知')}`
  }
  return '未提供真实消息引用'
}

function dimensionLabel(dimension: string): string {
  return RADAR_DIMENSIONS.find(([key]) => key === dimension)?.[1] ?? dimension
}

function scopedObjectRef(item: RelationshipItem, scopeQuery?: ObjectRefScopeQuery): ObjectRefDescriptor | null {
  const objectRef = item.object_ref
  if (!objectRef?.ref?.trim()) return null
  if (objectRef.scope_query || !scopeQuery) return objectRef
  return {
    ...objectRef,
    scope_query: {
      ...scopeQuery,
      subject_principal_id: item.subject_principal_id,
    },
  }
}

function rawValueFor(item: RelationshipItem | undefined, dimension: string): { raw: number | null; min: number; max: number } {
  const definition = RADAR_DIMENSIONS.find(([key]) => key === dimension) ?? RADAR_DIMENSIONS[0]
  const [key, , min, max] = definition
  const raw = item?.values?.[key]?.effective_value
  return {
    raw: typeof raw === 'number' && Number.isFinite(raw) ? raw : null,
    min,
    max,
  }
}

/** SoulPage 使用的群级只读关系雷达；不包含任何关系写入口。 */
export function GroupRelationshipRadarCard({
  relationships,
  scopeQuery,
  relationshipLoading = false,
  relationshipError = null,
  onRetryRelationships,
  history = [],
  historyLoading = false,
  historyError = null,
  selectedSubjectId,
  selectedDimension,
  onSelectedSubjectIdChange,
  onSelectedDimensionChange,
  onRetryHistory,
}: GroupRelationshipRadarCardProps) {
  const candidates = relationships
  const series = useMemo<RelationshipSeries[]>(
    () => candidates.map((item, index) => ({
      item,
      seriesKey: `relationship_${index}`,
      color: SERIES_COLORS[index % SERIES_COLORS.length],
      points: relationshipRadarPoints(item.values),
    })),
    [candidates],
  )
  const chartSeries = useMemo(
    () => series.filter((entry) => entry.points.some((point) => point.raw !== null)),
    [series],
  )
  const [internalSelectedSubjectId, setInternalSelectedSubjectId] = useState<string | null>(null)
  const [internalSelectedDimension, setInternalSelectedDimension] = useState<RelationshipRadarDimension>('familiarity')
  // 用户可自主配置在雷达上叠加显示的对象 ID 集合；默认只勾选当前解释对象，避免默认乱成一团
  const [visibleSubjectIds, setVisibleSubjectIds] = useState<Set<string>>(new Set())

  useEffect(() => {
    const ids = new Set(chartSeries.map((entry) => entry.item.subject_principal_id))
    if (internalSelectedSubjectId && ids.has(internalSelectedSubjectId)) return
    const firstId = chartSeries[0]?.item.subject_principal_id ?? null
    setInternalSelectedSubjectId(firstId)
    if (firstId) {
      setVisibleSubjectIds((prev) => (prev.size > 0 ? prev : new Set([firstId])))
    }
  }, [chartSeries, internalSelectedSubjectId])

  const activeSubjectId = selectedSubjectId !== undefined ? selectedSubjectId : internalSelectedSubjectId
  const activeDimension = selectedDimension ?? internalSelectedDimension
  const activeSeries = chartSeries.find((entry) => entry.item.subject_principal_id === activeSubjectId) ?? chartSeries[0]
  const activeItem = activeSeries?.item
  const activeValue = rawValueFor(activeItem, activeDimension)
  const activeHistory = useMemo(
    () => history
      .filter((item) => item.dimension === activeDimension)
      .sort((a, b) => Number(b.timestamp ?? 0) - Number(a.timestamp ?? 0))
      .slice(0, 3),
    [activeDimension, history],
  )
  const visibleSeries = useMemo(() => {
    const activeSet = visibleSubjectIds.size > 0
      ? visibleSubjectIds
      : new Set(activeSubjectId ? [activeSubjectId] : [])
    return chartSeries.filter((entry) => activeSet.has(entry.item.subject_principal_id))
  }, [activeSubjectId, chartSeries, visibleSubjectIds])

  const chartData = useMemo(() => RADAR_DIMENSIONS.map(([key, label, min, max]) => {
    const row: Record<string, string | number | null> = { key, dimension: label, min, max }
    for (const entry of visibleSeries) {
      const point = entry.points.find((candidate) => candidate.key === key)
      row[entry.seriesKey] = point?.raw === null || point === undefined || !Number.isFinite(point.value) ? null : point.value
    }
    return row
  }), [visibleSeries])

  const chartConfig = useMemo<ChartConfig>(
    () => Object.fromEntries(visibleSeries.map((entry) => [entry.seriesKey, { label: displayName(entry.item), color: entry.color }])),
    [visibleSeries],
  )

  const selectSubject = (subjectPrincipalId: string) => {
    setInternalSelectedSubjectId(subjectPrincipalId)
    onSelectedSubjectIdChange?.(subjectPrincipalId)
    // 点击查看时确保该对象也在雷达上显示
    setVisibleSubjectIds((prev) => new Set([...prev, subjectPrincipalId]))
  }

  const toggleVisibleSubject = (subjectPrincipalId: string) => {
    setVisibleSubjectIds((prev) => {
      const next = new Set(prev)
      if (next.has(subjectPrincipalId)) {
        if (next.size > 1) next.delete(subjectPrincipalId)
      } else {
        next.add(subjectPrincipalId)
      }
      return next
    })
  }

  const selectOnlyCurrent = () => {
    if (activeSubjectId) {
      setVisibleSubjectIds(new Set([activeSubjectId]))
    }
  }

  const selectAll = () => {
    setVisibleSubjectIds(new Set(chartSeries.map((entry) => entry.item.subject_principal_id)))
  }

  const selectDimension = (dimension: RelationshipRadarDimension) => {
    setInternalSelectedDimension(dimension)
    onSelectedDimensionChange?.(dimension)
  }

  return (
    <div data-slot="group-relationship-radar-card" className="rounded-xl border bg-card p-4 text-sm">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <RadarIcon className="size-4 text-primary" />
            <span>本群关系分布（只读对照）</span>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">勾选群友可在雷达图上自由叠加对比；点击可查看对应维度的事件历史。未建立的维度不画成 0。</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-[11px]" onClick={selectOnlyCurrent}>只看当前</Button>
          <Button type="button" size="sm" variant="outline" className="h-6 px-2 text-[11px]" onClick={selectAll}>全部叠加</Button>
          <Badge variant="outline">不含校准写入口</Badge>
        </div>
      </div>

      {relationshipLoading ? (
        <div className="rounded-xl border border-dashed bg-muted/10 p-8 text-center text-sm text-muted-foreground">
          正在读取本群关系对照…
        </div>
      ) : relationshipError ? (
        <div className="rounded-xl border border-destructive/20 bg-destructive/5 p-4 text-sm text-destructive" role="alert">
          <div className="flex items-center gap-2"><AlertCircleIcon className="size-4" />{relationshipError}</div>
          {onRetryRelationships ? <Button type="button" size="sm" variant="outline" className="mt-3 h-8 text-xs" onClick={onRetryRelationships}>重试关系对照</Button> : null}
        </div>
      ) : !chartSeries.length ? (
        <div className="rounded-xl border border-dashed bg-muted/10 p-8 text-center text-sm text-muted-foreground">
          当前群还没有可对照的五维关系记录。
        </div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(260px,0.75fr)]">
          <div className="min-w-0">
            <ChartContainer config={chartConfig} className="mx-auto aspect-square max-h-[360px] w-full">
              <RadarChart data={chartData} cx="50%" cy="50%" outerRadius="68%">
                <PolarGrid strokeDasharray="3 3" className="stroke-border/60" />
                <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11 }} className="fill-muted-foreground" />
                <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
                <ChartTooltip content={<ChartTooltipContent />} />
                {visibleSeries.map((entry) => (
                    <Radar
                      key={entry.seriesKey}
                      dataKey={entry.seriesKey}
                      name={displayName(entry.item)}
                      stroke={entry.color}
                      fill={entry.color}
                      fillOpacity={0.15}
                      strokeWidth={activeSubjectId === entry.item.subject_principal_id ? 2.5 : 1.5}
                      connectNulls={false}
                      isAnimationActive={false}
                    />
                ))}
              </RadarChart>
            </ChartContainer>

            <div className="mt-2 flex max-h-72 flex-col gap-2 overflow-y-auto" role="listbox" aria-label="本群关系对象">
              {chartSeries.map((entry) => {
                const subjectId = entry.item.subject_principal_id
                const isVisible = visibleSubjectIds.has(subjectId)
                const selected = activeSubjectId === subjectId
                const objectRef = scopedObjectRef(entry.item, scopeQuery)
                return (
                  <div key={subjectId} className={`flex flex-wrap items-center gap-2 rounded-lg border px-2.5 py-1.5 transition-colors ${selected ? 'border-primary/40 bg-primary/5' : 'bg-muted/10'}`}>
                    <label className="inline-flex items-center gap-2 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        aria-label={`在雷达中叠加 ${displayName(entry.item)}`}
                        checked={isVisible}
                        onChange={() => toggleVisibleSubject(subjectId)}
                        className="size-3.5 rounded border-muted-foreground/40 accent-primary"
                      />
                      <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: entry.color }} />
                    </label>
                    <button
                      type="button"
                      role="option"
                      aria-selected={selected}
                      aria-label={`查看 ${displayName(entry.item)} 关系雷达`}
                      className="inline-flex min-w-0 flex-1 items-center gap-1.5 text-left text-xs"
                      onClick={() => selectSubject(subjectId)}
                    >
                      <span className={`truncate ${selected ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}>{displayName(entry.item)}</span>
                      {selected ? <Badge variant="secondary" className="text-[10px]">当前解释</Badge> : null}
                    </button>
                    <Button type="button" size="sm" variant={selected ? 'secondary' : 'ghost'} className="h-6 px-1.5 text-[10px]" onClick={() => selectSubject(subjectId)}>
                      选择
                    </Button>
                    {objectRef?.ref?.trim() ? (
                      <ObjectDeepLink to="/people" objectRef={objectRef} ariaLabel={`打开 ${displayName(entry.item)} 的人物关系页`}>
                        人物页
                      </ObjectDeepLink>
                    ) : (
                      <span className="text-[10px] text-muted-foreground">未签发对象引用</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div className="flex min-w-0 flex-col gap-3">
            <div>
              <p className="mb-1.5 text-xs font-semibold">选择维度查看最近事件摘要</p>
              <div className="flex flex-wrap gap-1.5" role="group" aria-label="关系维度解释">
                {RADAR_DIMENSIONS.map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={activeDimension === key}
                    className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${activeDimension === key ? 'border-primary bg-primary/10 font-semibold text-primary' : 'text-muted-foreground hover:bg-muted'}`}
                    onClick={() => selectDimension(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div className="rounded-lg border bg-muted/10 p-3">
              <p className="text-xs text-muted-foreground">当前解释对象</p>
              <p className="mt-1 truncate font-semibold">{activeItem ? displayName(activeItem) : '未选择群友'}</p>
              <p className="mt-2 text-xs text-muted-foreground">{dimensionLabel(activeDimension)} · 正式值</p>
              <p className="mt-1 font-mono text-lg">{activeValue.raw === null ? '未建立' : `${formatDisplayNumber(activeValue.raw)}（范围 ${activeValue.min} ~ ${activeValue.max}）`}</p>
            </div>

            <div className="rounded-lg border bg-muted/10 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <p className="text-xs font-semibold">最近 3 条正式事件</p>
                <Badge variant="outline">只读</Badge>
              </div>
              {historyLoading ? (
                <p className="rounded border border-dashed p-4 text-center text-xs text-muted-foreground">正在读取事件摘要…</p>
              ) : historyError ? (
                <div className="rounded border border-destructive/20 bg-destructive/5 p-3 text-xs text-destructive" role="alert">
                  <div className="flex items-center gap-2"><AlertCircleIcon className="size-3.5" />{historyError}</div>
                  {onRetryHistory ? <Button type="button" size="sm" variant="outline" className="mt-2 h-7 text-[11px]" onClick={onRetryHistory}>重试事件摘要</Button> : null}
                </div>
              ) : activeHistory.length ? (
                <div className="grid gap-2">
                  {activeHistory.map((item) => (
                    <div key={item.id} className="rounded border bg-background/70 px-2.5 py-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge variant={item.kind === 'manual' ? 'secondary' : 'outline'} className="text-[10px]">{item.kind === 'manual' ? '人工' : '自动'}</Badge>
                        {item.event_type ? <Badge variant="outline" className="max-w-full truncate text-[10px]">{item.event_type}</Badge> : null}
                        <span className="text-[10px] text-muted-foreground">{formatTime(item.timestamp)} · revision {item.revision ?? '未记录'}</span>
                      </div>
                      <p className="mt-1 text-xs font-medium">{item.reason || '服务端未提供原因'}</p>
                      <p className="mt-1 text-[10px] text-muted-foreground">来源：{sourceText(item)}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="rounded border border-dashed p-4 text-center text-xs text-muted-foreground">暂无该维度正式事件。</p>
              )}
            </div>

            <p className="text-[10px] leading-relaxed text-muted-foreground">
              这里只读展示当前群关系对照；完整事件流与人工校准请通过人物页查看。
              {activeItem?.object_ref?.ref?.trim() ? <span className="ml-1">请使用上方人物页深链。</span> : null}
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

export default GroupRelationshipRadarCard

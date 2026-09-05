import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart } from 'recharts'

import type { RelationshipItem } from '@/api/people'
import { ChartContainer, ChartTooltip, type ChartConfig } from '@/components/ui/chart'
import { formatDisplayNumber } from '@/lib/format-number'

export const RADAR_DIMENSIONS = [
  ['familiarity', '熟悉度', 0, 100],
  ['trust', '信任', -50, 100],
  ['fun', '趣味', 0, 80],
  ['hostility', '敌意', 0, 100],
  ['depth', '深度', 0, 80],
] as const

export type RelationshipRadarDimension = (typeof RADAR_DIMENSIONS)[number][0]

export interface RelationshipRadarPoint {
  key: RelationshipRadarDimension
  dimension: string
  value: number
  raw: number | null
  min: number
  max: number
}

/** 按维度的正式范围归一化到雷达图 0–100；无效值保持不可用。 */
export function normalizeRelationshipRadarValue(value: unknown, min: number, max: number): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return Math.max(0, Math.min(100, Math.round(((value - min) / (max - min)) * 1000) / 10))
}

export function relationshipRadarPoints(values: RelationshipItem['values']): RelationshipRadarPoint[] {
  return RADAR_DIMENSIONS.map(([key, label, min, max]) => {
    const value = values?.[key]?.effective_value
    const raw = typeof value === 'number' && Number.isFinite(value) ? value : null
    const normalized = normalizeRelationshipRadarValue(raw, min, max)
    return { key, dimension: label, value: normalized ?? Number.NaN, raw, min, max }
  })
}

const radarChartConfig = {
  value: { label: '关系强度', color: 'var(--chart-4)' },
} satisfies ChartConfig

function RadarTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload?: { dimension: string; raw: number | null; min: number; max: number } }> }) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  return (
    <div className="rounded-lg border bg-background px-2.5 py-1.5 text-xs shadow-sm">
      <span className="font-medium">{point.dimension}</span>
      <span className="ml-2 font-mono">{point.raw === null ? '未建立' : `${formatDisplayNumber(point.raw)}（范围 ${point.min} ~ ${point.max}）`}</span>
    </div>
  )
}

/** 单人五维关系快照（操作位）；与 SoulPage 只读对照雷达共用维度定义。 */
export function RelationshipRadarCard({ values }: { values: RelationshipItem['values'] }) {
  const points = relationshipRadarPoints(values)
  if (!points.some((point) => point.raw !== null)) return null

  return (
    <div className="rounded-lg border bg-muted/10 p-3.5">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-xs font-semibold">五维关系快照</span>
        <span className="text-[10px] text-muted-foreground">轴按各维度范围归一化到 0–100</span>
      </div>
      <ChartContainer config={radarChartConfig} className="mx-auto aspect-square max-h-60 w-full">
        <RadarChart data={points} cx="50%" cy="50%" outerRadius="68%">
          <PolarGrid strokeDasharray="3 3" className="stroke-border/60" />
          <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11 }} className="fill-muted-foreground" />
          <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
          <Radar dataKey="value" stroke="var(--chart-4)" fill="var(--chart-4)" fillOpacity={0.25} connectNulls={false} isAnimationActive={false} />
          <ChartTooltip content={<RadarTooltip />} />
        </RadarChart>
      </ChartContainer>
    </div>
  )
}

export default RelationshipRadarCard

import { RotateCcwIcon, ZapIcon, InfoIcon, AlertTriangleIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Field } from '@/components/ui/field'
import { cn } from '@/lib/utils'

export type ApplyMode = 'hot' | 'restart' | 'next-run' | 'unknown'

export interface FieldValueStateProps {
  label: string
  defaultValue?: unknown
  savedValue?: unknown
  effectiveValue?: unknown
  applyMode?: ApplyMode | null
  effectiveSince?: string | number | null
  className?: string
}

function displayValue(value: unknown): string {
  if (value === undefined || value === null) return '未配置'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'string') return value || '空'
  if (typeof value === 'number' || typeof value === 'bigint') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const APPLY_LABELS: Record<ApplyMode, string> = {
  hot: '热生效',
  restart: '重启生效',
  // next_run 表示「不需要重启，保存后由运行时下次读取路径生效」。
  // 原文案“下次生效”容易被读成“下次重启才生效”，与事实不符。
  'next-run': '保存即生效',
  unknown: '未知生效',
}

/** 保存值与生效值不一致时，说明各自需要什么动作，避免拼出“需要保存即生效”这类病句。 */
const APPLY_PENDING_HINTS: Record<ApplyMode, string> = {
  hot: '需要应用热参数',
  restart: '需要重启 AstrBot',
  'next-run': '会在运行时下次读取配置时生效',
  unknown: '需要确认生效方式',
}

export function FieldValueState({
  label,
  defaultValue,
  savedValue,
  effectiveValue,
  applyMode,
  effectiveSince,
  className,
}: FieldValueStateProps) {
  const mode = applyMode ?? 'unknown'
  const differs = savedValue !== undefined && effectiveValue !== undefined && !Object.is(savedValue, effectiveValue)

  return (
    <Field data-slot="field-value-state" className={cn('rounded-xl border border-border/40 bg-muted/10 p-3.5', className)}>
      <div className="flex items-center justify-between gap-3 mb-2.5">
        <span className="text-xs font-semibold text-foreground/80 truncate">{label}</span>
        <Badge variant={mode === 'hot' ? 'default' : mode === 'restart' ? 'secondary' : 'outline'} className="text-[10px] h-5 py-0 px-2 shrink-0">
          {mode === 'hot'
            ? <ZapIcon className="size-3 mr-1" aria-hidden="true" />
            : <RotateCcwIcon className="size-3 mr-1" aria-hidden="true" />}
          {APPLY_LABELS[mode]}
        </Badge>
      </div>

      <div className="grid gap-2 grid-cols-3 text-[11px] leading-normal font-mono mb-2">
        <div className="flex flex-col gap-0.5 rounded-lg bg-background/50 p-2 border border-border/20">
          <span className="text-muted-foreground scale-95 origin-left">默认</span>
          <span className="truncate text-foreground/80">{displayValue(defaultValue)}</span>
        </div>
        <div className="flex flex-col gap-0.5 rounded-lg bg-background/50 p-2 border border-border/20">
          <span className="text-muted-foreground scale-95 origin-left">已保存</span>
          <span className="truncate text-foreground/80">{displayValue(savedValue)}</span>
        </div>
        <div className={cn(
          "flex flex-col gap-0.5 rounded-lg p-2 border",
          differs 
            ? "bg-amber-500/5 border-amber-500/20 text-amber-600" 
            : "bg-background/50 border-border/20 text-foreground/80"
        )}>
          <span className="text-muted-foreground scale-95 origin-left">当前生效</span>
          <span className="truncate font-semibold">{displayValue(effectiveValue)}</span>
        </div>
      </div>

      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground mt-1 leading-normal">
        {differs ? (
          <>
            <AlertTriangleIcon className="size-3 text-amber-500 shrink-0" />
            <span className="text-amber-600">已保存值与当前生效值不同；{APPLY_PENDING_HINTS[mode]}。</span>
          </>
        ) : (
          <>
            <InfoIcon className="size-3 text-muted-foreground/60 shrink-0" />
            <span>已保存与生效值完全同步。{effectiveSince ? `自 ${effectiveSince} 起。` : ''}</span>
          </>
        )}
      </div>
    </Field>
  )
}

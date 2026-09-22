import type { ReactNode } from 'react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export interface HeroMetricItem {
  label: string
  value: ReactNode
  tone?: string
  description?: string
}

export function HeroMetric({ label, value, tone, description }: HeroMetricItem) {
  return (
    <div className="rounded-lg border bg-background/80 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className={cn('mt-1 text-2xl font-semibold tabular-nums', tone)}>{value}</div>
      {description ? <p className="mt-1 text-xs text-muted-foreground">{description}</p> : null}
    </div>
  )
}

export interface HeroHeaderProps {
  icon: ReactNode
  title: ReactNode
  description?: ReactNode
  badge?: ReactNode
  metrics?: HeroMetricItem[]
  children?: ReactNode
  className?: string
}

/**
 * 统一的页面顶部 Hero 看板组件
 * 封装带渐变底色、主题图标、标题、受控徽章与指标方块的卡片结构
 */
export function HeroHeader({
  icon,
  title,
  description,
  badge,
  metrics,
  children,
  className,
}: HeroHeaderProps) {
  return (
    <Card
      className={cn(
        'overflow-hidden border-primary/10 bg-gradient-to-br from-primary/5 via-card to-card',
        className,
      )}
    >
      <CardHeader className="gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex gap-3">
          <div className="rounded-xl bg-primary/10 p-3 text-primary">
            {icon}
          </div>
          <div>
            {typeof title === 'string' ? (
              <CardTitle className="text-xl">{title}</CardTitle>
            ) : (
              title
            )}
            {description ? (
              <CardDescription className="mt-1 max-w-3xl">
                {description}
              </CardDescription>
            ) : null}
          </div>
        </div>
        {badge ? <div className="flex items-center gap-2">{badge}</div> : null}
      </CardHeader>
      {metrics?.length || children ? (
        <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {metrics
            ? metrics.map((item, idx) => (
                <HeroMetric
                  key={`${item.label}:${idx}`}
                  label={item.label}
                  value={item.value}
                  tone={item.tone}
                  description={item.description}
                />
              ))
            : children}
        </CardContent>
      ) : null}
    </Card>
  )
}

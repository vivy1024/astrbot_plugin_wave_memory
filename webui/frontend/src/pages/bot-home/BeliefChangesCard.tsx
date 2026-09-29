import { Link } from 'react-router-dom'
import { ArrowRightIcon, SproutIcon } from 'lucide-react'

import type { BotHomeBeliefs } from '@/api/botHome'
import { QueryState } from '@/components/shared/QueryState'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { humanizeReason } from '@/lib/reason-label'
import { cn } from '@/lib/utils'

function changeBadgeClass(status: string): string {
  if (status === 'formed' || status === 'stable') return 'border-primary/30 bg-primary/10 text-primary'
  if (status === 'shaken') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  if (status === 'vetoed') return 'border-destructive/30 bg-destructive/10 text-destructive'
  return 'border-border text-muted-foreground'
}

function formatDay(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return ''
  const date = new Date(seconds * 1000)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

/** Bot 主页小卡片：她最近形成或改变的看法（信念 v2 的状态变化）。 */
export function BeliefChangesCard({ section, botId, onRetry }: { section: BotHomeBeliefs | undefined; botId: string; onRetry: () => void }) {
  const items = section?.items ?? []
  const allHref = `/beliefs?bot_id=${encodeURIComponent(botId)}`
  let body
  if (!section) {
    body = <p className="text-sm text-muted-foreground">后端还没有提供这一块。</p>
  } else if (section.status === 'error') {
    body = <QueryState status="error" title="这一块暂时读不到" description={humanizeReason(section.reason_code, '服务端没有返回这一块的数据。')} onRetry={onRetry} />
  } else if (section.status === 'unavailable') {
    body = <p className="text-sm text-muted-foreground">看法数据还没准备好。</p>
  } else if (!items.length) {
    body = (
      <QueryState
        status="empty"
        emptyTitle="还没有形成看法"
        emptyDescription="开启「信念反思」后，她会在每天写完日记后回想这段时间的经历，慢慢形成自己的看法。"
      />
    )
  } else {
    body = (
      <ul className="flex flex-col divide-y divide-border" data-testid="belief-changes">
        {items.map((item) => (
          <li key={item.event_id} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Badge variant="outline" className={cn(changeBadgeClass(item.to_status))}>{item.change_label}</Badge>
              <span className="ml-auto shrink-0">{formatDay(item.occurred_at)}</span>
            </div>
            <Link to={allHref} className="line-clamp-2 text-sm text-foreground/90 hover:text-primary">{item.content}</Link>
            {item.reason ? <p className="line-clamp-1 text-xs text-muted-foreground">{item.reason}</p> : null}
          </li>
        ))}
      </ul>
    )
  }
  return (
    <Card data-testid="bot-home-beliefs">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><SproutIcon className="size-4 text-primary" aria-hidden="true" />她最近形成或改变的看法</CardTitle>
        <CardDescription>
          {section?.status === 'ready' ? `现在有 ${section.held ?? 0} 条已形成的看法，聊到相关话题时会浮现` : '看法来自她每天日记之后的反思'}
        </CardDescription>
        <CardAction>
          <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
            <Link to={allHref}>全部看法<ArrowRightIcon data-icon="inline-end" aria-hidden="true" /></Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { Clock3Icon, HeartIcon, RefreshCwIcon, SearchIcon } from 'lucide-react'

import { getTimeAnchors, type TimeAnchorItem } from '@/api/soul'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'

export function TimeAnchorsExplorer({ botId }: { botId?: string }) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<TimeAnchorItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const limit = 20

  const loadAnchors = useCallback(async () => {
    setLoading(true)
    try {
      const res = await getTimeAnchors({
        bot_id: botId || undefined,
        search: search.trim() || undefined,
        limit,
        offset: (page - 1) * limit,
      })
      setItems(res.items || [])
      setTotal(res.total || res.items.length)
    } catch {
      setItems([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [botId, search, page])

  useEffect(() => {
    if (open) {
      loadAnchors()
    }
  }, [open, loadAnchors])

  const totalPages = Math.ceil(total / limit) || 1

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Clock3Icon className="mr-1.5 size-3.5" />
          探索全量时间锚点 ({total > 0 ? total : '2600+'})
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-3xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Clock3Icon className="size-5 text-primary" /> 全量时间锚点日志
          </DialogTitle>
          <DialogDescription>
            对话中触发的高情感权重重要关键节点日志（共 {total} 条）
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2 py-2">
          <div className="relative flex-1">
            <SearchIcon className="absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
            <Input
              placeholder="搜索事件描述..."
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(1)
              }}
              className="pl-8"
            />
          </div>
          <Button variant="ghost" size="icon" onClick={loadAnchors} disabled={loading}>
            <RefreshCwIcon className={`size-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto pr-1 space-y-3">
          {loading ? (
            Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="p-3 border rounded-lg space-y-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            ))
          ) : items.length === 0 ? (
            <p className="py-12 text-center text-sm text-muted-foreground">未找到相关时间锚点记录</p>
          ) : (
            <div className="ml-3 border-l-2 border-primary/20 space-y-4 pl-4 py-1">
              {items.map((item) => (
                <div key={item.id} className="relative group">
                  <span className="absolute -left-[21px] top-1.5 size-2.5 rounded-full border-2 border-background bg-primary group-hover:scale-125 transition-transform" />
                  <div className="rounded-lg border bg-card/60 p-3 shadow-sm hover:border-primary/40 transition-colors">
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-sm font-medium leading-normal">{item.event_summary}</p>
                      {item.emotional_weight ? (
                        <Badge variant="secondary" className="shrink-0 text-xs">
                          <HeartIcon className="mr-1 size-2.5 text-pink-500" />
                          {(item.emotional_weight * 100).toFixed(0)}%
                        </Badge>
                      ) : null}
                    </div>
                    <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
                      <span>{new Date(item.timestamp * 1000).toLocaleString('zh-CN')}</span>
                      {item.bot_id ? <Badge variant="outline" className="text-xs">{item.bot_id}</Badge> : null}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {totalPages > 1 ? (
          <div className="flex items-center justify-between border-t pt-3">
            <span className="text-xs text-muted-foreground">
              {page} / {totalPages} 页 (共 {total} 条)
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1 || loading}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages || loading}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页
              </Button>
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

import type { ReactNode } from 'react'
import { Loader2Icon, MessageSquareQuoteIcon } from 'lucide-react'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'

export interface ChatEvidenceMessage {
  id: number | string
  sender_name?: string | null
  sender_id?: string | null
  timestamp?: number
  content: string
  role: 'before' | 'anchor' | 'after' | string
}

export interface ChatContextEvidenceDialogProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  before: number
  after: number
  onBeforeChange: (val: number) => void
  onAfterChange: (val: number) => void
  onRefresh: () => void
  loading?: boolean
  error?: string
  statusBadges?: ReactNode
  sourceQuote?: string
  messages: ChatEvidenceMessage[]
  anchorBadgeLabel?: string
  emptyDescription?: string
  headerExtra?: ReactNode
  children?: ReactNode
}

function formatMessageTime(seconds?: number): string {
  if (!seconds || !Number.isFinite(seconds)) return '未记录时间'
  return new Date(seconds * 1000).toLocaleString('zh-CN')
}

/**
 * 统一的聊天证据还原气泡弹窗组件
 * 封装前/后文条数控制器、刷新交互、原话支撑高亮卡片与聊天消息流
 */
export function ChatContextEvidenceDialog({
  open,
  onClose,
  title,
  description,
  before,
  after,
  onBeforeChange,
  onAfterChange,
  onRefresh,
  loading = false,
  error = '',
  statusBadges,
  sourceQuote,
  messages,
  anchorBadgeLabel = '事实关联原话锚点',
  emptyDescription = '未关联到可安全还原的本群上下文记录。',
  headerExtra,
  children,
}: ChatContextEvidenceDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(isOpen) => { if (!isOpen) onClose() }}>
      <DialogContent className="flex h-[min(80vh,760px)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="border-b p-4 pr-12">
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>

        <div className="flex flex-wrap items-end gap-3 border-b bg-muted/30 p-3">
          <Field className="w-24 gap-1">
            <FieldLabel htmlFor="evidence-window-before">前文条数</FieldLabel>
            <Input
              id="evidence-window-before"
              type="number"
              min="0"
              max="50"
              value={before}
              onChange={(e) => onBeforeChange(Math.max(0, Math.min(50, Number(e.target.value) || 0)))}
            />
          </Field>
          <Field className="w-24 gap-1">
            <FieldLabel htmlFor="evidence-window-after">后文条数</FieldLabel>
            <Input
              id="evidence-window-after"
              type="number"
              min="0"
              max="50"
              value={after}
              onChange={(e) => onAfterChange(Math.max(0, Math.min(50, Number(e.target.value) || 0)))}
            />
          </Field>
          <Button
            type="button"
            size="sm"
            disabled={loading}
            onClick={onRefresh}
          >
            {loading ? (
              <Loader2Icon data-icon="inline-start" className="animate-spin" />
            ) : (
              <MessageSquareQuoteIcon data-icon="inline-start" />
            )}
            刷新证据
          </Button>

          {statusBadges ? (
            <div className="ml-auto flex flex-wrap gap-2">
              {statusBadges}
            </div>
          ) : null}

          {headerExtra}
        </div>

        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-3 p-4">
            {loading ? (
              <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2Icon className="animate-spin" />
                正在读取同作用域证据上下文
              </div>
            ) : null}

            {!loading && error ? (
              <Alert variant="destructive">
                <AlertTitle>证据读取失败</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}

            {sourceQuote ? (
              <div className="rounded-lg border border-primary/40 bg-primary/5 p-3 text-sm">
                <span className="font-semibold text-primary">提审支撑原话：</span>
                <p className="mt-1 whitespace-pre-wrap break-words italic">“{sourceQuote}”</p>
              </div>
            ) : null}

            {children}

            {!loading && messages.length
              ? messages.map((message) => (
                  <article
                    key={`${message.role}:${message.id}`}
                    data-evidence-role={message.role}
                    className={
                      message.role === 'anchor'
                        ? 'rounded-lg border border-primary/40 bg-primary/10 p-3 shadow-xs'
                        : 'rounded-lg border bg-card p-3'
                    }
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground/90">
                        {message.sender_name || message.sender_id || '未知发言者'}
                      </span>
                      <span>
                        {formatMessageTime(message.timestamp)}
                      </span>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">
                      {message.content}
                    </p>
                    {message.role === 'anchor' ? (
                      <Badge className="mt-2" variant="default">
                        {anchorBadgeLabel}
                      </Badge>
                    ) : null}
                  </article>
                ))
              : null}

            {!loading && !messages.length && !sourceQuote && !children ? (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                {emptyDescription}
              </div>
            ) : null}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}

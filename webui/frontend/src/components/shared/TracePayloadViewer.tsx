import { useMemo, useState } from 'react'
import { CheckIcon, CopyIcon, DownloadIcon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

export interface TracePayloadViewerProps {
  payload: unknown
  rawPayload?: string
  downloadName?: string
  maxHeightClassName?: string
  onCopy?: (content: string) => void | Promise<void>
  onDownload?: (content: string, fileName: string) => void
  className?: string
}

const SECTION_LABELS: Record<string, string> = {
  request: '请求',
  budget: '预算',
  channels: '通道',
  hits: '命中项',
  filtered: '过滤项',
  filtered_items: '过滤项',
  warnings: '警告',
  errors: '错误',
  final_text: '最终文本',
  final_injection_text: '最终文本',
}

function safeJson(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch (error) {
    return `无法序列化载荷：${error instanceof Error ? error.message : String(error)}`
  }
}

function defaultDownload(content: string, fileName: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'application/json;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

export function TracePayloadViewer({
  payload,
  rawPayload,
  downloadName = 'trace-payload.json',
  maxHeightClassName = 'h-[min(60vh,36rem)]',
  onCopy,
  onDownload = defaultDownload,
  className,
}: TracePayloadViewerProps) {
  const [copied, setCopied] = useState(false)
  const fullContent = rawPayload ?? safeJson(payload)
  const sections = useMemo(() => {
    if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) return []
    return Object.entries(payload)
      .filter(([key]) => key in SECTION_LABELS)
      .map(([key, value]) => ({ key, label: SECTION_LABELS[key], content: safeJson(value) }))
  }, [payload])

  const copy = async () => {
    if (onCopy) await onCopy(fullContent)
    else await navigator.clipboard.writeText(fullContent)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  const views = [...sections, { key: '__full__', label: '完整 JSON', content: fullContent }]

  return (
    <section data-slot="trace-payload-viewer" aria-label="注入载荷查看器" className={cn('flex min-w-0 flex-col gap-3 rounded-lg border bg-card p-3', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">分区仅用于阅读；复制和下载始终使用完整服务端内容，不截断尾部。</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => void copy()} aria-label="复制完整注入载荷">
            {copied ? <CheckIcon data-icon="inline-start" /> : <CopyIcon data-icon="inline-start" />}
            {copied ? '已复制' : '复制全部'}
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={() => onDownload(fullContent, downloadName)} aria-label="下载完整注入载荷">
            <DownloadIcon data-icon="inline-start" />
            下载全部
          </Button>
        </div>
      </div>
      <span className="sr-only" role="status" aria-live="polite">{copied ? '完整注入载荷已复制' : ''}</span>
      <Tabs defaultValue={views[0].key}>
        <div className="overflow-x-auto pb-1">
          <TabsList aria-label="注入载荷分区">
            {views.map((view) => <TabsTrigger key={view.key} value={view.key}>{view.label}</TabsTrigger>)}
          </TabsList>
        </div>
        {views.map((view) => (
          <TabsContent key={view.key} value={view.key}>
            <ScrollArea className={cn('w-full rounded-md border bg-muted/40', maxHeightClassName)}>
              <pre className="min-w-max p-4 font-mono text-sm leading-relaxed whitespace-pre-wrap break-words text-foreground" data-trace-section={view.key}>{view.content}</pre>
            </ScrollArea>
          </TabsContent>
        ))}
      </Tabs>
    </section>
  )
}

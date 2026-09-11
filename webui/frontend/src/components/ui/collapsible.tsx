"use client"

import * as React from "react"
import { Collapsible as CollapsiblePrimitive } from "radix-ui"
import { ChevronRightIcon } from "lucide-react"

import { cn } from "@/lib/utils"

function Collapsible({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.Root>) {
  return <CollapsiblePrimitive.Root data-slot="collapsible" {...props} />
}

function CollapsibleTrigger({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleTrigger>) {
  return (
    <CollapsiblePrimitive.CollapsibleTrigger
      data-slot="collapsible-trigger"
      {...props}
    />
  )
}

function CollapsibleContent({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleContent>) {
  return (
    <CollapsiblePrimitive.CollapsibleContent
      data-slot="collapsible-content"
      {...props}
    />
  )
}

export interface SettingsSectionProps {
  /** 分组标题 */
  title: string
  /** 该组字段数，显示在标题右侧 */
  count: number
  /** 该组是否已展开 */
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 展开后渲染的字段内容 */
  children: React.ReactNode
  className?: string
}

/**
 * 配置分组容器：标题一行可点开/收起，右侧显示该组字段数。
 * 收起时完全不渲染 children 的内容高度，避免长页面把滚动条撑满。
 */
export function SettingsSection({
  title,
  count,
  open,
  onOpenChange,
  children,
  className,
}: SettingsSectionProps) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} className={cn("rounded-xl border bg-card", className)}>
      <CollapsibleTrigger
        className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-label={`${open ? '收起' : '展开'} ${title}`}
      >
        <ChevronRightIcon
          className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
          aria-hidden="true"
        />
        <span className="text-sm font-semibold">{title}</span>
        <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">{count}</span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="border-t px-4 py-3">{children}</div>
      </CollapsibleContent>
    </Collapsible>
  )
}

export { Collapsible, CollapsibleTrigger, CollapsibleContent }

import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

export interface BatchActionBarProps {
  selectedCount: number
  onClear: () => void
  disabled?: boolean
  children?: ReactNode
  extra?: ReactNode
}

/**
 * 统一的批量操作条组件
 * 消除各业务页面在多选时重复手写 Badge、容器样式、取消按钮等逻辑
 */
export function BatchActionBar({
  selectedCount,
  onClear,
  disabled = false,
  children,
  extra,
}: BatchActionBarProps) {
  if (selectedCount <= 0) return null

  return (
    <div
      data-slot="batch-action-bar"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3 animate-in fade-in duration-200"
    >
      <Badge variant="secondary" className="px-2 py-1 text-xs">
        已选 {selectedCount} 条
      </Badge>
      <div className="flex flex-wrap items-center gap-2">
        {children}
      </div>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        disabled={disabled}
        className="ml-auto text-xs text-muted-foreground hover:text-foreground"
        onClick={onClear}
      >
        取消选择
      </Button>
      {extra ? <div className="w-full text-xs text-muted-foreground pt-1">{extra}</div> : null}
    </div>
  )
}

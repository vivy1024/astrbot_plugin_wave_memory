import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { useIsMobile } from '@/hooks/use-mobile'

/**
 * 列定义：只写一次「这一列怎么渲染」，
 * 框架自动产出桌面 Table 与移动端 Card 结构，杜绝重复 JSX。
 */
export interface DataColumn<T> {
  /** 列唯一 key */
  key: string
  /** 桌面端表头文字 */
  header?: ReactNode
  /** 列宽（桌面） */
  width?: string
  /** 移动端卡片标题占位列（取这一列的值作为卡片头部） */
  isTitle?: boolean
  /** 渲染该列数据，返回 ReactNode */
  render: (row: T, rowIndex: number) => ReactNode
  /** 附加 className（桌面单元格 / 移动端项） */
  className?: string
  /** 移动端隐藏这一列（例如 Actions） */
  hideOnMobile?: boolean
}

export interface DeclarativeDataTableProps<T> {
  /** ARIA 区域标签 */
  label: string
  /** 数据源 */
  items: T[]
  /** 行的唯一 key */
  keyExtractor: (row: T) => string
  /** 列定义（只需写一次） */
  columns: Array<DataColumn<T>>
  /** 桌面端行点击 */
  onRowClick?: (row: T) => void
  /** 当前选中行 id 集合（用于多选高亮） */
  selectedKeys?: Set<string>
  /** 多选切换 */
  onToggleSelect?: (row: T) => void
  /** 当前页全选 */
  onToggleAll?: (checked: boolean) => void
  /** 空状态 */
  emptyState?: ReactNode
  /** 卡片尾部操作区 */
  renderCardActions?: (row: T) => ReactNode
  /** 卡片头部辅助信息（小字） */
  renderCardSubtitle?: (row: T) => ReactNode
  /** 渲染行/卡片附加 className */
  rowClassName?: (row: T) => string | undefined
  className?: string
}

/** 声明式响应式数据表格 —— 一份列定义，自动生成桌面表格与移动端卡片，杜绝重复 JSX 与双倍渲染。 */
export function DeclarativeDataTable<T>({
  label,
  items,
  keyExtractor,
  columns,
  onRowClick,
  selectedKeys,
  onToggleSelect,
  onToggleAll,
  emptyState,
  renderCardActions,
  renderCardSubtitle,
  rowClassName,
  className,
}: DeclarativeDataTableProps<T>) {
  const isMobile = useIsMobile()

  if (items.length === 0) {
    return emptyState ? <>{emptyState}</> : null
  }

  const visibleColumns = columns.filter((col) => !col.hideOnMobile)
  const titleColumn = visibleColumns.find((col) => col.isTitle) ?? visibleColumns[0]
  const subtitleColumns = visibleColumns.filter((col) => col !== titleColumn && !col.hideOnMobile)
  const allSel = items.length > 0 && items.every((row) => selectedKeys?.has(keyExtractor(row)))

  if (isMobile) {
    return (
      <section
        aria-label={label}
        data-slot="responsive-table"
        data-responsive-table="cards"
        data-mode="cards"
        className={cn('flex min-w-0 flex-col gap-3', className)}
      >
        {items.map((row, i) => (
          <article
            key={keyExtractor(row)}
            className={cn(
              'flex flex-col gap-3 rounded-lg border bg-card p-4',
              selectedKeys?.has(keyExtractor(row)) && 'border-primary/50 bg-primary/5',
              rowClassName?.(row),
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                {onToggleSelect && (
                  <label className="mb-1 flex items-center gap-2">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${label}`}
                      checked={selectedKeys?.has(keyExtractor(row)) ?? false}
                      onChange={() => onToggleSelect(row)}
                    />
                    <span className="text-xs text-muted-foreground">选中此行</span>
                  </label>
                )}
                <div className="font-medium">{titleColumn.render(row, i)}</div>
                {renderCardSubtitle?.(row)}
              </div>
              {!onToggleSelect && subtitleColumns
                .filter((c) => c.header != null)
                .map((col) => (
                  <Badge key={col.key} variant="outline" className="shrink-0 text-xs">
                    {col.render(row, i)}
                  </Badge>
                ))}
            </div>
            {subtitleColumns.length > 0 && (
              <dl className="grid gap-2 text-sm">
                {subtitleColumns
                  .filter((col) => col.header != null)
                  .map((col) => (
                    <div key={col.key} className="flex min-w-0 gap-2">
                      <dt className="shrink-0 text-muted-foreground">{col.header}</dt>
                      <dd className="min-w-0 break-words">{col.render(row, i)}</dd>
                    </div>
                  ))}
              </dl>
            )}
            {(() => {
              const actionCols = columns.filter((c) => c.header === null)
              const asideActions = actionCols.length > 0
              return (
                <div className={cn('flex flex-wrap justify-end gap-2', !asideActions && !renderCardActions && 'hidden')}>
                  {asideActions && actionCols.map((col) => <span key={col.key}>{col.render(row, i)}</span>)}
                  {renderCardActions?.(row)}
                </div>
              )
            })()}
          </article>
        ))}
      </section>
    )
  }

  return (
    <div role="region" aria-label={label} data-slot="responsive-table" data-responsive-table="table" data-mode="table" className={cn('overflow-x-auto rounded-lg border', className)}>
      <Table>
        <TableHeader>
          <TableRow>
            {onToggleSelect && (
              <TableHead className="w-10">
                <input type="checkbox" aria-label={`全选 ${label}`} checked={allSel} onChange={(e) => onToggleAll?.(e.target.checked)} />
              </TableHead>
            )}
            {columns.map((col) => (
              <TableHead key={col.key} className={cn(col.width && 'w-' + col.width, col.className)}>
                {col.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((row, i) => (
            <TableRow
              key={keyExtractor(row)}
              className={cn(
                onRowClick && 'cursor-pointer',
                selectedKeys?.has(keyExtractor(row)) && 'bg-primary/5',
                rowClassName?.(row),
              )}
              onClick={() => onRowClick?.(row)}
            >
              {onToggleSelect && (
                <TableCell>
                  <input
                    type="checkbox"
                    aria-label={`选择 ${label}`}
                    checked={selectedKeys?.has(keyExtractor(row)) ?? false}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => onToggleSelect(row)}
                  />
                </TableCell>
              )}
              {columns.map((col) => (
                <TableCell key={col.key} className={col.className}>
                  {col.render(row, i)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

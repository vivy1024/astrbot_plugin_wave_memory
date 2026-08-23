import type { InputHTMLAttributes, ReactNode } from 'react'
import { SearchIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'

export interface InputWithIconProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'size'> {
  icon?: ReactNode
  /** 外层容器 className */
  containerClassName?: string
  onValueChange?: (value: string) => void
  /** 同时支持 value 控制 input */
  value?: string
  onChange?: never
}

/** 带图标前缀的搜索输入框（自动定位，无需手写 Tailwind 对齐类） */
export function InputWithIcon({
  icon = <SearchIcon className="size-3.5" />,
  placeholder = '搜索…',
  containerClassName,
  onValueChange,
  value,
  ...props
}: InputWithIconProps) {
  return (
    <div className={cn('relative min-w-0 flex-1', containerClassName)}>
      <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground">{icon}</span>
      <Input
        aria-label={placeholder}
        className="h-8 pl-8"
        value={value ?? ''}
        onChange={(e) => onValueChange?.(e.target.value)}
        placeholder={placeholder}
        {...props}
      />
    </div>
  )
}

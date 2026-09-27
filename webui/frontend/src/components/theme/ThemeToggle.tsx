import { MonitorIcon, MoonIcon, SunIcon, type LucideIcon } from 'lucide-react'
import { useTheme } from 'next-themes'

import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

import { THEME_OPTIONS, isThemePreference, themeLabel, type ThemePreference } from './theme-init'

const THEME_ICONS: Record<ThemePreference, LucideIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: MonitorIcon,
}

/** 主题切换：浅色 / 深色 / 跟随系统。按钮图标反映当前偏好而不是解析后的结果。 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()
  const current: ThemePreference = isThemePreference(theme) ? theme : 'system'
  const Icon = THEME_ICONS[current]
  const label = `切换主题（当前：${themeLabel(current)}）`

  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={label} className={cn('text-muted-foreground hover:text-foreground', className)}>
              <Icon aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="top">主题：{themeLabel(current)}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent side="top" align="end" className="min-w-36">
        <DropdownMenuLabel>界面主题</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={current} onValueChange={(value) => { if (isThemePreference(value)) setTheme(value) }}>
          {THEME_OPTIONS.map((option) => {
            const OptionIcon = THEME_ICONS[option.value]
            return (
              <DropdownMenuRadioItem key={option.value} value={option.value}>
                <OptionIcon aria-hidden="true" />
                {option.label}
              </DropdownMenuRadioItem>
            )
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

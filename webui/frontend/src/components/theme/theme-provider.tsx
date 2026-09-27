import { ThemeProvider as NextThemesProvider } from 'next-themes'
import type { ReactNode } from 'react'

import { THEME_STORAGE_KEY } from './theme-init'

/** 全站主题：class 挂在 <html>，默认跟随系统，切换时禁用过渡避免闪烁。 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange storageKey={THEME_STORAGE_KEY}>
      {children}
    </NextThemesProvider>
  )
}

import { useEffect, useState } from 'react'

import type { GraphTheme } from '@/lib/graph-palette'

function readTheme(): GraphTheme {
  if (typeof document === 'undefined') return 'light'
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

/** next-themes 把主题写成 <html> 上的 .dark 类；直接监听它，切换主题时图谱颜色实时跟随。 */
export function useGraphTheme(): GraphTheme {
  const [theme, setTheme] = useState<GraphTheme>(readTheme)
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(readTheme()))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    setTheme(readTheme())
    return () => observer.disconnect()
  }, [])
  return theme
}

/** 宽屏（≥ 1024px）把详情放在图右侧，窄屏改用 Sheet。 */
export function useWideLayout(minWidth = 1024): boolean {
  const [wide, setWide] = useState(() => typeof window === 'undefined' || window.innerWidth >= minWidth)
  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= minWidth)
    onResize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [minWidth])
  return wide
}

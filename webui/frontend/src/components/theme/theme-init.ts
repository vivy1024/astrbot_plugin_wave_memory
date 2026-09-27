/**
 * 主题偏好的共享常量与首帧预应用。
 *
 * next-themes 在纯客户端渲染（非 SSR）下，它注入的防闪烁 <script> 不会被执行，
 * 真正把 class 写到 <html> 要等 React 挂载后的 effect。为避免刷新时先白后黑，
 * main.tsx 在 createRoot 之前同步调用 applyInitialTheme()，逻辑与 next-themes 保持一致：
 * attribute="class"、storageKey 相同、system 时按 prefers-color-scheme 解析。
 */
export const THEME_STORAGE_KEY = 'wavememory-theme'

export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_OPTIONS: ReadonlyArray<{ value: ThemePreference; label: string }> = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
]

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
}

export function themeLabel(value: string | undefined): string {
  return THEME_OPTIONS.find((option) => option.value === value)?.label ?? '跟随系统'
}

export function resolveTheme(preference: string | null | undefined, prefersDark: boolean): ResolvedTheme {
  if (preference === 'light' || preference === 'dark') return preference
  return prefersDark ? 'dark' : 'light'
}

export function applyInitialTheme(doc: Document = document, win: Window = window): ResolvedTheme {
  let stored: string | null = null
  try {
    stored = win.localStorage.getItem(THEME_STORAGE_KEY)
  } catch {
    // 隐私模式等场景下 localStorage 不可用，按跟随系统处理。
  }
  const prefersDark = Boolean(win.matchMedia?.('(prefers-color-scheme: dark)').matches)
  const resolved = resolveTheme(stored, prefersDark)
  const root = doc.documentElement
  root.classList.remove('light', 'dark')
  root.classList.add(resolved)
  root.style.colorScheme = resolved
  return resolved
}

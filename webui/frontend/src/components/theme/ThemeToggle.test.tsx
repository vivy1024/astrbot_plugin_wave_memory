import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'

import { ThemeToggle } from './ThemeToggle'
import { THEME_STORAGE_KEY, applyInitialTheme, resolveTheme } from './theme-init'
import { ThemeProvider } from './theme-provider'

function mockSystemDark(prefersDark: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('prefers-color-scheme: dark') ? prefersDark : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

/** Node 25 自带的 localStorage 会遮蔽 jsdom 的实现，这里与 auth-provider.test 一样换成内存 Storage。 */
function installMemoryStorage() {
  const values = new Map<string, string>()
  const storage = {
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => Array.from(values.keys())[index] ?? null,
    get length() { return values.size },
    removeItem: (key: string) => { values.delete(key) },
    setItem: (key: string, value: string) => { values.set(key, String(value)) },
  } satisfies Storage
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
}

function resetRoot() {
  document.documentElement.classList.remove('light', 'dark')
  document.documentElement.style.colorScheme = ''
}

describe('主题首帧预应用', () => {
  beforeEach(() => {
    installMemoryStorage()
    resetRoot()
  })

  it('偏好解析：显式偏好优先，system 跟随系统', () => {
    expect(resolveTheme('dark', false)).toBe('dark')
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme(null, false)).toBe('light')
  })

  it('无存储偏好且系统为深色时，在 html 上加 .dark', () => {
    mockSystemDark(true)
    expect(applyInitialTheme()).toBe('dark')
    expect(document.documentElement).toHaveClass('dark')
    expect(document.documentElement.style.colorScheme).toBe('dark')
  })

  it('存储了浅色时，即使系统为深色也保持浅色', () => {
    mockSystemDark(true)
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    expect(applyInitialTheme()).toBe('light')
    expect(document.documentElement).toHaveClass('light')
    expect(document.documentElement).not.toHaveClass('dark')
  })
})

describe('ThemeToggle', () => {
  beforeEach(() => {
    installMemoryStorage()
    resetRoot()
    mockSystemDark(false)
  })

  it('在浅色 / 深色 / 跟随系统之间切换，并写入 html class 与本地存储', async () => {
    const user = userEvent.setup()
    render(
      <ThemeProvider>
        <TooltipProvider>
          <ThemeToggle />
        </TooltipProvider>
      </ThemeProvider>,
    )

    const trigger = screen.getByRole('button', { name: '切换主题（当前：跟随系统）' })
    await waitFor(() => expect(document.documentElement).toHaveClass('light'))

    await user.click(trigger)
    await user.click(await screen.findByRole('menuitemradio', { name: '深色' }))
    await waitFor(() => expect(document.documentElement).toHaveClass('dark'))
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(screen.getByRole('button', { name: '切换主题（当前：深色）' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '切换主题（当前：深色）' }))
    await user.click(await screen.findByRole('menuitemradio', { name: '浅色' }))
    await waitFor(() => expect(document.documentElement).not.toHaveClass('dark'))
    expect(document.documentElement).toHaveClass('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')

    await user.click(screen.getByRole('button', { name: '切换主题（当前：浅色）' }))
    const systemItem = await screen.findByRole('menuitemradio', { name: '跟随系统' })
    await user.click(systemItem)
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('system')
    expect(screen.getByRole('button', { name: '切换主题（当前：跟随系统）' })).toBeInTheDocument()
  })
})

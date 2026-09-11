import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ScrollArea } from '@/components/ui/scroll-area'

describe('ScrollArea', () => {
  // jsdom 不做真实布局，无法测量 scrollHeight，
  // 这里锁住修复底部白屏所需的那对类名。
  it('Root 裁剪溢出，内容高度不会泄漏到文档层造成底部白屏', () => {
    render(
      <ScrollArea className="h-[calc(100svh-3.5rem)]" data-testid="area">
        <div style={{ height: 4000 }} />
      </ScrollArea>,
    )
    const root = screen.getByTestId('area')
    expect(root).toHaveClass('overflow-hidden')
    expect(root).toHaveClass('relative')
  })

  // Root 只有 max-h-* 时没有确定高度，viewport 的 height:100% 会解析成内容高度，
  // 导致完全滚不动；内联 max-height:inherit 让 viewport 继承同一上限，从而恢复滚动。
  // 必须是内联样式：max-h-[inherit] 工具类会被解析成 none，实测无效。
  it('viewport 内联继承 max-height，避免 max-h 型滚动区滚不动', () => {
    render(
      <ScrollArea className="max-h-[55vh]" data-testid="area">
        <div style={{ height: 4000 }} />
      </ScrollArea>,
    )
    const viewport = screen.getByTestId('area').querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
    expect(viewport).not.toBeNull()
    expect(viewport?.style.maxHeight).toBe('inherit')
    expect(viewport).toHaveClass('size-full')
  })
})

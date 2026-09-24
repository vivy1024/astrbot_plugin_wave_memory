import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ConfigInventoryPage } from '@/pages/settings/ConfigInventoryPage'

vi.mock('@/api/configInventory', () => ({
  getConfigInventory: vi.fn().mockResolvedValue({
    revision: 'inv-1',
    precedence: ['builtin', 'static', 'bot', 'override'],
    counts: { builtin: 1, override: 1 },
    items: [
      { key: 'hot:ingress.debounce_seconds', layer: 'override', title: '合并等待', scope: 'global', default: 4, saved: 2.5, effective: 2.5, source: 'wavememory_db.config_overrides', apply_mode: 'hot', changed: true, warning: null },
      { key: 'hot:spike.max_hops', layer: 'builtin', title: '最大跳数', scope: 'global', default: 4, saved: 4, effective: 4, source: 'builtin_default', apply_mode: 'hot', changed: false, warning: null },
    ],
    suspects: [{ key: 'Query_Settings.enable_auto_inject', message: '自动注入：默认开启，当前保存为关闭。' }],
  }),
}))

describe('ConfigInventoryPage', () => {
  it('默认只显示改过的项，并提示被覆盖的开关', async () => {
    const user = userEvent.setup()
    render(<ConfigInventoryPage />)
    expect(await screen.findByText('hot:ingress.debounce_seconds')).toBeVisible()
    expect(screen.queryByText('hot:spike.max_hops')).not.toBeInTheDocument()
    expect(screen.getByText(/默认开启的开关被保存成了关闭/)).toBeVisible()
    await user.click(screen.getByRole('switch', { name: '只看改过的' }))
    expect(screen.getByText('hot:spike.max_hops')).toBeVisible()
  })
})

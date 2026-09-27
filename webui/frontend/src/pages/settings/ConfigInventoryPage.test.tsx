import { MemoryRouter } from 'react-router-dom'
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
      { key: 'channel:jargon.enabled', layer: 'override', title: '注入通道 jargon · enabled', scope: 'global', default: true, saved: null, effective: false, source: 'Channel_Settings', apply_mode: 'hot', changed: true, warning: null },
      { key: 'WebUI_Settings.webui_password', layer: 'static', title: '访问登录密码', scope: 'global', default: '', saved: 'hunter2', effective: 'hunter2', source: 'plugin_config', apply_mode: 'next_run', changed: true, warning: null },
      { key: 'hot:spike.max_hops', layer: 'builtin', title: '最大跳数', scope: 'global', default: 4, saved: 4, effective: 4, source: 'builtin_default', apply_mode: 'hot', changed: false, warning: null },
    ],
    suspects: [{ key: 'Query_Settings.enable_auto_inject', message: '自动注入：默认开启，当前保存为关闭。' }],
  }),
}))

describe('ConfigInventoryPage', () => {
  it('默认只显示改过的项，并提示被覆盖的开关', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter><ConfigInventoryPage /></MemoryRouter>)
    expect(await screen.findByText('hot:ingress.debounce_seconds')).toBeVisible()
    expect(screen.queryByText('hot:spike.max_hops')).not.toBeInTheDocument()
    expect(screen.getByText(/默认开启的开关被保存成了关闭/)).toBeVisible()
    await user.click(screen.getByRole('switch', { name: '只看改过的' }))
    expect(screen.getByText('hot:spike.max_hops')).toBeVisible()
  })

  it('每行「去修改」按共享定位规则跳到真实编辑位置；嵌入时交给宿主处理', async () => {
    const user = userEvent.setup()
    const { unmount } = render(<MemoryRouter><ConfigInventoryPage /></MemoryRouter>)
    expect(await screen.findByRole('link', { name: '去修改 hot:ingress.debounce_seconds' })).toHaveAttribute('href', '/settings?key=ingress.debounce_seconds&tab=hot')
    expect(screen.getByRole('link', { name: '去修改 channel:jargon.enabled' })).toHaveAttribute('href', '/channels?channel=jargon&field=enabled')
    // 密码不渲染明文
    expect(screen.queryByText('hunter2')).not.toBeInTheDocument()
    expect(screen.getByText('已设置（已隐藏）')).toBeVisible()
    unmount()

    const onLocate = vi.fn()
    render(<MemoryRouter><ConfigInventoryPage onLocate={onLocate} /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: '去修改 channel:jargon.enabled' }))
    expect(onLocate).toHaveBeenCalledWith(expect.objectContaining({ target: 'channels', params: { channel: 'jargon', field: 'enabled' } }))
  })
})

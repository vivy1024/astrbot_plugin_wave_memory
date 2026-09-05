import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { setViewport } from '@/test/setup'
import { EvidenceList } from '../EvidenceList'
import { FieldValueState } from '../FieldValueState'
import { ResponsiveDetail } from '../ResponsiveDetail'
import { OperationStatus, QualityDecisionBadge } from '../StatusIndicators'
import { TracePayloadViewer } from '../TracePayloadViewer'

const evidence = [{
  type: 'message',
  id: 'message-1',
  content_hash: 'sha256:abc',
  source_scope: 'bot:yushu/session:group:42',
  availability: 'available' as const,
  object_ref: { ref: 'opaque-evidence-ref' },
}]

describe('响应式键盘详情', () => {
  it('桌面 Dialog 支持 Escape 关闭并恢复触发器焦点', async () => {
    setViewport(1024)
    const user = userEvent.setup()
    render(<ResponsiveDetail title="桌面详情" description="描述">正文</ResponsiveDetail>)
    const trigger = screen.getByRole('button', { name: '查看详情' })

    await user.click(trigger)
    expect(await screen.findByRole('dialog')).toHaveAttribute('data-responsive-detail', 'dialog')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('窄屏 EvidenceList 降级为卡片与键盘 Sheet，不渲染桌面表格', async () => {
    setViewport(375)
    const user = userEvent.setup()
    render(<MemoryRouter><EvidenceList evidence={evidence} objectPath="/memories" /></MemoryRouter>)

    await waitFor(() => expect(document.querySelector('[data-evidence-layout="mobile"]')).toBeInTheDocument())
    expect(document.querySelector('[data-evidence-layout="table"]')).not.toBeInTheDocument()
    const trigger = screen.getByRole('button', { name: '查看详情' })
    await user.click(trigger)
    expect(await screen.findByRole('dialog')).toHaveAttribute('data-responsive-detail', 'sheet')
    expect(screen.getByText('sha256:abc')).toBeVisible()
    expect(screen.getByRole('link', { name: '打开这条证据' })).toHaveAttribute('href', '/memories?ref=opaque-evidence-ref')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('缺少 evidence 数组时显示内嵌空状态而不是崩溃', () => {
    setViewport(1024)
    render(<MemoryRouter><EvidenceList evidence={undefined} /></MemoryRouter>)
    expect(screen.getByText('当前没有可展示的证据。')).toBeInTheDocument()
    expect(document.querySelector('[data-evidence-layout="empty"]')).toBeInTheDocument()
  })

  it('对象 source_scope 显示 bot 与 session，不渲染 [object Object]', () => {
    setViewport(1024)
    render(
      <MemoryRouter>
        <EvidenceList
          evidence={[{
            type: 'memory',
            id: '12',
            source_scope: { bot_id: 'yushu', session: { id: '羽书:group:42' }, visibility: 'group' },
            captured_at: 1_700_000_000,
            summary: '群友说了句谢谢',
            availability: 'available',
          }]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('群友说了句谢谢')).toBeInTheDocument()
    expect(screen.getByText('yushu · 羽书:group:42 · group')).toBeInTheDocument()
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
  })

  it('管道拼接的 source_scope 拆成可读群名', () => {
    setViewport(1024)
    render(
      <MemoryRouter>
        <EvidenceList
          evidence={[{
            type: 'memory',
            id: '323370',
            source_scope: 'yushu|羽书:group:398291136|group',
            availability: 'available',
          }]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('yushu · 羽书:group:398291136 · group')).toBeInTheDocument()
    expect(screen.queryByText('yushu|羽书:group:398291136|group')).not.toBeInTheDocument()
  })
})

describe('Trace 与状态展示', () => {
  it('复制/下载使用完整原始载荷且完整视图保留尾部', async () => {
    setViewport(1024)
    const user = userEvent.setup()
    const onCopy = vi.fn()
    const onDownload = vi.fn()
    const raw = JSON.stringify({ request: { prompt: 'hello' }, final_text: `body-${'x'.repeat(50_100)}-TAIL-MARKER` })
    render(<TracePayloadViewer payload={JSON.parse(raw)} rawPayload={raw} onCopy={onCopy} onDownload={onDownload} />)

    await user.click(screen.getByRole('button', { name: '复制完整注入载荷' }))
    expect(onCopy).toHaveBeenCalledWith(raw)
    expect(await screen.findByText('完整注入载荷已复制')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '下载完整注入载荷' }))
    expect(onDownload).toHaveBeenCalledWith(raw, 'trace-payload.json')

    await user.click(screen.getByRole('tab', { name: '完整 JSON' }))
    const fullView = screen.getByText(/TAIL-MARKER/)
    expect(fullView).toBeInTheDocument()
    expect(fullView.textContent).toBe(raw)
  })

  it('显式 false、saved/effective 差异、quality 与 operation 均有文字语义', () => {
    render(
      <>
        <FieldValueState label="关键开关" defaultValue savedValue={false} effectiveValue applyMode="restart" />
        <QualityDecisionBadge decision="quarantine" reasonCode="scope_unresolved" />
        <OperationStatus status="rolled_back" operationId="op-7" revision="rev-2" />
      </>,
    )

    expect(screen.getByText('false')).toBeVisible()
    expect(screen.getByText(/已保存值与当前生效值不同/)).toBeVisible()
    expect(screen.getByText(/质量门：隔离/)).toBeVisible()
    expect(screen.getByText(/操作：已回滚/)).toBeVisible()
    expect(screen.getByText(/op-7/)).toBeVisible()
  })

  it('EvidenceList 真实空态不创建替代证据', () => {
    render(<MemoryRouter><EvidenceList evidence={[]} /></MemoryRouter>)
    expect(screen.getByText('当前没有可展示的证据。')).toBeVisible()
    expect(document.querySelector('[data-evidence-layout="empty"]')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})

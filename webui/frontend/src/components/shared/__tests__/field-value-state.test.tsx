import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { FieldValueState } from '../FieldValueState'

describe('FieldValueState 生效方式文案', () => {
  it('next_run 表示不需要重启，不写成“下次生效”', () => {
    render(<FieldValueState label="显示标签图谱图例" defaultValue savedValue effectiveValue applyMode="next-run" />)
    expect(screen.getByText('保存即生效')).toBeDefined()
    // 旧文案会被读成“下次重启才生效”，与事实不符
    expect(screen.queryByText('下次生效')).toBeNull()
  })

  it('restart 与 hot 各自保留明确语义', () => {
    const { unmount } = render(<FieldValueState label="端口" defaultValue={1} savedValue={1} effectiveValue={1} applyMode="restart" />)
    expect(screen.getByText('重启生效')).toBeDefined()
    unmount()
    render(<FieldValueState label="注入条数" defaultValue={1} savedValue={1} effectiveValue={1} applyMode="hot" />)
    expect(screen.getByText('热生效')).toBeDefined()
  })

  it('保存值与生效值不一致时给出可执行提示，不拼出病句', () => {
    render(<FieldValueState label="图例数量" defaultValue={10} savedValue={20} effectiveValue={10} applyMode="next-run" />)
    const hint = screen.getByText(/已保存值与当前生效值不同/)
    expect(hint.textContent).toContain('会在运行时下次读取配置时生效')
    // “需要保存即生效”是旧实现拼接出来的病句
    expect(hint.textContent).not.toContain('需要保存即生效')
  })

  it('重启类配置的待生效提示必须点名重启', () => {
    render(<FieldValueState label="端口" defaultValue={1} savedValue={2} effectiveValue={1} applyMode="restart" />)
    expect(screen.getByText(/已保存值与当前生效值不同/).textContent).toContain('重启')
  })
})

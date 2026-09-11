import { MemoryRouter } from 'react-router-dom'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { groupSessionOptions, scopeOptionsFor, type ScopeOptionsPayload } from '@/api/options'
import { buildObjectDeepLink } from '@/lib/object-deep-link'
import { ObjectDeepLink } from '../ObjectDeepLink'
import { QueryState } from '../QueryState'
import { ScopeSelect } from '../ScopeSelect'

const options = [
  { value: 'bot:yushu', label: '羽书', kind: 'bot' as const },
  { value: 'session:group:42', label: '群 42', kind: 'session' as const },
]

describe('ScopeSelect 与 ObjectDeepLink', () => {
  it('只呈现异步返回的真实 scoped options', async () => {
    const onChange = vi.fn()
    const loadOptions = vi.fn(async () => options)
    const user = userEvent.setup()
    render(<ScopeSelect loadOptions={loadOptions} onValueChange={onChange} />)

    const trigger = await screen.findByRole('combobox', { name: '作用域' })
    await user.click(trigger)
    await user.click(screen.getByRole('option', { name: '羽书' }))

    expect(onChange).toHaveBeenCalledWith('bot:yushu', options[0])
    expect(screen.queryByRole('option', { name: '白真真' })).not.toBeInTheDocument()
  })

  it('群会话选项显示群名与群号，无群名时回退到群号', () => {
    const payload: ScopeOptionsPayload = {
      bots: [],
      sessions: [
        {
          id: 'qq:group:42',
          bot_id: 'yushu',
          platform_id: 'qq',
          kind: 'group',
          conversation_id: '42',
          group_name: '记忆实验群',
          label: '记忆实验群（42）',
        },
        {
          id: 'qq:group:99',
          bot_id: 'yushu',
          platform_id: 'qq',
          kind: 'group',
          conversation_id: '99',
          label: '99',
        },
      ],
      channels: [],
      generated_at: 0,
      source: { health: 'healthy', reason_code: null },
    }

    expect(scopeOptionsFor(payload, ['session']).map((item) => item.label)).toEqual(['记忆实验群（42）', '99'])
  })

  it('group-only 页面会禁用私聊 session，并保留群会话', () => {
    const payload: ScopeOptionsPayload = {
      bots: [],
      sessions: [
        {
          id: '羽书:group:42',
          bot_id: 'yushu',
          platform_id: '羽书',
          kind: 'group',
          conversation_id: '42',
          label: '42',
        },
        {
          id: '羽书:private:1',
          bot_id: 'yushu',
          platform_id: '羽书',
          kind: 'private',
          conversation_id: '1',
          label: '1',
        },
      ],
      channels: [],
      generated_at: 0,
      source: { health: 'healthy', reason_code: null },
    }
    const options = groupSessionOptions(scopeOptionsFor(payload, ['session']), 'yushu')
    expect(options).toHaveLength(2)
    expect(options[0]).toMatchObject({ value: '羽书:group:42', disabled: false })
    expect(options[1]?.disabled).toBe(true)
    expect(options[1]?.description).toContain('本页只支持群会话')
  })

  it('触发器只显示单行标签，不把下拉说明带进固定高度输入框', async () => {
    const describedOptions = [
      { value: 'bot:yushu', label: '羽书', description: 'Bot ID yushu · QQ 250', kind: 'bot' as const },
    ]
    render(<ScopeSelect value="bot:yushu" loadOptions={async () => describedOptions} onValueChange={() => undefined} />)

    const trigger = await screen.findByRole('combobox', { name: '作用域' })
    await waitFor(() => expect(trigger).toHaveTextContent('羽书'))
    expect(trigger).not.toHaveTextContent('Bot ID yushu')
  })

  it('规范 Scope 默认值回填时保持受控，不触发 Radix uncontrolled 警告', async () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { rerender } = render(<ScopeSelect loadOptions={async () => options} onValueChange={() => undefined} />)

    await screen.findByRole('combobox', { name: '作用域' })
    rerender(<ScopeSelect value="bot:yushu" loadOptions={async () => options} onValueChange={() => undefined} />)
    await waitFor(() => expect(screen.getByRole('combobox', { name: '作用域' })).toHaveTextContent('羽书'))

    expect(warning.mock.calls.flat().join(' ')).not.toContain('uncontrolled')
    warning.mockRestore()
  })

  it('真实 options 为空时禁用选择且明确显示 empty', async () => {
    render(<ScopeSelect loadOptions={async () => []} onValueChange={() => undefined} />)

    expect(await screen.findByText('当前真实为空，请先检查 Bot 与会话来源配置。')).toBeVisible()
    expect(screen.getByRole('combobox', { name: '作用域' })).toBeDisabled()
  })

  it('深链序列化 ref、locator 与服务端 scope query，不接受默认 scope', () => {
    const href = buildObjectDeepLink('/beliefs?view=active', {
      ref: 'signed:scope/version+token', kind: 'belief', locator: 7, scope_key: 'bot:yushu',
      scope_query: { bot_id: 'bot-yushu', session_id: 'qq:group:42', visibility: 'group' }, version: 3,
    }, 'evidence', 'trace-7')
    expect(href).toContain('ref=signed%3Ascope%2Fversion%2Btoken')
    expect(href).toContain('object_id=7')
    expect(href).toContain('bot_id=bot-yushu')
    expect(href).toContain('session_id=qq%3Agroup%3A42')
    expect(href).toContain('visibility=group')
    expect(href).toContain('trace_id=trace-7')
    expect(new URLSearchParams(href.split('?')[1]).has('id')).toBe(false)

    render(
      <MemoryRouter>
        <ObjectDeepLink to="/beliefs" objectRef={{ ref: 'opaque-ref' }}>打开信念</ObjectDeepLink>
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: '打开信念' })).toHaveAttribute('href', '/beliefs?ref=opaque-ref')
  })

  it('scope mismatch 显示可恢复 not-found 而不是生成链接', () => {
    render(
      <MemoryRouter>
        <ObjectDeepLink to="/beliefs" objectRef={{ ref: 'opaque-ref' }} state="scope-mismatch" />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('不会用裸编号或默认 Bot 猜测')
  })
})

describe('QueryState', () => {
  it('区分真实 empty、error 与 unknown', async () => {
    const { rerender } = render(<QueryState status="empty" />)
    expect(screen.getByText('当前真实为空')).toBeVisible()

    // 空态不得复用错误风格标题；只传 error 标题时，empty 显示中性文案
    rerender(<QueryState status="empty" title="事实读取失败" emptyTitle="当前群没有正式事实" />)
    expect(screen.getByText('当前群没有正式事实')).toBeVisible()
    expect(screen.queryByText('事实读取失败')).not.toBeInTheDocument()

    rerender(<QueryState status="error" error={new Error('API unavailable')} onRetry={() => undefined} />)
    expect(screen.getByRole('alert')).toHaveTextContent('接口暂时不可用')
    expect(screen.getByRole('button', { name: '重试' })).toBeVisible()

    rerender(<QueryState status="unknown" />)
    await waitFor(() => expect(screen.getByText('状态未知')).toBeVisible())
    expect(screen.getByText(/不等同于空数据或成功/)).toBeVisible()
  })
})

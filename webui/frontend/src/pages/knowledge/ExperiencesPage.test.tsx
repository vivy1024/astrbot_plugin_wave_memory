import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ExperiencesPage } from './ExperiencesPage'

const api = vi.hoisted(() => ({ listExperiences: vi.fn(), getScopeOptions: vi.fn() }))

vi.mock('@/api/experiences', () => ({
  listExperiences: api.listExperiences,
}))

vi.mock('@/api/options', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/options')>()
  return {
    ...actual,
    getScopeOptions: api.getScopeOptions,
  }
})

beforeEach(() => {
  api.getScopeOptions.mockResolvedValue({
    bots: [{ db_id: 'bot-a', name: 'Bot A', status: 'active' }],
    sessions: [{ id: 'qq:group:1', bot_id: 'bot-a', platform_id: 'qq', kind: 'group', conversation_id: '1', label: '群 1' }],
    channels: [],
    generated_at: 1,
    source: { health: 'healthy', reason_code: null },
  })
  api.listExperiences.mockReset()
})

describe('ExperiencesPage 证据链展示', () => {
  it('未选择 Scope 时不请求经历，并提示空态', async () => {
    render(<MemoryRouter initialEntries={['/knowledge/experiences']}><ExperiencesPage /></MemoryRouter>)

    expect(await screen.findByText('请先选择 Bot 和群，不会加载全部 Bot 的经历。')).toBeVisible()
    expect(api.listExperiences).not.toHaveBeenCalled()
  })

  it('展示 episode 证据链定位，不把它当成事实或注入通道', async () => {
    api.listExperiences.mockResolvedValue({
      items: [{
        id: 42,
        bot_id: 'bot-a',
        group_id: '1',
        user_id: 'u1',
        episode_type: 'shared_event',
        trigger_text: '一起排查死锁',
        outcome: '问题暂未解决',
        source_memory_ids: [11, 12],
        reflection_candidate: true,
        created_at: 1720000000,
      }],
      page: { total: 1, total_status: 'exact', reason_code: null, limit: 18, offset: 0, page: 1, page_count: 1, has_more: false },
    })

    render(<MemoryRouter initialEntries={['/knowledge/experiences?bot_id=bot-a&session_id=qq%3Agroup%3A1']}><ExperiencesPage /></MemoryRouter>)

    expect(await screen.findByText('共同经历')).toBeVisible()
    expect(screen.getByText(/结构化群经历与反思证据/)).toBeVisible()
    expect(screen.getByText('episode:42')).toBeVisible()
    expect(screen.getByText('来源 memory: 11, 12')).toBeVisible()
    expect(screen.getByText('反思候选')).toBeVisible()
    expect(screen.getByText(/也不会作为独立通道注入/)).toBeVisible()
  })
})

import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RelationshipItem } from '@/api/people'
import { getSoulState } from '@/api/soul'
import { GroupRelationshipRadarCard } from '@/components/relationship/GroupRelationshipRadarCard'
import { RelationshipRadarCard, relationshipRadarPoints } from '@/components/relationship/RelationshipRadarCard'
import { RelationshipTrajectoryCard } from '@/components/relationship/RelationshipTrajectoryCard'

vi.mock('@/api/soul', () => ({
  getSoulState: vi.fn(),
  refreshSoulState: vi.fn(),
}))

vi.mock('@/components/ui/chart', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/chart')>()
  return {
    ...actual,
    ChartContainer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    ChartLegend: () => null,
    ChartLegendContent: () => null,
    ChartTooltip: () => null,
    ChartTooltipContent: () => null,
  }
})

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    CartesianGrid: () => null,
    Line: () => null,
    LineChart: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    PolarAngleAxis: ({ dataKey }: { dataKey: string }) => <div data-testid="polar-angle-axis" data-key={dataKey} />,
    PolarGrid: () => null,
    PolarRadiusAxis: (props: { type?: string; domain?: unknown; allowDataOverflow?: boolean }) => (
      <div data-testid="polar-radius-axis" data-type={props.type} data-domain={JSON.stringify(props.domain)} data-allow-overflow={String(Boolean(props.allowDataOverflow))} />
    ),
    Radar: () => null,
    RadarChart: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    Tooltip: () => null,
    XAxis: () => null,
    YAxis: () => null,
  }
})

const getSoulStateMock = vi.mocked(getSoulState)

function valuePayload(effective: number) {
  return {
    dimension: 'x',
    automatic_value: effective,
    manual_adjustment: null,
    manual_override: null,
    effective_value: effective,
    relationship_revision: 3,
    evidence: [],
  }
}

describe('RelationshipRadarCard 五维快照', () => {
  it('渲染五维快照卡片与极轴数据', () => {
    render(
      <RelationshipRadarCard
        values={{
          familiarity: valuePayload(50),
          trust: valuePayload(25),
          fun: valuePayload(40),
          hostility: valuePayload(0),
          depth: valuePayload(80),
        }}
      />,
    )

    expect(screen.getByText('五维关系快照')).toBeInTheDocument()
    expect(screen.getByText('轴按各维度范围归一化到 0–100')).toBeInTheDocument()
    expect(screen.getByTestId('polar-angle-axis')).toHaveAttribute('data-key', 'dimension')
    expect(screen.getByTestId('polar-radius-axis')).toHaveAttribute('data-type', 'number')
    expect(screen.getByTestId('polar-radius-axis')).toHaveAttribute('data-domain', '[0,100]')
    expect(screen.getByTestId('polar-radius-axis')).toHaveAttribute('data-allow-overflow', 'true')
  })

  it('全部未建立时不渲染雷达图', () => {
    const { container } = render(<RelationshipRadarCard values={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('未建立维度不画成 0', () => {
    const points = relationshipRadarPoints({
      familiarity: valuePayload(50),
      trust: valuePayload(25),
    } as never)
    const missing = points.find((point) => point.key === 'fun')
    expect(missing?.raw).toBeNull()
    expect(Number.isFinite(missing?.value ?? 0)).toBe(false)
  })
})

describe('GroupRelationshipRadarCard 群级只读对照', () => {
  function relationship(name: string, userId: string, ref: string, values = { familiarity: valuePayload(50), trust: valuePayload(25), fun: valuePayload(40), hostility: valuePayload(0), depth: valuePayload(20) }): RelationshipItem {
    return {
      subject_principal_id: `qq:user:${userId}`,
      person: { user_id: userId, display_name: name } as never,
      affinity: 10,
      state: 'known',
      revision: 3,
      values,
      evidence: [],
      object_ref: { ref, kind: 'relationship', locator: `qq:user:${userId}` },
      calibration: { available: true, reason_code: null },
    }
  }

  it('叠加多位群友雷达并显示当前维度最近三条正式事件', async () => {
    const history = [
      { id: 'h4', dimension: 'trust', timestamp: 1720000400, reason: '第四条', kind: 'automatic', action: null, revision: 4, source_memory_id: 14, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
      { id: 'h3', dimension: 'trust', timestamp: 1720000300, reason: '第三条', kind: 'automatic', action: null, revision: 3, source_memory_id: 13, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
      { id: 'h2', dimension: 'trust', timestamp: 1720000200, reason: '第二条', kind: 'manual', action: 'adjust', revision: 2, source_memory_id: null, source_episode_id: 12, operation_id: null, actor: 'admin', evidence: [], after: null },
      { id: 'h1', dimension: 'trust', timestamp: 1720000100, reason: '第一条', kind: 'automatic', action: null, revision: 1, source_memory_id: 11, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
    ] as never

    render(
      <MemoryRouter>
        <GroupRelationshipRadarCard
          relationships={[relationship('群友甲', 'u1', 'rel-u1'), relationship('群友乙', 'u2', 'rel-u2')]}
          scopeQuery={{ bot_id: 'bot-a', session_id: 'qq:group:g1', visibility: 'group' }}
          history={history}
        />
      </MemoryRouter>,
    )

    expect(screen.getByText('本群关系分布（只读对照）')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '查看 群友甲 关系雷达' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '查看 群友乙 关系雷达' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '打开 群友甲 的人物关系页' })).toHaveAttribute('href', expect.stringContaining('/people'))

    fireEvent.click(screen.getByRole('button', { name: '信任' }))
    expect(screen.getByText('第四条')).toBeInTheDocument()
    expect(screen.queryByText('第一条')).not.toBeInTheDocument()
    expect(screen.queryByText('导致变化的真实来源')).not.toBeInTheDocument()
  })

  it('最近正式事件丢掉路过噪音，半径轴锁死 0–100', () => {
    const history = [
      { id: 'noise-1', dimension: 'familiarity', timestamp: 1720000400, reason: '看见一条群友消息', event_type: 'message_seen', kind: 'automatic', action: null, revision: 4, source_memory_id: null, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
      { id: 'noise-2', dimension: 'familiarity', timestamp: 1720000300, reason: '消息带来趣味感', event_type: 'joke', kind: 'automatic', action: null, revision: 3, source_memory_id: null, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
      { id: 'real-1', dimension: 'familiarity', timestamp: 1720000200, reason: '连续直接互动后更熟了', event_type: 'direct_reply', kind: 'automatic', action: null, revision: 2, source_memory_id: 12, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: null },
    ] as never

    render(
      <MemoryRouter>
        <GroupRelationshipRadarCard
          relationships={[relationship('群友甲', 'u1', 'rel-u1')]}
          history={history}
        />
      </MemoryRouter>,
    )

    expect(screen.getByTestId('polar-radius-axis')).toHaveAttribute('data-domain', '[0,100]')
    expect(screen.getByText('连续直接互动后更熟了')).toBeInTheDocument()
    expect(screen.queryByText('看见一条群友消息')).not.toBeInTheDocument()
    expect(screen.queryByText('消息带来趣味感')).not.toBeInTheDocument()
  })

  it('名单展示全部传入对象，雷达只解释当前选中的人，缺少对象引用时不伪造人物页链接', () => {
    const relationships = Array.from({ length: 13 }, (_, index) => relationship(`群友${index + 1}`, `u${index + 1}`, index === 0 ? '' : `rel-u${index + 1}`))
    render(<MemoryRouter><GroupRelationshipRadarCard relationships={relationships} /></MemoryRouter>)

    expect(screen.getByText('群友13')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '查看 群友1 关系雷达' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('未签发对象引用')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开 群友1 的人物关系页' })).not.toBeInTheDocument()
  })
})

describe('RelationshipTrajectoryCard 五维同图', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('按时间合并五维轨迹并提供维度开关', async () => {
    getSoulStateMock.mockResolvedValue({
      relationship_history: {
        items: [
          { id: 2, dimension: 'trust', timestamp: 1720000100, delta: 1, reason: '被感谢', kind: 'automatic', action: null, revision: 2, source_memory_id: null, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: { automatic_value: 11, manual_adjustment: null, manual_override: null, effective_value: 11 } },
          { id: 1, dimension: 'familiarity', timestamp: 1720000000, delta: 1, reason: '日常聊天', kind: 'automatic', action: null, revision: 1, source_memory_id: null, source_episode_id: null, operation_id: null, actor: null, evidence: [], after: { automatic_value: 5, manual_adjustment: null, manual_override: null, effective_value: 5 } },
        ],
      },
    } as never)

    render(<RelationshipTrajectoryCard botId="bot-a" sessionId="qq:group:g1" subjectPrincipalId="qq:user:u1" />)

    await waitFor(() => expect(getSoulStateMock).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByRole('group', { name: '轨迹维度开关' })).toBeInTheDocument())

    expect(screen.getAllByText('熟悉度').length).toBeGreaterThan(0)
    expect(screen.getAllByText('信任').length).toBeGreaterThan(0)
    expect(screen.getByText('导致变化的真实来源')).toBeInTheDocument()
  })

  it('当前作用域没有五维轨迹时展示空态', async () => {
    getSoulStateMock.mockResolvedValue({ relationship_history: { items: [] } } as never)

    render(<RelationshipTrajectoryCard botId="bot-a" sessionId="qq:group:g1" subjectPrincipalId="qq:user:u1" />)

    await waitFor(() => expect(screen.getByText('还没有五维轨迹数据。')).toBeInTheDocument())
  })
})

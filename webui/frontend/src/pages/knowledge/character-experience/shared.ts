import type { CharacterJob, TimelineStage } from '@/api/characterExperience'

export const number = new Intl.NumberFormat('zh-CN')
export const selectClass = 'h-8 rounded-md border bg-background px-2 text-xs text-foreground'

export function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

export function when(ts: number | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function stageLabel(stages: TimelineStage[], key: string): string {
  if (!key) return '未分阶段'
  const stage = stages.find((item) => item.stage_key === key)
  return stage ? `${stage.ordinal}. ${stage.title}` : `${key}（不在时间线里）`
}

export function csv(values: string[] | undefined): string {
  return (values ?? []).join('、')
}

export function fromCsv(text: string): string[] {
  return text.split(/[,，、]/).map((item) => item.trim()).filter(Boolean)
}

/** 开始任务时返回 409 confirmation_required：服务端附上最新计划，界面换成新计划让管理员重新确认 */
export function conflictPlan<T>(reason: unknown): T | null {
  const payload = (reason as { payload?: unknown } | null)?.payload
  if (payload && typeof payload === 'object' && 'plan' in payload) return (payload as { plan: T }).plan
  return null
}

/** 任务还在跑（排队 / 运行 / 等待取消）：页面要定时刷新进度 */
export function jobActive(job: CharacterJob): boolean {
  return job.status === 'pending' || job.status === 'running' || job.status === 'cancel_requested'
}

export const JOB_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  succeeded: '已完成',
  failed: '失败',
}

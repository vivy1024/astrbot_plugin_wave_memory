import { fetchJson } from './client'
import type { PageResponse } from '@/components/shared/types'

export interface ExperienceEpisode {
  id: number
  bot_id?: string
  group_id?: string
  user_id?: string
  episode_type?: string
  trigger_text?: string | null
  bot_inner_thought?: string | null
  bot_action?: string | null
  bot_reply?: string | null
  user_reaction?: string | null
  outcome?: string | null
  source_memory_ids?: string | null
  emotional_weight?: number | null
  created_at?: number | null
}

export interface ListExperiencesParams {
  bot_id?: string
  group_id?: string
  search?: string
  episode_type?: string
  min_emotional_weight?: number
  limit?: number
  offset?: number
  signal?: AbortSignal
}

export function listExperiences(params: ListExperiencesParams = {}): Promise<PageResponse<ExperienceEpisode>> {
  const query = new URLSearchParams()
  if (params.bot_id) query.set('bot_id', params.bot_id)
  if (params.group_id) query.set('group_id', params.group_id)
  if (params.search) query.set('search', params.search)
  if (params.episode_type) query.set('episode_type', params.episode_type)
  if (params.min_emotional_weight !== undefined) query.set('min_emotional_weight', String(params.min_emotional_weight))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.offset !== undefined) query.set('offset', String(params.offset))

  return fetchJson<PageResponse<ExperienceEpisode>>(
    `/api/knowledge/experiences?${query.toString()}`,
    { signal: params.signal },
  )
}

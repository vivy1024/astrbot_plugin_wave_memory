import { useCallback } from 'react'
import { EyeIcon, EyeOffIcon, WavesIcon } from 'lucide-react'

import type { TagGraphLayer } from '@/api/tagGraph'
import { getScopeOptions, groupSessionOptions, scopeOptionsFor } from '@/api/options'
import { ScopeSelect } from '@/components/shared'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'

const LAYER_LABELS: Record<TagGraphLayer, string> = {
  cooccurrence: '有向共现',
  relations: '显式关系',
}

export interface TagGraphControlsProps {
  botId: string
  sessionId: string
  layers: TagGraphLayer[]
  includePulse: boolean
  loading?: boolean
  onScopeChange: (value: { botId?: string; sessionId?: string }) => void
  onLayersChange: (layers: TagGraphLayer[]) => void
  onPulseChange: (enabled: boolean) => void
}

export function TagGraphControls({ botId, sessionId, layers, includePulse, loading, onScopeChange, onLayersChange, onPulseChange }: TagGraphControlsProps) {
  const loadBots = useCallback(async () => scopeOptionsFor(await getScopeOptions(), ['bot']), [])
  const loadSessions = useCallback(async () => {
    return groupSessionOptions(scopeOptionsFor(await getScopeOptions(), ['session']), botId)
  }, [botId])
  const toggleLayer = (layer: TagGraphLayer) => {
    onLayersChange(layers.includes(layer) ? layers.filter((item) => item !== layer) : [...layers, layer])
  }

  return <Card className="border-border/60"><CardContent className="flex flex-col gap-3 p-4">
    <div className="flex flex-wrap items-end gap-3">
      <Badge variant="outline" className="mb-1">当前群</Badge>
      <ScopeSelect className="min-w-48 flex-1 xl:max-w-64" value={botId || undefined} loadOptions={loadBots} label="Bot" placeholder="选择 Bot" required onValueChange={(value) => onScopeChange({ botId: value, sessionId: '' })} />
      <ScopeSelect className="min-w-56 flex-[1.3] xl:max-w-80" value={sessionId || undefined} loadOptions={loadSessions} label="群 / 会话" placeholder="选择该 Bot 的群" disabled={!botId} required onValueChange={(value) => onScopeChange({ sessionId: value })} />
      <span className="pb-2 text-[10px] text-muted-foreground">只读 · 当前群</span>
    </div>
    <div className="flex flex-wrap items-center gap-2 border-t pt-3" aria-label="标签图层">
      {(Object.keys(LAYER_LABELS) as TagGraphLayer[]).map((layer) => {
        const visible = layers.includes(layer)
        return <Button key={layer} type="button" size="sm" variant={visible ? 'secondary' : 'outline'} aria-pressed={visible} disabled={loading} onClick={() => toggleLayer(layer)}>{visible ? <EyeIcon aria-hidden="true" /> : <EyeOffIcon aria-hidden="true" />}{LAYER_LABELS[layer]}</Button>
      })}
      <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground"><WavesIcon className="size-4" aria-hidden="true" />脉冲能量<Switch checked={includePulse} disabled={loading} onCheckedChange={onPulseChange} aria-label="显示脉冲能量" /></label>
    </div>
  </CardContent></Card>
}

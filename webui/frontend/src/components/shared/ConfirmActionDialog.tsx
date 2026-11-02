/* oxlint-disable react/only-export-components -- 确认框组件与其状态 hook 成对导出 */
import { useCallback, useState, type ReactNode } from 'react'
import { Loader2Icon } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { humanizeApiError } from '@/lib/reason-label'

export interface ConfirmAction {
  title: string
  description: ReactNode
  label: string
  destructive?: boolean
  run: () => Promise<void> | void
}

export interface ConfirmActionDialogProps {
  action: ConfirmAction | null
  running: boolean
  onCancel: () => void
  onConfirm: () => void
}

/** 破坏性或高影响操作前的统一确认框：执行中禁止关闭，避免重复提交。 */
export function ConfirmActionDialog({ action, running, onCancel, onConfirm }: ConfirmActionDialogProps) {
  return (
    <Dialog open={Boolean(action)} onOpenChange={(open) => { if (!open && !running) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{action?.title}</DialogTitle>
          <DialogDescription asChild={typeof action?.description !== 'string'}>
            {typeof action?.description === 'string' ? action.description : <div>{action?.description}</div>}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={running} onClick={onCancel}>取消</Button>
          <Button type="button" variant={action?.destructive ? 'destructive' : 'default'} disabled={running} onClick={onConfirm}>
            {running ? <Loader2Icon className="animate-spin" /> : null}
            {action?.label}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * 确认框状态 hook：`request(action)` 打开确认，确认后执行 `run`；
 * `run` 抛错时保留确认框并 toast 报错，成功则关闭。
 */
export function useConfirmAction() {
  const [action, setAction] = useState<ConfirmAction | null>(null)
  const [running, setRunning] = useState(false)

  const request = useCallback((next: ConfirmAction) => setAction(next), [])
  const cancel = useCallback(() => setAction(null), [])

  const confirm = useCallback(async () => {
    if (!action) return
    setRunning(true)
    try {
      await action.run()
      setAction(null)
    } catch (reason) {
      toast.error(humanizeApiError(reason, '操作失败'))
    } finally {
      setRunning(false)
    }
  }, [action])

  const dialog = <ConfirmActionDialog action={action} running={running} onCancel={cancel} onConfirm={() => void confirm()} />
  return { request, dialog, running }
}

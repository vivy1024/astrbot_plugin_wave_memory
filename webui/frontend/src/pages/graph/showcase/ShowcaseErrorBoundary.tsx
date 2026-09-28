import { Component, type ReactNode } from 'react'

interface State {
  error: Error | null
}

/**
 * 3D 模块是独立的懒加载分包（three.js）：分包加载失败（例如部署后旧页面请求旧文件名）或
 * WebGL 初始化抛错时，只在舞台内提示，不让整个关系图谱页崩掉。
 */
export class ShowcaseErrorBoundary extends Component<{ children: ReactNode; onFallback2D(): void }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="flex h-[72svh] min-h-[480px] flex-col items-center justify-center gap-3 rounded-xl border bg-card p-6 text-center text-sm" role="alert">
        <p className="font-medium">3D 展示加载失败</p>
        <p className="max-w-md text-muted-foreground">{this.state.error.message || '未知错误'}。刷新页面可重新加载；也可以先用平面图。</p>
        <div className="flex gap-2">
          <button type="button" className="rounded-md border px-3 py-1.5 hover:bg-accent" onClick={() => window.location.reload()}>刷新页面</button>
          <button type="button" className="rounded-md border px-3 py-1.5 hover:bg-accent" onClick={this.props.onFallback2D}>切到平面图</button>
        </div>
      </div>
    )
  }
}

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyInitialTheme } from './components/theme/theme-init'

// 在 React 挂载前同步写入 .dark/.light，避免刷新时主题闪烁。
applyInitialTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

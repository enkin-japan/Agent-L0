import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { DeckPrint } from './components/deck/deck-print.tsx'

// /print/deck：PDF 导出专用的纯渲染路由（无头 Chrome 加载），不挂聊天应用、
// 不触发 store 副作用。其余路径一律进主应用。
const isDeckPrint = window.location.pathname === '/print/deck'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isDeckPrint ? <DeckPrint /> : <App />}
  </StrictMode>,
)

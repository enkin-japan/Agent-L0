import path from "path"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig, loadEnv } from "vite"

// dev 鉴权（见 docs/frontend-refactor-decisions.md §2）：
// 密钥由代理在 Node 侧注入 Authorization 头，浏览器 bundle 永不持有 key。
// AGENT_API_KEY 无 VITE_ 前缀，不会被打进客户端。
// 环境变量统一在【仓库根】的 .env / .env.local（envDir 指向上级；已被根 .gitignore 忽略）——
// 与后端共用同一份密钥文件，devctl.sh 启动后端时 source 的也是它。
// 生产由 Nginx 注入同一头（前后端分离部署），前端代码对鉴权无感。
//
// ⚠️ 安全边界：host 0.0.0.0 + 代理注入 key，意味着【任何能访问 5173 的设备都等于免钥
// 使用 Agent】（含 shell 等强力工具）。本端口只允许暴露在两条受信通路上：
//   1. Tailscale 内网直连；
//   2. cloudflared 通道 test.enkin.dev——鉴权在 Cloudflare Access 边缘完成，到达本机的
//      流量已过验证（HTTPS 在边缘终止，剪贴板等安全上下文 API 因此可用）。
// 绝不可裸端口转发/直连到公网或不受信 LAN——后端 14159 的 Bearer 鉴权在这条通路上形同虚设。
export default defineConfig(({ mode }) => {
  // 环境变量目录 = 仓库根（backend/frontend 共用一份 .env / .env.local）
  const envDir = path.resolve(__dirname, "..")
  const env = loadEnv(mode, envDir, "")
  // 进程环境变量优先（devctl.sh 已 export 时不再读文件），回落根 .env 文件
  const apiKey = process.env.AGENT_API_KEY ?? env.AGENT_API_KEY ?? ""

  return {
    plugins: [
      react(),
      tailwindcss(),
    ],
    envDir,
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      // Vite 默认拦截非 localhost 的 Host 头；放行 cloudflared 通道域名，
      // 否则经 test.enkin.dev 访问会收到 "Blocked request"。
      allowedHosts: ["test.enkin.dev"],
      proxy: {
        "/api": {
          target: "http://localhost:14159",
          changeOrigin: true,
          // 仅在配置了密钥时注入；未配置则不加头（对应后端"未启用鉴权"放行）
          headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : undefined,
        },
      },
    },
  }
})

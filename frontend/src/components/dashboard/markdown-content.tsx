import { memo, type ReactElement, type ReactNode } from "react"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

import { CopyButton } from "@/components/ui/copy-button"

/** 递归提取 React 子树的纯文本（供代码块复制按钮取原文）。 */
function toText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return ""
  if (typeof node === "string" || typeof node === "number") return String(node)
  if (Array.isArray(node)) return node.map(toText).join("")
  if (typeof node === "object" && "props" in node)
    return toText((node as ReactElement<{ children?: ReactNode }>).props.children)
  return ""
}

/**
 * 模型正文的 Markdown 渲染（仅 assistant 消息使用；用户输入保持纯文本）。
 *
 * 安全边界（与后端反注入立场一致——模型正文可能混入 web_research 带回的敌意内容）：
 *  - 不启用 raw HTML（不装 rehype-raw），react-markdown 默认转义 HTML 标签；
 *  - 不加载远程图片（<img> 会让浏览器自动外发请求，等于给注入内容开信标通道），
 *    图片降级为可点开的链接；唯一放行：同源 /api/v1/files/ 相对路径（后端会话资产，
 *    render_chart 等工具的产物）——不出门、经代理注入的 Bearer 鉴权，信标通道不存在；
 *  - 所有链接 target=_blank + rel=noopener noreferrer。
 */
const components: Components = {
  p: ({ children }) => <p className="mb-2 leading-relaxed last:mb-0">{children}</p>,
  h1: ({ children }) => <h1 className="mb-2 mt-3 text-lg font-semibold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-3 text-base font-semibold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1.5 mt-2.5 text-sm font-semibold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1.5 mt-2 text-sm font-semibold first:mt-0">{children}</h4>,
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed [&>p]:mb-1">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-border pl-3 text-muted-foreground last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-border" />,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-primary underline underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),
  img: ({ src, alt }) => {
    // 同源会话资产白名单：以 /api/v1/files/ 开头的相对路径才真正渲染成图片。
    // 判定必须是「前缀锚定的相对路径」——含协议/双斜杠的绝对 URL 一律不进白名单。
    if (typeof src === "string" && /^\/api\/v1\/files\/[^/]+\/assets\/[^/?#]+$/.test(src)) {
      return (
        <img
          src={src}
          alt={alt || "image"}
          loading="lazy"
          className="my-2 max-w-full rounded-lg border border-border"
        />
      )
    }
    return (
      <a
        href={typeof src === "string" ? src : undefined}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline underline-offset-2"
      >
        [{alt || "image"}]
      </a>
    )
  },
  // 行内 code 的样式；块级 code 在 pre 里用 arbitrary variant 归零重置
  code: ({ children, className }) => (
    <code className={`rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] ${className ?? ""}`}>
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <div className="group/code relative mb-2 last:mb-0">
      <pre className="overflow-x-auto rounded-lg bg-muted/70 p-3 text-xs leading-relaxed [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-xs">
        {children}
      </pre>
      <CopyButton
        text={toText(children).replace(/\n$/, "")}
        className="absolute right-1.5 top-1.5 bg-background/80 opacity-0 backdrop-blur transition-opacity focus-visible:opacity-100 group-hover/code:opacity-100"
      />
    </div>
  ),
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto last:mb-0">
      <table className="w-max min-w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border bg-muted/60 px-2 py-1 text-left font-medium">{children}</th>
  ),
  td: ({ children }) => <td className="border border-border px-2 py-1 align-top">{children}</td>,
}

/** memo：remark 解析是消息列表里最贵的一步。正文不变（引用相等）时跳过重解析——
 *  没有 memo 时，流式期间每个 token 增量都会把整个会话的全部 markdown 重新解析一遍
 *  （实测 Safari 内存重载事故的主因），memo 后只有流式气泡自身随增量重算。 */
export const MarkdownContent = memo(function MarkdownContent({ text }: { text: string }) {
  return (
    <div className="min-w-0 break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
})

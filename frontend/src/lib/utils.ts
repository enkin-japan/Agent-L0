import { useCallback, useLayoutEffect, useRef } from "react"
import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** 身份恒定的回调包装：返回的函数引用永不变化，调用时转发到最新的 fn。
 *  供 React.memo 的浅比较使用——父级每次渲染重建的内联箭头函数会让 memo 失效，
 *  包一层后子组件按值比较即可跳过重渲染，且不存在闭包过期问题。 */
export function useStableCallback<A extends unknown[], R>(
  fn: (...args: A) => R,
): (...args: A) => R {
  const ref = useRef(fn)
  useLayoutEffect(() => {
    ref.current = fn
  })
  return useCallback((...args: A) => ref.current(...args), [])
}

/** 复制文本到剪贴板。优先 Clipboard API（需安全上下文：HTTPS/localhost），
 *  Tailscale 裸 HTTP 等场景回退到临时 textarea + execCommand。 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      /* 落入回退路径 */
    }
  }
  const ta = document.createElement("textarea")
  ta.value = text
  ta.style.position = "fixed"
  ta.style.opacity = "0"
  document.body.appendChild(ta)
  ta.select()
  try {
    return document.execCommand("copy")
  } catch {
    return false
  } finally {
    document.body.removeChild(ta)
  }
}

/** 触发浏览器下载：把文本内容存为本地文件。 */
export function downloadText(filename: string, text: string, mime = "text/markdown") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
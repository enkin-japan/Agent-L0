import { useRef, useState, type ComponentProps, type MouseEvent } from "react"
import { Check, Copy } from "lucide-react"

import { cn, copyText } from "@/lib/utils"

/** 通用一键复制按钮：点击复制 text，图标短暂切换为 ✓ 作反馈。
 *  label/copiedLabel 可选（消息操作行带文字，代码块角标只有图标）。
 *  透传 button props/ref，可作 Radix TooltipTrigger asChild 的子组件。 */
export function CopyButton({
  text,
  label,
  copiedLabel,
  className,
  onClick,
  ...rest
}: ComponentProps<"button"> & {
  text: string
  label?: string
  copiedLabel?: string
}) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>(null)

  const handleClick = async (e: MouseEvent<HTMLButtonElement>) => {
    onClick?.(e)
    if (!(await copyText(text))) return
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button
      type="button"
      {...rest}
      onClick={handleClick}
      className={cn(
        "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground/60 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        copied && "text-emerald-500 hover:text-emerald-500",
        className,
      )}
    >
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {(copied ? copiedLabel ?? label : label) && (
        <span>{copied ? copiedLabel ?? label : label}</span>
      )}
    </button>
  )
}

"use client"

import { useEffect, useRef, useState } from "react"
import { ArrowUp, Check, FileAudio, FileText, Loader2, Mic, Paperclip, Pause, Square, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useT } from "@/store/useUiStore"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

// 图像/音频原生直喂；其余文件（PDF/文档/代码/文本）走后端摄入解析层
// （急解析或懒登记，见 framework/ingestion），因此不再限制可选类型。
type Attachment = {
  id: string
  file: File
  kind: "image" | "audio" | "file"
  previewUrl?: string
}

type RecordState = "idle" | "recording" | "processing"

function writeString(view: DataView, offset: number, text: string) {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
}

// 直接用 Web Audio 采集裸 PCM（见 startRecording），最终封成后端 soundfile 能读的
// 16-bit 单声道 WAV。避开了 MediaRecorder 的 webm/mp4 编码 + decodeAudioData 解码这条
// 跨浏览器易碎链路——此前「录音转码失败」正是 decodeAudioData 在该链路上抛错所致。
function encodeWav(mono: Float32Array, sampleRate: number): Blob {
  const frames = mono.length
  const view = new DataView(new ArrayBuffer(44 + frames * 2))
  writeString(view, 0, "RIFF")
  view.setUint32(4, 36 + frames * 2, true)
  writeString(view, 8, "WAVE")
  writeString(view, 12, "fmt ")
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeString(view, 36, "data")
  view.setUint32(40, frames * 2, true)
  for (let i = 0; i < frames; i++) {
    const s = Math.max(-1, Math.min(1, mono[i]))
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([view], { type: "audio/wav" })
}

// 把逐帧采到的 Float32 片段拼成一整段单声道 PCM
function mergePcm(chunks: Float32Array[]): Float32Array {
  let length = 0
  for (const c of chunks) length += c.length
  const out = new Float32Array(length)
  let offset = 0
  for (const c of chunks) {
    out.set(c, offset)
    offset += c.length
  }
  return out
}

function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60)
  const s = totalSeconds % 60
  return `${m}:${String(s).padStart(2, "0")}`
}

export function ChatInput({
  onSend,
  onStop,
  onSuspend,
  disabled,
  approvalPending = false,
  canSuspend = false,
  canStop = false,
  stopPending = false,
  suspendPending = false,
  thinkingEnabled,
}: {
  onSend: (value: string, files: File[]) => void
  onStop: () => void
  /** 挂起当前生成中的任务（让出 worker，转入可恢复态） */
  onSuspend?: () => void
  /** 请求在途时禁用输入（一次只发一条） */
  disabled: boolean
  /** 有敏感操作等待用户审批：禁用输入但不显示停止按钮（无在途请求可停） */
  approvalPending?: boolean
  /** 生成中且可挂起：在停止按钮旁显示"挂起"按钮 */
  canSuspend?: boolean
  canStop?: boolean
  stopPending?: boolean
  suspendPending?: boolean
  thinkingEnabled: boolean
}) {
  const t = useT()
  const [value, setValue] = useState("")
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // --- 实时录音 ---
  const [recordState, setRecordState] = useState<RecordState>("idle")
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [recordError, setRecordError] = useState<string | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const pcmRef = useRef<Float32Array[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const discardRef = useRef(false)

  // 卸载时兜底停止采集、释放麦克风与 AudioContext
  useEffect(() => {
    return () => {
      discardRef.current = true
      teardownCapture()
    }
  }, [])

  /** 停止采集、断开音频图、释放麦克风与 AudioContext；返回采样率供封装 WAV 用 */
  function teardownCapture(): number {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
    const processor = processorRef.current
    if (processor) {
      processor.onaudioprocess = null
      try { processor.disconnect() } catch { /* 已断开 */ }
    }
    try { sourceRef.current?.disconnect() } catch { /* 已断开 */ }
    streamRef.current?.getTracks().forEach((t) => t.stop())
    const ctx = audioCtxRef.current
    const sampleRate = ctx?.sampleRate ?? 48000
    void ctx?.close()
    processorRef.current = null
    sourceRef.current = null
    streamRef.current = null
    audioCtxRef.current = null
    return sampleRate
  }

  async function startRecording() {
    setRecordError(null)
    // http + 非 localhost 属于不安全上下文，浏览器直接不暴露 mediaDevices
    if (!navigator.mediaDevices?.getUserMedia) {
      setRecordError(t.input.errMicContext)
      return
    }
    const AudioContextCtor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AudioContextCtor) {
      setRecordError(t.input.errMicUnsupported)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContextCtor()
      if (ctx.state === "suspended") await ctx.resume()
      const source = ctx.createMediaStreamSource(stream)
      // ScriptProcessor 虽已废弃但跨浏览器（含 Safari）稳定、无需独立 worklet 模块文件；
      // onaudioprocess 里把输入帧原样收下、输出恒填静音，避免把麦克风回灌到扬声器。
      const processor = ctx.createScriptProcessor(4096, 1, 1)
      pcmRef.current = []
      discardRef.current = false
      processor.onaudioprocess = (e) => {
        if (!discardRef.current) {
          pcmRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)))
        }
        e.outputBuffer.getChannelData(0).fill(0)
      }
      source.connect(processor)
      processor.connect(ctx.destination) // 接上 destination 才会持续触发 onaudioprocess
      streamRef.current = stream
      audioCtxRef.current = ctx
      sourceRef.current = source
      processorRef.current = processor
      setRecordSeconds(0)
      setRecordState("recording")
      timerRef.current = setInterval(() => setRecordSeconds((s) => s + 1), 1000)
    } catch {
      teardownCapture()
      setRecordError(t.input.errMicFailed)
    }
  }

  /** 结束录音；discard 为 true 时丢弃录音内容。封装是纯本地同步计算，不再有解码这一步。 */
  function stopRecording(discard: boolean) {
    if (!audioCtxRef.current) return
    discardRef.current = discard
    const sampleRate = teardownCapture()
    const chunks = pcmRef.current
    pcmRef.current = []

    if (discard) {
      setRecordState("idle")
      return
    }
    const mono = mergePcm(chunks)
    if (mono.length === 0) {
      setRecordState("idle")
      setRecordError(t.input.errMicNoData)
      return
    }
    setRecordState("processing")
    try {
      const wav = encodeWav(mono, sampleRate)
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15)
      const file = new File([wav], `recording-${stamp}.wav`, { type: "audio/wav" })
      setAttachments((prev) => [
        ...prev,
        {
          id: `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 7)}`,
          file,
          kind: "audio" as const,
          previewUrl: URL.createObjectURL(file),
        },
      ])
    } catch (err) {
      console.error("录音封装 WAV 失败", err)
      setRecordError(t.input.errWavFailed)
    }
    setRecordState("idle")
  }

  function handleInput(event: React.ChangeEvent<HTMLTextAreaElement>) {
    setValue(event.target.value)
    const el = event.target
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }

  function addFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return
    const next: Attachment[] = Array.from(fileList).map((file) => {
      const kind = file.type.startsWith("image/")
        ? ("image" as const)
        : file.type.startsWith("audio/")
          ? ("audio" as const)
          : ("file" as const) // PDF/文档/代码等：后端摄入层解析或懒登记
      return {
        id: `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 7)}`,
        file,
        kind,
        // 图片缩略图 / 音频试听；普通文件无预览
        previewUrl: kind === "file" ? undefined : URL.createObjectURL(file),
      }
    })
    setAttachments((prev) => [...prev, ...next])
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id)
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl)
      return prev.filter((a) => a.id !== id)
    })
  }

  function submit() {
    if (disabled) return
    const trimmed = value.trim()
    if (!trimmed && attachments.length === 0) return
    // prompt 为必填字段：纯附件时用文件名兜底
    onSend(trimmed || attachments.map((a) => a.file.name).join(", "), attachments.map((a) => a.file))
    // 输入区的缩略图/试听 blob URL 随发送作废（消息气泡用的是 store 自建的另一份），
    // 不 revoke 会把原始字节钉在内存里直到页面关闭
    for (const a of attachments) {
      if (a.previewUrl) URL.revokeObjectURL(a.previewUrl)
    }
    setValue("")
    setAttachments([])
    if (textareaRef.current) textareaRef.current.style.height = "auto"
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  const canSend = !disabled && (value.trim().length > 0 || attachments.length > 0)

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-5">
      <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => { addFiles(e.target.files); e.target.value = "" }} />

      <div className="rounded-3xl border border-border bg-card p-2 shadow-[0_8px_30px_-12px_rgba(15,23,42,0.18)]">
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-1 pb-2 pt-1">
            {attachments.map((attachment) => (
              <div key={attachment.id} className="flex items-center gap-2 rounded-xl border border-border bg-background py-1.5 pl-1.5 pr-2">
                {attachment.kind === "image" && attachment.previewUrl ? (
                  <img src={attachment.previewUrl} alt={attachment.file.name} className="size-8 shrink-0 rounded-lg object-cover" />
                ) : attachment.kind === "audio" && attachment.previewUrl ? (
                  <audio src={attachment.previewUrl} controls preload="metadata" className="h-8 max-w-48" />
                ) : (
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                    {attachment.kind === "audio" ? <FileAudio className="size-4" /> : <FileText className="size-4" />}
                  </span>
                )}
                <span className="max-w-40 truncate text-xs">{attachment.file.name}</span>
                <button type="button" onClick={() => removeAttachment(attachment.id)} className="text-muted-foreground hover:text-foreground">
                  <X className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={disabled}
          placeholder={
            approvalPending
              ? t.input.placeholderApproval
              : disabled
                ? t.input.placeholderWaiting
                : t.input.placeholder
          }
          className="block max-h-48 w-full resize-none bg-transparent px-3 pt-2.5 pb-1 text-sm leading-relaxed focus:outline-none disabled:opacity-60"
        />

        <div className="flex items-center justify-between gap-2 px-1 pt-1">
          <div className="flex items-center gap-1">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="rounded-xl text-muted-foreground" disabled={disabled || recordState !== "idle"} onClick={() => fileInputRef.current?.click()}>
                  <Paperclip className="size-5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{t.input.upload}</TooltipContent>
            </Tooltip>

            {recordState === "idle" && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className="rounded-xl text-muted-foreground" disabled={disabled} onClick={() => void startRecording()}>
                    <Mic className="size-5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.input.record}</TooltipContent>
              </Tooltip>
            )}

            {recordState === "recording" && (
              <div className="flex items-center gap-1.5 rounded-xl bg-destructive/10 py-0.5 pl-2.5 pr-1">
                <span className="size-2 animate-pulse rounded-full bg-destructive" />
                <span className="min-w-9 font-mono text-xs text-destructive">
                  {formatDuration(recordSeconds)}
                </span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button variant="ghost" size="icon" className="size-7 rounded-lg text-muted-foreground" onClick={() => stopRecording(true)}>
                      <X className="size-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{t.input.cancelRecord}</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button size="icon" className="size-7 rounded-lg" onClick={() => stopRecording(false)}>
                      <Check className="size-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{t.input.finishRecord}</TooltipContent>
                </Tooltip>
              </div>
            )}

            {recordState === "processing" && (
              <div className="flex items-center gap-1.5 px-2 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {t.input.processing}
              </div>
            )}
          </div>

          {disabled && !approvalPending ? (
            <div className="flex items-center gap-1.5">
              {canSuspend && onSuspend && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button size="icon" variant="outline" className="size-9 rounded-xl" onClick={onSuspend} disabled={suspendPending}>
                      <Pause className="size-4" />
                      <span className="sr-only">{t.input.suspendTooltip}</span>
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{t.input.suspendTooltip}</TooltipContent>
                </Tooltip>
              )}
              {canStop && <Tooltip>
                <TooltipTrigger asChild>
                  <Button size="icon" variant="outline" className="size-9 rounded-xl" onClick={onStop} disabled={stopPending}>
                    <Square className="size-4" />
                    <span className="sr-only">{t.input.stopTooltip}</span>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.input.stopTooltip}</TooltipContent>
              </Tooltip>}
            </div>
          ) : (
            <Button size="icon" className="size-9 rounded-xl" disabled={!canSend} onClick={submit}>
              <ArrowUp className="size-5" />
            </Button>
          )}
        </div>
      </div>
      <p className="pt-2 text-center text-xs text-muted-foreground">
        {recordError ? (
          <span className="text-destructive">{recordError}</span>
        ) : recordState === "recording" ? (
          t.input.recordingHint
        ) : approvalPending ? (
          t.input.approvalHint
        ) : disabled ? (
          t.input.generatingHint
        ) : thinkingEnabled ? (
          t.input.thinkingOnHint
        ) : (
          t.input.thinkingOffHint
        )}
      </p>
    </div>
  )
}

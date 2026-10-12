import { useEffect } from "react"
import { sessionAssetUrl } from "@/api/agent"
import { useChatStore } from "@/store/useChatStore"
import { useT, useUiStore } from "@/store/useUiStore"
import type { SessionFile, SessionRetentionPolicy } from "@/types/agent-api"
import { SessionRetentionNotice } from "./session-retention-notice"

// Files are a temporary projection of the existing assets endpoint, not a file library.
export function FilesPanel({ sessionId }: { sessionId: string }) {
  const snapshot = useUiStore((s) => s.files)
  const watch = useUiStore((s) => s.watchFiles)
  const refresh = useUiStore((s) => s.refreshFiles)
  const selectionVersion = useChatStore((s) => s.selectionVersion)
  const policy = useChatStore((s) => s.retentionPolicy)
  useEffect(() => watch(sessionId), [watch, sessionId, selectionVersion])
  return <FilesView sessionId={sessionId} snapshot={snapshot} policy={policy} onRefresh={refresh} />
}

/** The single files renderer, shared by the live container and CPU SSR tests. */
export function FilesView({ sessionId, snapshot, policy, onRefresh }: {
  sessionId: string
  snapshot: { sessionId: string; data: SessionFile[] | null; state: "unknown" | "loading" | "fresh" | "stale"; error: "unauthorized" | "unavailable" | null } | null
  policy?: SessionRetentionPolicy | null
  onRefresh: () => Promise<void>
}) {
  const t = useT()
  const current = snapshot?.sessionId === sessionId ? snapshot : null
  return (
    <section className="flex flex-col gap-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold">{t.files.title}</h3>
        <button type="button" className="underline" onClick={() => void onRefresh()}>{t.files.refresh}</button>
      </div>
      <p className="text-muted-foreground">{t.files.scope}</p>
      {(!current || current.state === "loading") && <p role="status">{t.files.loading}</p>}
      {current?.state === "unknown" && <p role="status">{t.files.unknown}</p>}
      {current?.state === "stale" && <p role="status">{t.files.stale}</p>}
      {current?.error && <p role="status" className="text-destructive">{current.error === "unauthorized" ? t.files.unauthorized : t.files.failed}</p>}
      {current?.state === "fresh" && current.data?.length === 0 && <p>{t.files.empty}</p>}
      {current?.data?.map((file) => {
        const url = sessionAssetUrl(sessionId, file.name, file.mtime)
        return <div key={file.name} className="rounded-lg border border-border p-2">
          {url ? <a href={url} download={file.name} className="break-all font-medium underline">{file.name}</a> : <span className="break-all">{file.name} · {t.files.unavailable}</span>}
          <p className="mt-1 text-muted-foreground">{file.size} B · {Number.isFinite(file.mtime) ? new Date(file.mtime * 1000).toLocaleString() : t.files.unknown}</p>
        </div>
      })}
      <SessionRetentionNotice policy={policy} text={t.retention} />
    </section>
  )
}

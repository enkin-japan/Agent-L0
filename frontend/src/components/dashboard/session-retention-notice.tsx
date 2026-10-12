import type { Dict } from "@/lib/i18n"
import type { SessionRetentionPolicy } from "@/types/agent-api"

export function SessionRetentionNotice({ policy, text }: {
  policy?: SessionRetentionPolicy | null
  text: Dict["retention"]
}) {
  return (
    <details className="rounded-lg border border-border/60 p-2 text-xs text-muted-foreground">
      <summary className="cursor-pointer font-medium">{text.title}</summary>
      <div className="mt-2 space-y-2 leading-relaxed">
        {!policy ? <p>{text.unknown}</p> : <>
          <p>{policy.enabled ? text.history(policy.retention_days) : text.disabled}</p>
          {policy.enabled && <><p>{text.empty(policy.empty_ttl_hours)}</p><p>{text.exemptions}</p><p>{text.timing}</p></>}
          <p>{text.files}</p>
        </>}
      </div>
    </details>
  )
}

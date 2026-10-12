import { useMemo, useRef, useState } from "react"
import type { DeletionReceipt } from "@/store/useChatStore"
import {
  BrainCircuit,
  Cpu,
  Monitor,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  PenSquare,
  Pin,
  PinOff,
  Search,
  Settings,
  Sun,
  Trash2,
  CheckCircle2,
  Circle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { convTitle, type Conversation } from "@/lib/agent-data"
import { LANGS, type Lang } from "@/lib/i18n"
import { useT, useUiStore, type Theme } from "@/store/useUiStore"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Switch } from "@/components/ui/switch"
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { SchedulerStrip } from "@/components/dashboard/scheduler-strip"
import { ScheduleStrip } from "@/components/dashboard/schedule-strip"
import { SessionRetentionNotice } from "@/components/dashboard/session-retention-notice"
import type { SessionRetentionPolicy } from "@/types/agent-api"

type AgentSidebarProps = {
  conversations: Conversation[]
  retentionPolicy?: SessionRetentionPolicy | null
  activeId: string
  onSelect: (id: string) => void
  onNewChat: (workspaceId: string | null) => void
  /** 打开定时任务的专属会话（本地无条目则以任务名补建）。 */
  onOpenScheduleSession: (sessionId: string, title: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
  onDeleteMany: (ids: string[]) => Promise<DeletionReceipt[] | null>
  deletingSessions?: Record<string, boolean>
  deletionResults?: Record<string, DeletionReceipt>
  onTogglePin: (id: string, pinned: boolean) => void
  onCollapse: () => void
  thinkingEnabled: boolean
  onThinkingChange: (value: boolean) => void
}

export function AgentSidebar({
  conversations, retentionPolicy, activeId, onSelect, onNewChat, onOpenScheduleSession,
  onRename, onDelete, onDeleteMany, onTogglePin, onCollapse,
  deletingSessions = {}, deletionResults = {},
  thinkingEnabled, onThinkingChange,
}: AgentSidebarProps) {
  const t = useT()
  const tasks = useUiStore((s) => s.tasks)
  const tasksState = useUiStore((s) => s.tasksState)
  const tasksError = useUiStore((s) => s.tasksError)
  const [searchQuery, setSearchQuery] = useState("")
  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [deletingSelected, setDeletingSelected] = useState(false)
  const [deleteFeedback, setDeleteFeedback] = useState<string | null>(null)
  const selectionScope = useRef(0)

  const filteredConversations = useMemo(() =>
    conversations.filter((c) =>
      convTitle(c.title, t.store.newChatTitle).toLowerCase().includes(searchQuery.toLowerCase()),
    ),
    [conversations, searchQuery, t.store.newChatTitle],
  )

  const recentChats = useMemo(() =>
    filteredConversations
      // pin 优先，其次按最后更新倒序
      .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || b.updatedAt - a.updatedAt),
    [filteredConversations],
  )

  function toggleSelected(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function exitSelectMode() {
    selectionScope.current++
    setDeletingSelected(false)
    setDeleteFeedback(null)
    setSelectMode(false)
    setSelectedIds(new Set())
  }

  async function confirmDeleteSelected() {
    if (deletingSelected) return
    const scope = selectionScope.current
    setDeletingSelected(true); setDeleteFeedback(null)
    try {
      const receipts = await onDeleteMany([...selectedIds])
      if (scope !== selectionScope.current || receipts === null) return
      const completed = new Set(receipts.filter((r) => r.status === "deleted").map((r) => r.id))
      // Remove only confirmed successes from the *current* selection. Failed
      // targets and choices added while waiting remain available for retry.
      setSelectedIds((current) => new Set([...current].filter((id) => !completed.has(id))))
      const incomplete = receipts.filter((r) => r.status !== "deleted")
      setDeleteFeedback(incomplete.length ? incomplete.map((r) => `${r.id}: ${r.message}`).join("\n") : t.management.deleteComplete)
    } catch (error) {
      if (scope === selectionScope.current) setDeleteFeedback(t.management.failed(error instanceof Error ? error.message : "HTTP failed"))
    } finally { if (scope === selectionScope.current) setDeletingSelected(false) }
  }

  function renderChatItem(conversation: Conversation) {
    const isActive = conversation.id === activeId
    const isPinned = !!conversation.pinned
    // 选择模式：整行变复选，点击切换选中而非跳转；操作菜单让位给复选框
    if (selectMode) {
      const checked = selectedIds.has(conversation.id)
      return (
        <button
          key={conversation.id}
          type="button"
          onClick={() => toggleSelected(conversation.id)}
          className={cn(
            "flex w-full items-center gap-2 rounded-xl py-2 pl-2.5 pr-3 text-left transition-colors",
            checked ? "bg-accent" : "hover:bg-accent/60",
          )}
        >
          {checked
            ? <CheckCircle2 className="size-4 shrink-0 text-primary" />
            : <Circle className="size-4 shrink-0 text-muted-foreground/50" />}
          {isPinned && <Pin className="size-3 shrink-0 text-muted-foreground/70" />}
          <span className="truncate text-sm">{convTitle(conversation.title, t.store.newChatTitle)}</span>
        </button>
      )
    }
    return (
      <div key={conversation.id} className={cn("group/item relative flex items-center rounded-xl transition-colors", isActive ? "bg-accent" : "hover:bg-accent/60")}>
        <button onClick={() => onSelect(conversation.id)} className="flex min-w-0 flex-1 items-center gap-1.5 rounded-xl py-2 pl-3 pr-9 text-left">
          {isPinned && <Pin className="size-3 shrink-0 text-muted-foreground/70" />}
          <span className={cn("truncate text-sm", isActive ? "font-medium" : "text-foreground/80")}>
            {convTitle(conversation.title, t.store.newChatTitle)}
          </span>
        </button>
        <div className="absolute right-1.5 top-1/2 -translate-y-1/2">
          <DropdownMenu>
            {/* 恒常显示：iOS Safari「请求桌面网站」会伪装成桌面宽度 + hover:hover，
                任何靠 @media(hover) 或 lg 断点收起的方案都会在触屏上把入口藏死。可靠起见一律常显。 */}
            <DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="h-8 w-8 text-muted-foreground"><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuItem onClick={() => onTogglePin(conversation.id, !isPinned)}>
                  {isPinned
                    ? <><PinOff className="mr-2 size-4" /> {t.workspace.unpin}</>
                    : <><Pin className="mr-2 size-4" /> {t.workspace.pin}</>}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => onRename(conversation.id, prompt(t.workspace.renamePrompt, convTitle(conversation.title, t.store.newChatTitle)) || conversation.title)}><Pencil className="mr-2 size-4" /> {t.workspace.rename}</DropdownMenuItem>
                <DropdownMenuItem className="text-destructive" disabled={!!deletingSessions[conversation.id]} onClick={() => onDelete(conversation.id)}><Trash2 className="mr-2 size-4" /> {t.workspace.delete}</DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    )
  }

  return (
    <aside className="flex h-full w-72 max-w-[85vw] flex-col border-r border-sidebar-border bg-sidebar p-3">
      {/* Brand mark（占位式标志：Cpu 符号 + 中性描述名，后续可替换） */}
      <div className="flex items-center gap-2.5 px-2 py-3">
        <div className="flex size-8 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <Cpu className="size-4" />
        </div>
        <div className="flex min-w-0 flex-col leading-tight">
          <span className="truncate text-sm font-semibold">{t.appName}</span>
          <span className="truncate text-[10px] text-muted-foreground">{t.tagline}</span>
        </div>
        <Button variant="ghost" size="icon" className="ml-auto shrink-0" onClick={onCollapse}>
          <PanelLeftClose className="size-4" />
          <span className="sr-only">{t.sidebar.collapse}</span>
        </Button>
      </div>

      <div className="px-2 mb-2">
        <Button className="w-full justify-start gap-2 rounded-xl" onClick={() => onNewChat(null)}>
          <PenSquare className="size-4" /> {t.sidebar.newChat}
        </Button>
      </div>

      <div className="px-2 mb-3 relative">
        <Search className="absolute left-4 top-2.5 size-4 text-muted-foreground" />
        <Input placeholder={t.sidebar.searchPlaceholder} className="pl-9 rounded-xl" value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} />
      </div>

      {/* Worker 状态条（pillar A） */}
      <div className="px-2 mb-3">
        <SchedulerStrip tasks={tasks} tasksState={tasksState} tasksError={tasksError}
          conversations={conversations} activeId={activeId} onSelect={onSelect} />
      </div>

      {/* 定时任务条（落盘实体；无任务时自隐藏） */}
      <div className="px-2 mb-3 empty:mb-0 empty:px-0">
        <ScheduleStrip onOpenSession={onOpenScheduleSession} />
      </div>

      {/* min-h-0：flex 子项默认 min-height:auto 不会缩到内容以下，缺它则 flex-1 被内容撑高、
          ScrollArea 内部 viewport 拿不到有界高度、纵向滚动失效，会话多时整条侧栏被撑爆。 */}
      <ScrollArea className="min-h-0 flex-1 px-2">
        <div className="flex items-center justify-between pb-2">
          <p className="text-xs font-semibold text-muted-foreground">{t.sidebar.recentChats}</p>
          {recentChats.length > 0 && (
            <button
              type="button"
              className="text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
              onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
            >
              {selectMode ? t.sidebar.cancelSelect : t.sidebar.select}
            </button>
          )}
        </div>
        <div className="space-y-1">{recentChats.map((conversation) => <div key={conversation.id}>
              {renderChatItem(conversation)}
              {deletingSessions[conversation.id] ? <p role="status" className="px-3 text-xs text-muted-foreground">{t.management.deletePending}</p>
                : deletionResults[conversation.id] && deletionResults[conversation.id].status !== "deleted"
                  ? <p role="status" className="whitespace-pre-wrap px-3 text-xs text-muted-foreground">{deletionResults[conversation.id].message}</p> : null}
            </div>)}</div>
        <div className="mt-3"><SessionRetentionNotice policy={retentionPolicy} text={t.retention} /></div>
      </ScrollArea>

      {/* 批量删除操作条：仅选择模式且有选中时出现 */}
      {selectMode && selectedIds.size > 0 && (
        <div className="px-2 pt-2">
          <Button
            variant="destructive"
            className="w-full justify-center gap-2 rounded-xl"
            onClick={confirmDeleteSelected}
            disabled={deletingSelected}
          >
            <Trash2 className="size-4" /> {t.sidebar.deleteSelected} ({selectedIds.size})
          </Button>
        </div>
      )}
      {deleteFeedback && <p role="status" className="whitespace-pre-wrap px-3 pt-2 text-xs text-muted-foreground">{deleteFeedback}</p>}

      {/* Deep Thinking 开关（对应接口 enable_thinking） */}
      <div className="px-2 pt-3">
        <label htmlFor="thinking-mode" className="flex cursor-pointer items-center gap-3 rounded-2xl border border-border bg-card p-3">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent text-accent-foreground">
            <BrainCircuit className="size-4" />
          </div>
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="text-sm font-medium">{t.settings.deepThinking}</span>
            <span className="truncate text-xs text-muted-foreground">
              {thinkingEnabled ? t.settings.deepThinkingDesc : t.input.thinkingOffHint}
            </span>
          </div>
          <Switch id="thinking-mode" checked={thinkingEnabled} onCheckedChange={onThinkingChange} />
        </label>
      </div>

      {/* 设置入口（语言 / 主题 / 深度思考） */}
      <button
        type="button"
        className="mt-2 flex items-center gap-3 rounded-xl px-2 py-2.5 text-left transition-colors hover:bg-accent"
        onClick={() => setSettingsOpen(true)}
      >
        <div className="flex size-8 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          <Settings className="size-4" />
        </div>
        <span className="flex-1 text-sm font-medium">{t.sidebar.settings}</span>
        <span className="text-xs uppercase text-muted-foreground">
          {LANGS.find((l) => l.code === useUiStore.getState().lang)?.label}
        </span>
      </button>

      {/* Settings dialog */}
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        thinkingEnabled={thinkingEnabled}
        onThinkingChange={onThinkingChange}
      />
    </aside>
  )
}

function SettingsDialog({
  open,
  onOpenChange,
  thinkingEnabled,
  onThinkingChange,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  thinkingEnabled: boolean
  onThinkingChange: (v: boolean) => void
}) {
  const t = useT()
  const lang = useUiStore((s) => s.lang)
  const theme = useUiStore((s) => s.theme)
  const setLang = useUiStore((s) => s.setLang)
  const setTheme = useUiStore((s) => s.setTheme)

  const themeOptions: { value: Theme; label: string; icon: typeof Sun }[] = [
    { value: "light", label: t.settings.themeLight, icon: Sun },
    { value: "dark", label: t.settings.themeDark, icon: Moon },
    { value: "system", label: t.settings.themeSystem, icon: Monitor },
  ]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t.settings.title}</DialogTitle>
          <DialogDescription>{t.settings.desc}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5 py-1">
          {/* Language */}
          <div>
            <p className="mb-2 text-sm font-medium">{t.settings.language}</p>
            <div className="grid grid-cols-3 gap-1.5">
              {LANGS.map((l) => (
                <SegButton key={l.code} active={lang === l.code} onClick={() => setLang(l.code as Lang)}>
                  {l.label}
                </SegButton>
              ))}
            </div>
          </div>

          {/* Theme */}
          <div>
            <p className="mb-2 text-sm font-medium">{t.settings.theme}</p>
            <div className="grid grid-cols-3 gap-1.5">
              {themeOptions.map((o) => (
                <SegButton key={o.value} active={theme === o.value} onClick={() => setTheme(o.value)}>
                  <o.icon className="size-3.5" />
                  {o.label}
                </SegButton>
              ))}
            </div>
          </div>

          {/* Deep thinking */}
          <label htmlFor="settings-thinking" className="flex cursor-pointer items-center gap-3">
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="text-sm font-medium">{t.settings.deepThinking}</span>
              <span className="text-xs text-muted-foreground">{t.settings.deepThinkingDesc}</span>
            </div>
            <Switch id="settings-thinking" checked={thinkingEnabled} onCheckedChange={onThinkingChange} />
          </label>
        </div>

        <DialogFooter>
          <DialogClose asChild><Button>{t.settings.done}</Button></DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function SegButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex items-center justify-center gap-1.5 rounded-lg border px-2 py-1.5 text-xs font-medium transition-colors",
        active
          ? "border-primary bg-primary text-primary-foreground"
          : "border-border bg-transparent text-muted-foreground hover:bg-accent",
      )}
    >
      {children}
    </button>
  )
}

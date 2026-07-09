"use client"

import { useEffect, useMemo, useState } from "react"

import { TooltipProvider } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { useChatStore } from "@/store/useChatStore"
import { applyTheme, useT, useUiStore } from "@/store/useUiStore"
import { AgentSidebar } from "@/components/dashboard/agent-sidebar"
import { ChatArea } from "@/components/dashboard/chat-area"
import { InspectorPanel } from "@/components/dashboard/inspector-panel"

export default function Page() {
  const t = useT()
  const workspaces = useChatStore((s) => s.workspaces)
  const conversations = useChatStore((s) => s.conversations)
  const activeId = useChatStore((s) => s.activeId)
  const thinkingEnabled = useChatStore((s) => s.thinkingEnabled)
  const generating = useChatStore((s) => !!s.generating[s.activeId])
  const pendingTools = useChatStore((s) => s.pendingApprovals[s.activeId] ?? null)
  const suspended = useChatStore((s) => !!s.suspendedSessions[s.activeId])
  const taskProgress = useChatStore((s) => s.taskProgress[s.activeId] ?? null)
  const activity = useChatStore((s) => s.liveActivity[s.activeId] ?? null)
  const notice = useChatStore((s) => s.notice)
  const lastMetrics = useChatStore((s) => s.lastMetrics)

  const theme = useUiStore((s) => s.theme)
  const startSchedulerPolling = useUiStore((s) => s.startSchedulerPolling)
  const stopSchedulerPolling = useUiStore((s) => s.stopSchedulerPolling)
  const startSchedulePolling = useUiStore((s) => s.startSchedulePolling)
  const stopSchedulePolling = useUiStore((s) => s.stopSchedulePolling)

  const {
    selectConversation,
    newConversation,
    renameConversation,
    moveConversation,
    deleteConversation,
    deleteConversations,
    togglePin,
    addWorkspace,
    renameWorkspace,
    deleteWorkspace,
    setThinking,
    clearNotice,
    sendMessage,
    respondApproval,
    deleteLastTurn,
    updateLastTurn,
    stopGenerating,
    suspendTask,
    resumeTask,
    abandonSuspended,
    branchFrom,
  } = useChatStore.getState()

  // UI-only state
  const [mobileOpen, setMobileOpen] = useState(false)
  const [desktopCollapsed, setDesktopCollapsed] = useState(false)
  // 右侧检视面板（Plan / Memory / Context）
  const [inspectorOpen, setInspectorOpen] = useState(false)

  // 主题：初次挂载即应用（system 模式下交给 CSS 媒体查询），随设置变化重应用
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // 启动时从服务端拉取 UI 状态（工作区/会话结构），实现换设备/换浏览器无缝迁移；
  // 随后以后端会话索引对账：剔除已被自动清扫回收的幽灵、补入孤儿会话、同步 pin。
  useEffect(() => {
    void useChatStore
      .getState()
      .hydrateFromServer()
      .then(() => useChatStore.getState().reconcileSessions())
  }, [])

  // 全局单 worker 调度快照的低频轮询（驱动侧栏 Worker 状态条）
  useEffect(() => {
    startSchedulerPolling()
    return () => stopSchedulerPolling()
  }, [startSchedulerPolling, stopSchedulerPolling])

  // 定时任务清单的更低频轮询（驱动侧栏 Schedules 条）
  useEffect(() => {
    startSchedulePolling()
    return () => stopSchedulePolling()
  }, [startSchedulePolling, stopSchedulePolling])

  // 切换会话时按需从服务端拉取历史（刷新后恢复对话内容）
  useEffect(() => {
    void useChatStore.getState().loadHistory(activeId)
  }, [activeId])

  const activeConversation = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? conversations[0],
    [conversations, activeId],
  )

  function handleDelete(id: string) {
    if (window.confirm(t.confirm.deleteConversation)) deleteConversation(id)
  }

  function handleDeleteMany(ids: string[]) {
    if (ids.length === 0) return
    if (window.confirm(t.confirm.deleteConversationsMany(ids.length))) deleteConversations(ids)
  }

  return (
    <TooltipProvider delayDuration={0}>
      <div className="flex h-dvh w-full overflow-hidden bg-background text-foreground">
        {/* Left rail */}
        <div
          className={cn(
            "fixed inset-y-0 left-0 z-40 transition-transform duration-300 lg:static",
            mobileOpen ? "translate-x-0" : "-translate-x-full",
            desktopCollapsed ? "lg:hidden lg:-translate-x-full" : "lg:translate-x-0",
          )}
        >
          <AgentSidebar
            workspaces={workspaces}
            conversations={conversations}
            activeId={activeId}
            onSelect={(id) => {
              selectConversation(id)
              setMobileOpen(false)
            }}
            onNewChat={(workspaceId) => {
              newConversation(workspaceId)
              setMobileOpen(false)
            }}
            onOpenScheduleSession={(sessionId, title) => {
              useChatStore.getState().openScheduleSession(sessionId, title)
              setMobileOpen(false)
            }}
            onRename={renameConversation}
            onDelete={handleDelete}
            onDeleteMany={handleDeleteMany}
            onTogglePin={togglePin}
            onMove={moveConversation}
            onAddWorkspace={addWorkspace}
            onRenameWorkspace={renameWorkspace}
            onDeleteWorkspace={(id) => {
              if (window.confirm(t.confirm.deleteWorkspace)) deleteWorkspace(id)
            }}
            onCollapse={() => {
              setMobileOpen(false)
              setDesktopCollapsed(true)
            }}
            thinkingEnabled={thinkingEnabled}
            onThinkingChange={setThinking}
          />
        </div>

        {/* Mobile overlay */}
        {mobileOpen && (
          <button
            type="button"
            aria-label="Close sidebar"
            onClick={() => setMobileOpen(false)}
            className="fixed inset-0 z-30 bg-foreground/20 backdrop-blur-sm lg:hidden"
          />
        )}

        {/* Center rail */}
        <ChatArea
          conversation={activeConversation}
          thinkingEnabled={thinkingEnabled}
          generating={generating}
          notice={notice}
          metrics={lastMetrics}
          sidebarHidden={desktopCollapsed}
          pendingTools={pendingTools}
          suspended={suspended}
          taskProgress={taskProgress}
          activity={activity}
          contextOpen={inspectorOpen}
          onToggleContext={() => setInspectorOpen((v) => !v)}
          onApproval={(approve) => void respondApproval(activeId, approve)}
          onSend={(value, files) => {
            if (suspended && !window.confirm(t.confirm.abandonForNewMessage)) return
            void sendMessage(value, files)
          }}
          onStop={() => stopGenerating(activeId)}
          onSuspend={() => suspendTask(activeId)}
          onResume={() => void resumeTask(activeId)}
          onAbandon={() => abandonSuspended(activeId)}
          onBranch={(nodeIndex) => void branchFrom(activeId, nodeIndex)}
          onUpdateLast={(text) => void updateLastTurn(text)}
          onDeleteLast={() => {
            if (window.confirm(t.confirm.deleteLastTurn)) void deleteLastTurn(activeId)
          }}
          onRename={renameConversation}
          onDismissNotice={clearNotice}
          onOpenMobileSidebar={() => setMobileOpen(true)}
          onShowSidebar={() => {
            setDesktopCollapsed(false)
            setMobileOpen(true)
          }}
        />

        {/* Right rail: 检视面板（桌面端内嵌，移动端为带遮罩浮层） */}
        {inspectorOpen && (
          <>
            <button
              type="button"
              aria-label="Close inspector"
              onClick={() => setInspectorOpen(false)}
              className="fixed inset-0 z-30 bg-foreground/20 backdrop-blur-sm lg:hidden"
            />
            <div className="fixed inset-y-0 right-0 z-40 lg:static lg:z-auto">
              <InspectorPanel
                conversation={activeConversation}
                taskProgress={taskProgress}
                onClose={() => setInspectorOpen(false)}
              />
            </div>
          </>
        )}
      </div>
    </TooltipProvider>
  )
}

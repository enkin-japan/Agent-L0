import SwiftUI

/// Suggested starting points fill the existing composer; they never submit a task.
struct ConversationWelcomeView: View {
    @EnvironmentObject var store: AgentStore

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s5) {
            Image(systemName: "asterisk")
                .font(.system(size: 26, weight: .medium))
                .foregroundStyle(Theme.accent)
                .frame(width: 56, height: 56)
                .background(Theme.selection, in: RoundedRectangle(cornerRadius: 18))
            VStack(alignment: .leading, spacing: Theme.s2) {
                Text(L("从一件想完成的事开始"))
                    .font(.title2.weight(.semibold))
                Text(L("提个问题，整理资料，或交给 Agent 一项任务。"))
                    .font(Theme.readingFont).foregroundStyle(Theme.secondaryText)
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Theme.s3) { suggestions }
                VStack(alignment: .leading, spacing: Theme.s2) { suggestions }
            }
        }
        .frame(maxWidth: Theme.readingWidth, alignment: .leading)
        .padding(.vertical, 36)
    }

    @ViewBuilder private var suggestions: some View {
        suggestion("整理资料", icon: "doc.on.doc", prompt: "帮我整理资料，提炼主要结论和待确认的问题。")
        suggestion("规划行程", icon: "map", prompt: "帮我规划一段行程，先了解我的时间和偏好。")
        suggestion("写作与总结", icon: "text.alignleft", prompt: "帮我整理思路，写出一份结构清晰的草稿。")
    }

    private func suggestion(_ title: String, icon: String, prompt: String) -> some View {
        Button {
            if store.selectedSessionId == nil { store.newSession() }
            store.composerPrefill = L(prompt)
        } label: {
            HStack(spacing: Theme.s2) {
                Image(systemName: icon).foregroundStyle(Theme.accent)
                Text(L(title)).font(.callout.weight(.medium))
                Spacer(minLength: Theme.s2)
                Image(systemName: "arrow.up.right").font(.caption).foregroundStyle(Theme.secondaryText)
            }
            .padding(Theme.s3)
            .frame(minWidth: 140, minHeight: Theme.controlHitSize)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.radiusCard))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard).strokeBorder(Theme.separator))
        }
        .buttonStyle(.plain)
    }
}

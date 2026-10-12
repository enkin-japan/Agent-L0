import SwiftUI

struct SessionRetentionNotice: View {
    let policy: SessionRetentionPolicy?

    var body: some View {
        DisclosureGroup(L("会话保留规则")) {
            VStack(alignment: .leading, spacing: 8) {
                if let policy {
                    if policy.enabled {
                        Text(String(format: L("普通会话超过 %@ 天没有保存新一轮对话后，可能被清理。"),
                                    policy.retentionDays.formatted()))
                        Text(String(format: L("没有历史记录的空会话在创建 %@ 小时后过期。"),
                                    policy.emptyTTLHours.formatted()))
                        Text(L("置顶、定时任务和进行中的会话受保护。需要长期保留时，请置顶会话。"))
                        Text(L("清理按周期执行，阅读历史不会延长期限。"))
                    } else {
                        Text(L("自动清理已关闭。"))
                    }
                    Text(L("附件和产物会随会话一并删除；保存到其他位置的副本不受影响。"))
                } else {
                    Text(L("尚未从服务端获取保留规则。"))
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 6)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

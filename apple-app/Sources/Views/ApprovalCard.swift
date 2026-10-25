import SwiftUI

/// 一次序列化生成摘要和全文，保证展开审阅与摘要使用同一份待执行参数。
struct ApprovalArguments {
    let fullText: String
    var summary: String { fullText.count > 500 ? String(fullText.prefix(500)) + "…" : fullText }

    init(_ arguments: [String: JSONValue]) {
        fullText = arguments.compact(max: Int.max)
    }

    static func displayValue(_ value: JSONValue) -> String {
        if case .string(let text) = value { return singleLine(text) }
        guard let data = try? JSONEncoder().encode(value) else { return "…" }
        return singleLine(String(decoding: data, as: UTF8.self))
    }

    /// Argument names are model/tool supplied too, and are shown as row labels.
    static func displayKey(_ key: String) -> String { singleLine(key) }

    /// Characters that change what the line reads as without being visible:
    /// LRM/RLM/ALM, the embedding/override pairs and the isolates (reordering),
    /// plus the zero-width and annotation formats (hiding or splitting words).
    /// The emoji joiners U+200C/U+200D are deliberately not here.
    private static let invisibleControls: Set<UInt32> = [0x200E, 0x200F, 0x061C, 0x202A, 0x202B,
                                                         0x202C, 0x202D, 0x202E, 0x2066, 0x2067,
                                                         0x2068, 0x2069, 0x200B, 0x2060, 0xFEFF,
                                                         0x00AD, 0xFFF9, 0xFFFA, 0xFFFB,
                                                         // zero-width fillers / joiners that hide text
                                                         0x180E, 0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0,
                                                         0x2800, 0x17B4, 0x17B5]
    /// TAG characters smuggle invisible ASCII (a hidden "; rm -rf ~" adds no visible
    /// width); invisible operators and deprecated format controls. Subdivision-flag
    /// emoji (TAG sequences) lose their flag on this card — fidelity traded for an
    /// approval that shows everything. Homoglyphs (Cyrillic а, fullwidth ／) are out
    /// of scope: they are visible, just look alike.
    private static let invisibleRanges: [ClosedRange<UInt32>] = [0xE0000...0xE007F, 0x2061...0x2064,
                                                                 0x206A...0x206F]

    private static func isConfusable(_ scalar: Unicode.Scalar) -> Bool {
        scalar.properties.generalCategory == .control
            || scalar.properties.generalCategory == .lineSeparator
            || scalar.properties.generalCategory == .paragraphSeparator
            || invisibleControls.contains(scalar.value)
            || invisibleRanges.contains { $0.contains(scalar.value) }
    }

    private static func escape(_ scalar: Unicode.Scalar) -> String.UnicodeScalarView {
        String(format: "\\u{%04X}", scalar.value).unicodeScalars
    }

    /// What is approved must be what is read. A string argument is drawn as one
    /// logical line with its line breaks and control characters escaped, so no
    /// value can forge another "key / value" row of this card (a path ending in
    /// "\n\ncommand\nrm -rf ~" reads as `/tmp/safe.txt\n\ncommand\nrm -rf ~`).
    /// Ordinary single-line values are unchanged, and 完整参数 still shows the
    /// exact JSON that will be executed.
    static func singleLine(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\n": out.append(contentsOf: "\\n".unicodeScalars)
            case "\r": out.append(contentsOf: "\\r".unicodeScalars)
            case "\t": out.append(contentsOf: "\\t".unicodeScalars)
            case "\\": out.append(contentsOf: "\\\\".unicodeScalars)
            default:
                if isConfusable(scalar) { out.append(contentsOf: escape(scalar)) }
                else { out.append(scalar) }
            }
        }
        return String(out)
    }

    /// For the already serialized texts (summary / 完整参数): JSON escapes the line
    /// breaks itself, so only the invisible reordering characters have to be made
    /// visible. Backslashes and quotes of the serialization are left untouched, so
    /// the displayed text stays the exact JSON that will be executed.
    static func visibleFormatting(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            if isConfusable(scalar) { out.append(contentsOf: escape(scalar)) }
            else { out.append(scalar) }
        }
        return String(out)
    }
}

/// 敏感工具审批卡片：账本 awaiting_approval 时出现，决议受理后由事件流续跑。
struct ApprovalCard: View {
    let tools: [PendingTool]
    let onDecision: (Bool) -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            HStack(alignment: .top, spacing: Theme.s3) {
                Image(systemName: "exclamationmark.shield")
                    .font(.headline)
                    .foregroundStyle(Theme.warning)
                    .padding(.top, 2)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Theme.s1) {
                    Text(L("需要你的确认"))
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Text(L("以下敏感操作等待你的授权"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(Array(tools.enumerated()), id: \.offset) { index, tool in
                VStack(alignment: .leading, spacing: Theme.s2) {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.s2) {
                        Text("\(index + 1)")
                            .font(.caption.monospacedDigit().weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text(tool.name)
                            .font(.system(.callout, design: .monospaced).weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let args = tool.arguments, !args.isEmpty {
                        let presentation = ApprovalArguments(args)
                        if presentation.fullText.count <= 500 {
                            ForEach(args.keys.sorted(), id: \.self) { key in
                                VStack(alignment: .leading, spacing: Theme.s1) {
                                    Text(ApprovalArguments.displayKey(key)).font(.caption).foregroundStyle(.secondary)
                                    Text(args[key].map(ApprovalArguments.displayValue) ?? "")
                                        .font(.callout)
                                        .textSelection(.enabled)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        } else {
                            Text(ApprovalArguments.visibleFormatting(presentation.summary))
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        DisclosureGroup(L("完整参数")) {
                            Text(ApprovalArguments.visibleFormatting(presentation.fullText))
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .padding(Theme.s3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.wash)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
            }
            decisionActions
        }
        .padding(Theme.s4)
        .background(Theme.card)
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusBubble)
            .strokeBorder(Theme.separator, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusBubble))
    }

    @ViewBuilder
    private var decisionActions: some View {
        #if os(macOS)
        HStack(spacing: Theme.s2) {
            Spacer(minLength: 0)
            rejectButton
            approveButton
        }
        #else
        if dynamicTypeSize.isAccessibilitySize {
            VStack(spacing: Theme.s2) {
                rejectButton.frame(maxWidth: .infinity)
                approveButton.frame(maxWidth: .infinity)
            }
        } else {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Theme.s2) { rejectButton; approveButton }
                VStack(spacing: Theme.s2) { rejectButton; approveButton }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        #endif
    }

    private var rejectButton: some View {
        Button(role: .destructive) { onDecision(false) } label: {
            Label(L("拒绝"), systemImage: "xmark")
                .padding(.horizontal, Theme.s2)
                .frame(minHeight: Theme.controlHitSize)
                .frame(maxWidth: actionMaxWidth)
                .fixedSize(horizontal: false, vertical: true)
        }
        .buttonStyle(.bordered)
        .controlSize(.regular)
    }

    private var approveButton: some View {
        Button { onDecision(true) } label: {
            Label(L("批准执行"), systemImage: "checkmark")
                .padding(.horizontal, Theme.s2)
                .frame(minHeight: Theme.controlHitSize)
                .frame(maxWidth: actionMaxWidth)
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(Theme.accentInk)
        }
        .buttonStyle(.borderedProminent)
        .tint(Theme.accent)
        .controlSize(.regular)
    }

    private var actionMaxWidth: CGFloat? {
        #if os(macOS)
        nil
        #else
        .infinity
        #endif
    }

}

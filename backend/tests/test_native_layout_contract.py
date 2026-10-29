"""CPU-only source contracts for the native layout crash workarounds.

These assert the protective structure, not the appearance or absence of an AppKit
crash. Real streaming and window resize behavior still need native validation.
Mutations operate on in-memory copies; production files are never rewritten.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2] / "apple-app/Sources"


def sources():
    names = ["Views/RootView.swift", "Views/ChatView.swift",
             "Views/MarkdownText.swift", "App/MacAgentApp.swift"]
    files = {name: re.sub(r"//[^\n]*", "", (ROOT / name).read_text()) for name in names}
    # 其余全部 Swift 源（守卫要扫整棵树：工具栏隐藏写在任何 macOS 视图上都会抹掉三色按钮）
    others = [re.sub(r"//[^\n]*", "", p.read_text()) for p in sorted(ROOT.rglob("*.swift"))
              if p.relative_to(ROOT).as_posix() not in names]
    files["__others__"] = "\n".join(others)
    return files


# placement 是可变参数：`for: .navigationBar, .windowToolbar` 也要命中，故不要求 windowToolbar 紧跟右括号
WINDOW_TOOLBAR_HIDDEN = re.compile(r"\.toolbar(?:Visibility)?\(\s*\.hidden\s*,\s*for:[^)]*\bwindowToolbar\b")


def assert_layout_contract(files):
    root = files["Views/RootView.swift"]
    chat = files["Views/ChatView.swift"]
    markdown = files["Views/MarkdownText.swift"]
    app = files["App/MacAgentApp.swift"]
    assert not re.search(r"\bNavigationSplitView\s*[{(]", root), "system split view reintroduced"
    assert re.search(r"HStack\(spacing: 0\).*?SessionListView\(\).*?\.frame\(width: 260\)", root, re.S), "fixed sidebar missing"
    assert ".frame(minWidth: 760, minHeight: 480)" in root, "root minimum size missing"
    # hiddenTitleBar 下隐藏窗口工具栏会连系统三色按钮一起抹掉（build 80 回归，2026-10-29 探针实测，
    # benchmarks/titleprobe）。扫整棵 Swift 源树，.toolbar / .toolbarVisibility 两种写法都算。
    everything = "\n".join(files.values())
    assert not WINDOW_TOOLBAR_HIDDEN.search(everything), "window toolbar hidden somewhere: traffic lights vanish under hiddenTitleBar"
    assert ".windowStyle(.hiddenTitleBar)" in app, "hidden title bar style missing"
    assert "store.showInspector ? 960 : 760" in app, "inspector minimum width missing"
    assert ".navigationDestination(item: chatRoute)" in root, "iOS item route missing"
    assert re.search(r"set: \{ route in\s*if route == nil \{\s*store.selectedSessionId = nil", root), "iOS pop binding missing"
    mac_preview = re.search(r"#if os\(macOS\)\s*(ScrollView\s*\{.*?streamingPreview.*?)#else", chat, re.S)
    assert mac_preview, "macOS isolated streaming scroller missing"
    assert ".defaultScrollAnchor(.bottom)" in mac_preview[1], "stream anchor missing"
    assert ".frame(height: 320)" in mac_preview[1], "stream container must have constant height"
    stream_code = re.search(r"if streaming \{(.*?)\} else \{", markdown, re.S)
    assert stream_code and "Text(code)" in stream_code[1], "streaming code must use Text"
    assert "ScrollView" not in stream_code[1], "streaming code creates nested scroll host"
    assert "ScrollView(.horizontal, showsIndicators: false)" in markdown[stream_code.end():], "final code lost horizontal scrolling"
    assert "MarkdownText(text: store.streamText, streaming: true)" in chat, "live Markdown lost streaming mode"
    assert "store.decideApproval(approve: approve, target: target)" in chat, "approval target capture missing"
    assert '.id(target.sessionId + ":" + target.turnId + ":" + String(target.seq))' in chat, "approval batch identity missing"


class NativeLayoutContractTests(unittest.TestCase):
    def test_production_layout_contract(self):
        assert_layout_contract(sources())

    def test_mutations_are_rejected(self):
        mutations = [
            ("Views/RootView.swift", "HStack(spacing: 0)", "NavigationSplitView"),
            ("Views/RootView.swift", ".frame(width: 260)", ".frame(maxWidth: .infinity)"),
            ("App/MacAgentApp.swift", "store.showInspector ? 960 : 760", "760"),
            ("Views/RootView.swift", "store.selectedSessionId = nil", "_ = route"),
            ("Views/ChatView.swift", ".frame(height: 320)", ".frame(minHeight: 320)"),
            ("Views/ChatView.swift", "MarkdownText(text: store.streamText, streaming: true)", "MarkdownText(text: store.streamText)"),
            ("Views/MarkdownText.swift", "if streaming {", "if streaming { ScrollView(.horizontal) {}"),
            ("Views/ChatView.swift", "target: target", "target: store.pendingApprovalTarget!"),
            # 三色按钮守卫：RootView 重加旧句 / 别的视图用 toolbarVisibility 写法 / 去掉 hiddenTitleBar
            ("Views/RootView.swift", ".frame(minWidth: 760, minHeight: 480)",
             ".frame(minWidth: 760, minHeight: 480).toolbar(.hidden, for: .windowToolbar)"),
            ("Views/ChatView.swift", "MarkdownText(text: store.streamText, streaming: true)",
             "MarkdownText(text: store.streamText, streaming: true).toolbarVisibility(.hidden, for: .windowToolbar)"),
            ("App/MacAgentApp.swift", ".windowStyle(.hiddenTitleBar)", ".windowStyle(.automatic)"),
            # 四个具名文件之外的视图（__others__ = 其余全部 Swift 源）藏一句，也要抓到
            ("__others__", "struct SessionListView", "struct SessionListView.toolbar(.hidden, for: .windowToolbar)"),
            # 可变参数 placement 写法
            ("Views/RootView.swift", ".frame(minWidth: 760, minHeight: 480)",
             ".frame(minWidth: 760, minHeight: 480).toolbar(.hidden, for: .navigationBar, .windowToolbar)"),
        ]
        for name, before, after in mutations:
            with self.subTest(protected=before):
                files = sources()
                self.assertIn(before, files[name])
                files[name] = files[name].replace(before, after, 1)
                with self.assertRaises(AssertionError):
                    assert_layout_contract(files)


if __name__ == "__main__":
    unittest.main()

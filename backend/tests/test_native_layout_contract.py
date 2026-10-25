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
    return {name: re.sub(r"//[^\n]*", "", (ROOT / name).read_text()) for name in names}


def assert_layout_contract(files):
    root = files["Views/RootView.swift"]
    chat = files["Views/ChatView.swift"]
    markdown = files["Views/MarkdownText.swift"]
    app = files["App/MacAgentApp.swift"]
    assert not re.search(r"\bNavigationSplitView\s*[{(]", root), "system split view reintroduced"
    assert re.search(r"HStack\(spacing: 0\).*?SessionListView\(\).*?\.frame\(width: 260\)", root, re.S), "fixed sidebar missing"
    assert ".frame(minWidth: 760, minHeight: 480)" in root, "root minimum size missing"
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

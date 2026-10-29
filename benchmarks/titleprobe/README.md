# titleprobe：hiddenTitleBar 下 `.toolbar(.hidden, for: .windowToolbar)` 是否抹掉三色按钮（2026-10-29）

最小 SwiftUI app：`WindowGroup { Root() }.windowStyle(.hiddenTitleBar)`，`--hide-toolbar` 时在内容上加
`.toolbar(.hidden, for: .windowToolbar)`。启动 1.5 s 后打印窗口号、styleMask、三个 standardWindowButton 的
isHidden/alpha/frame，然后用 `screencapture -l <窗口号>` 截图。

```bash
xcrun swiftc -parse-as-library -O main.swift -o probe
./probe --hide-toolbar &   # 截图 → shot-hide.png：左上角没有三色按钮
./probe &                 # 截图 → shot-keep.png：三色按钮在
```

结论只由两张截图区分：AppKit 层两种情况都报 `hidden=false alpha=1.0`（按钮对象在、没被画），
`safeAreaInsets.top` 两种情况都是 32。这是 build 80 三色按钮没出现的定案证据，守卫在
`backend/tests/test_native_layout_contract.py`。

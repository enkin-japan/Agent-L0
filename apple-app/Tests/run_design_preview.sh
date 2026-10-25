#!/bin/bash
# Offline, screenshot-only preview of the actual Sources renderers (CPU only).
# Uses existing local build/ MarkdownUI artifacts; never resolves packages or starts services.
# Usage: bash Tests/run_design_preview.sh OUTPUT [--matrix | --appearance light|dark
#        --scenario conversation|approval|streaming|welcome --width 1120 --height 900]
# Optional: DESIGN_PREVIEW_BUILD=/existing/DerivedData to select prebuilt dependencies.
# --matrix emits light/dark at 1120x900 and 760x760 for conversation/approval/streaming.
# Each temporary, independently identified .app exits after its own view-only PNG + JSON.
# No formal MacAgent entry point, push bootstrap, sidecar startup or updater is launched.
set -euo pipefail
cd "$(dirname "$0")"
output=${1:?Usage: run_design_preview.sh OUTPUT [--matrix | preview options]}
shift
mkdir -p "$output"
output=$(cd "$output" && pwd)
build=${DESIGN_PREVIEW_BUILD:-../build}
build=$(cd "$build" && pwd)
products="$build/Build/Products/Debug"
for artifact in MarkdownUI.o NetworkImage.o cmark-gfm.o cmark-gfm-extensions.o; do
    test -f "$products/$artifact" || { echo "Missing existing dependency: $products/$artifact" >&2; exit 1; }
done
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
bundle="$scratch/MacAgentDesignPreview.app"
mkdir -p "$bundle/Contents/MacOS"
cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.enkin.macagent.design-preview</string>
<key>CFBundleExecutable</key><string>MacAgentDesignPreview</string>
<key>CFBundleName</key><string>MacAgent Design Preview</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
sources=()
while IFS= read -r source; do
    case "$source" in
        ../Sources/App/MacAgentApp.swift|../Sources/App/AppSettings.swift) ;;
        *) sources+=("$source") ;;
    esac
done < <(find ../Sources -name '*.swift' -type f | LC_ALL=C sort)
xcrun swiftc -parse-as-library -target arm64-apple-macos14.0 \
    -module-cache-path "$scratch/modules" \
    -I "$products" \
    -I "$build/SourcePackages/checkouts/swift-cmark/src/include" \
    -I "$build/SourcePackages/checkouts/swift-cmark/extensions/include" \
    "${sources[@]}" design_preview_support.swift design_preview.swift \
    "$products/MarkdownUI.o" "$products/NetworkImage.o" \
    "$products/cmark-gfm.o" "$products/cmark-gfm-extensions.o" \
    -o "$bundle/Contents/MacOS/MacAgentDesignPreview"
binary="$bundle/Contents/MacOS/MacAgentDesignPreview"
if [[ ${1:-} == --matrix ]]; then
    shift
    test "$#" -eq 0 || { echo '--matrix does not accept additional options' >&2; exit 1; }
    for appearance in light dark; do
        for scenario in conversation approval streaming; do
            "$binary" "$output" --appearance "$appearance" --scenario "$scenario" --width 1120 --height 900
            "$binary" "$output" --appearance "$appearance" --scenario "$scenario" --width 760 --height 760
        done
    done
else
    "$binary" "$output" "$@"
fi

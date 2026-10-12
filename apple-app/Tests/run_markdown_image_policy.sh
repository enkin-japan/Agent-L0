#!/bin/bash
# CPU-only renderer regression using already-built local MarkdownUI/cmark artifacts; no dependency resolution.
set -euo pipefail
cd "$(dirname "$0")"
build=${1:?Usage: run_markdown_image_policy.sh /path/to/existing/DerivedData [--baseline]}
shift
build=$(cd "$build" && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
xcrun swiftc -parse-as-library -target arm64-apple-macos14.0 \
    -module-cache-path "$scratch/modules" \
    -I "$build/Build/Products/Debug" \
    -I "$build/SourcePackages/checkouts/swift-cmark/src/include" \
    -I "$build/SourcePackages/checkouts/swift-cmark/extensions/include" \
    ../Sources/App/L10n.swift \
    ../Sources/Views/MarkdownImagePolicy.swift \
    ../Sources/Views/MarkdownImageProviders.swift \
    ../Sources/Views/MarkdownText.swift \
    markdown_image_policy_test.swift \
    "$build/Build/Products/Debug/MarkdownUI.o" \
    "$build/Build/Products/Debug/NetworkImage.o" \
    "$build/Build/Products/Debug/cmark-gfm.o" \
    "$build/Build/Products/Debug/cmark-gfm-extensions.o" \
    -o "$scratch/markdown_image_policy_test"
"$scratch/markdown_image_policy_test" "$@"

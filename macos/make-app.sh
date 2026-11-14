#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

BIN=.build/release/LedgePill
[[ -x "$BIN" ]] || { echo "build first: swift build -c release" >&2; exit 1; }

APP=LedgePill.app
rm -rf "$APP" LedgePill-macos.zip
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/LedgePill"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleIdentifier</key>
	<string>it.zorn.LedgePill</string>
	<key>CFBundleName</key>
	<string>LedgePill</string>
	<key>CFBundleExecutable</key>
	<string>LedgePill</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0.0</string>
	<key>CFBundleVersion</key>
	<string>1.0.0</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSAppleEventsUsageDescription</key>
	<string>LedgePill controls your music player.</string>
	<key>NSHighResolutionCapable</key>
	<true/>
</dict>
</plist>
EOF

ditto -c -k --keepParent "$APP" LedgePill-macos.zip
echo "LedgePill-macos.zip"

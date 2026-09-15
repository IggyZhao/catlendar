#!/bin/bash
# Builds Catlendar.app. The bundle exists so macOS has something to attach the
# Accessibility and Calendar permissions to: a bare Python process cannot hold
# them. The launcher stays alive and runs Python as its child, so prompts name
# Catlendar and the usage strings in Info.plist are the ones macOS shows.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/Catlendar.app"

if [ "$(uname)" != "Darwin" ]; then
  echo "This builds the macOS app. On Windows or Linux use: python -m catlendar track"
  exit 1
fi

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Catlendar</string>
    <key>CFBundleDisplayName</key><string>Catlendar</string>
    <key>CFBundleIdentifier</key><string>com.catlendar.app</string>
    <key>CFBundleExecutable</key><string>Catlendar</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSCalendarsUsageDescription</key>
    <string>Catlendar reads your calendar so your dashboard can show meetings next to your working time. Everything stays on this Mac.</string>
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Catlendar reads your calendar so your dashboard can show meetings next to your working time. Everything stays on this Mac.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Catlendar can ask Microsoft Outlook for your meetings if the Calendar app does not have them.</string>
</dict>
</plist>
PLIST

clang -O2 -o "$APP/Contents/MacOS/Catlendar" "$ROOT/launcher/launcher.c"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "note: ad hoc signing failed; permissions may be asked for again after a rebuild"

echo "Built $APP"
echo "Start it with:  open '$APP'"

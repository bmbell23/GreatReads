#!/bin/bash
# Rebuild the simple-app debug APK and stage it at web/ereader.apk so the
# device can pull the new build from http://100.69.184.113:8090/ereader.apk
# and install it in-place (no uninstall — debug builds share the same
# ~/.android/debug.keystore signing key, so Android treats it as an
# upgrade, not a fresh install).
#
# Usage:  ./build-app.sh           # build + stage
#         ./build-app.sh --clean   # gradle clean first
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT/simple-app"

VERSION="$(cat "$REPO_ROOT/version.txt" 2>/dev/null || echo unknown)"
echo "🔨 Building debug APK (version: $VERSION)"

if [ "$1" = "--clean" ]; then
    ./gradlew clean
fi

./gradlew assembleDebug

SRC="app/build/outputs/apk/debug/app-debug.apk"
DEST="$REPO_ROOT/web/ereader.apk"

if [ ! -f "$SRC" ]; then
    echo "❌ Build succeeded but APK not found at $SRC"
    exit 1
fi

cp "$SRC" "$DEST"
SIZE=$(stat -c%s "$DEST" 2>/dev/null || stat -f%z "$DEST")
STAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo
echo "✅ Staged: $DEST"
echo "   size:  $SIZE bytes"
echo "   built: $STAMP"
echo
echo "📲 On the phone:"
echo "   1. Open http://100.69.184.113:8090/ereader.apk in any browser"
echo "   2. Tap the downloaded file → 'Update' (in-place upgrade)"
echo "   3. Reopen GreatReads"
echo
echo "(No uninstall required — same signing key as the previous build.)"

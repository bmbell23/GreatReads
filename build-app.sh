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
# version.txt only moves on a commit, but the APK gets rebuilt many times between
# commits — so semver alone cannot answer "is the staged build newer than mine?"
# (#277). This stamp is baked into the APK assets and repeated in the sidecar the
# updater endpoint reads, so the two are directly comparable.
BUILD_STAMP="$(date -Iseconds)"
echo "🔨 Building debug APK (version: $VERSION, build: $BUILD_STAMP)"
printf '%s\n' "$BUILD_STAMP" > "$REPO_ROOT/web/build-stamp.txt"

# Stage the web shell into APK assets so the app can serve it offline (#23).
# Always re-copied from web/ so the bundled shell can't drift from the live one.
# Server-only scripts and the APK itself are excluded.
ASSETS="$REPO_ROOT/simple-app/app/src/main/assets/web"
echo "📦 Staging web shell → assets/web"
rm -rf "$ASSETS"
mkdir -p "$ASSETS"
cp -R "$REPO_ROOT/web/." "$ASSETS/"
rm -rf "$ASSETS/serve.py" "$ASSETS/keep-alive.sh" "$ASSETS/__pycache__" \
       "$ASSETS/ereader.apk" "$ASSETS/ereader.apk.idsig"

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

# Sidecar for the in-app updater (#277). The APK's versionName is baked from
# version.txt AT BUILD TIME, so the repo's current version.txt is NOT what the
# staged APK reports once version.txt moves on — without this, the app would
# compare against the wrong number and nag forever about an update it already has.
printf '{"version":"%s","built_at":"%s"}\n' \
    "$VERSION" "$BUILD_STAMP" > "$DEST.json"

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

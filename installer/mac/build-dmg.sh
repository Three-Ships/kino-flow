#!/bin/bash
# Build the Kino Flow macOS installer (Kino-Flow.dmg). RUN THIS ON A MAC.
#
#   cd installer/mac && ./build-dmg.sh
#
# Produces dist/Kino-Flow.dmg containing "Kino Flow.app". Unsigned — first launch
# is right-click > Open (Gatekeeper). Assets come from Google Drive; on first run
# the app builds the light Python envs (uv), ensures ffmpeg + the Claude CLI, and
# opens the studio in the browser.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APPNAME="Kino Flow"
BUILD="$HERE/build"
APP="$BUILD/$APPNAME.app"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"
DIST="$HERE/dist"

echo "==> cleaning"
rm -rf "$BUILD" "$DIST"
mkdir -p "$RES" "$MACOS" "$DIST"

echo "==> baked config"
if [ -f "$HERE/../config.local.json" ]; then
  cp "$HERE/../config.local.json" "$RES/kino.config.json"
  echo "    using config.local.json"
else
  cp "$HERE/../config.example.json" "$RES/kino.config.json"
  echo "    WARNING: config.local.json missing — baked the EXAMPLE (placeholder token)."
fi

echo "==> app payload"
copy_tree() { # src dest  (rsync with the same excludes as the Windows build)
  rsync -a --exclude '.venv' --exclude '.venv_disabled' --exclude '__pycache__' \
        --exclude '*.pyc' --exclude '.git' --exclude '.git_disabled' \
        --exclude 'node_modules' --exclude '*.egg-info' "$1" "$2"
}
copy_tree "$ROOT/studio/"    "$RES/studio/"
copy_tree "$ROOT/video-use/" "$RES/video-use/"
cp "$HERE/../launch.py" "$RES/launch.py"

echo "==> bundling static ffmpeg (evermeet.cx)"
mkdir -p "$RES/ffmpeg"
dl_ff() { # name
  local url="https://evermeet.cx/ffmpeg/getrelease/$1/zip"
  if curl -fLsS "$url" -o "$BUILD/$1.zip"; then
    unzip -oq "$BUILD/$1.zip" -d "$RES/ffmpeg" && chmod +x "$RES/ffmpeg/$1"
    echo "    $1 ok"
  else
    echo "    WARNING: could not download $1 — the app will fall back to brew at first run."
  fi
}
dl_ff ffmpeg
dl_ff ffprobe

echo "==> icon"
LOGO="$ROOT/studio/static/kinoflow-logo.png"
if [ -f "$LOGO" ] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$BUILD/kino.iconset"; mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s "$LOGO" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s*2)); sips -z $d $d "$LOGO" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RES/kino.icns"
  echo "    kino.icns ok"
else
  echo "    WARNING: no logo or iconutil — app will use the default icon."
fi

echo "==> launcher + Info.plist"
cat > "$MACOS/KinoFlow" <<'LAUNCH'
#!/bin/bash
set -e
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
cd "$RES"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
exec uv run --python 3.11 "$RES/launch.py"
LAUNCH
chmod +x "$MACOS/KinoFlow"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Kino Flow</string>
  <key>CFBundleDisplayName</key><string>Kino Flow 1.0</string>
  <key>CFBundleIdentifier</key><string>com.threeships.kinoflow</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>KinoFlow</string>
  <key>CFBundleIconFile</key><string>kino.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "==> dmg"
STAGE="$BUILD/dmg"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APPNAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/Kino-Flow.dmg"

echo ""
echo "DONE -> $DIST/Kino-Flow.dmg"
echo "Install: open the dmg, drag 'Kino Flow' to Applications."
echo "First launch: right-click the app > Open (unsigned; Gatekeeper asks once)."

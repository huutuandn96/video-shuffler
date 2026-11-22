#!/bin/zsh
# Đóng gói "Video Shuffler.app" cho Mac chip Apple (arm64), kèm sẵn ffmpeg/ffprobe.
# Kết quả: dist/Video Shuffler.app
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
BUILD=$ROOT/build
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

# 1. ffmpeg tĩnh cho arm64 (không phụ thuộc Homebrew) - https://ffmpeg.martin-riedl.de
mkdir -p "$BUILD/ffmpeg"
for t in ffmpeg ffprobe; do
  if [ ! -x "$BUILD/ffmpeg/$t" ]; then
    echo "→ Tải $t"
    curl -fsSL --max-time 900 -o "$BUILD/ffmpeg/$t.zip" \
      "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/$t.zip"
    unzip -o -q "$BUILD/ffmpeg/$t.zip" -d "$BUILD/ffmpeg" && rm "$BUILD/ffmpeg/$t.zip"
  fi
done
xattr -cr "$BUILD/ffmpeg"

# 2. Icon
if [ ! -f "$BUILD/AppIcon.icns" ]; then
  echo "→ Tạo icon"
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
    --hide-scrollbars --window-size=1024,1024 --force-device-scale-factor=1 \
    --default-background-color=00000000 --screenshot="$BUILD/icon_1024.png" \
    "file://$ROOT/packaging/icon.html" 2>/dev/null
  ICONSET="$BUILD/AppIcon.iconset"
  rm -rf "$ICONSET" && mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"
fi

# 3. PyInstaller
.venv/bin/python -c "import PyInstaller" 2>/dev/null || uv pip install -q pyinstaller
echo "→ Đóng gói app"
.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name "Video Shuffler" \
  --target-arch arm64 \
  --icon "$BUILD/AppIcon.icns" \
  --osx-bundle-identifier "local.videoshuffler" \
  --add-data "$ROOT/vshuffle/ui:vshuffle/ui" \
  --add-binary "$BUILD/ffmpeg/ffmpeg:bin" \
  --add-binary "$BUILD/ffmpeg/ffprobe:bin" \
  --exclude-module tkinter \
  --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller" --specpath "$BUILD" \
  "$ROOT/packaging/launcher.py"

APP="$ROOT/dist/Video Shuffler.app"
# Khai báo app hỗ trợ chế độ tối, yêu cầu macOS 12 trở lên
/usr/libexec/PlistBuddy -c "Add :NSRequiresAquaSystemAppearance bool false" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 0.5.0" "$APP/Contents/Info.plist"
# Ký ad-hoc lại toàn bộ app sau khi sửa Info.plist (bắt buộc trên chip Apple)
codesign --force --deep --sign - "$APP"
echo "✓ Xong: $APP ($(du -sh "$APP" | cut -f1))"

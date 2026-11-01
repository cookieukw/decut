#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
APP="decut"

for asset in decut.svg com.decut.App.desktop decut.appdata.xml AppRun; do
  if [[ ! -f "$ROOT/assets/$asset" ]]; then
    echo "Missing application asset: $ROOT/assets/$asset" >&2
    exit 1
  fi
done

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtual environment not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

"$PYTHON" -m pip install --upgrade pyinstaller
rm -rf build dist AppDir "${APP}.AppImage"
"$PYTHON" -m PyInstaller --noconfirm --clean decut.spec

mkdir -p AppDir/usr/bin AppDir/usr/lib AppDir/usr/share/applications AppDir/usr/share/icons/hicolor/scalable/apps AppDir/usr/share/metainfo
cp -a "dist/$APP" "AppDir/usr/lib/$APP"
cat > AppDir/usr/bin/$APP <<'LAUNCHER'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/../lib/decut/decut" "$@"
LAUNCHER
chmod +x AppDir/usr/bin/$APP

FFMPEG="$(command -v ffmpeg || true)"
if [[ -n "$FFMPEG" ]]; then
  cp "$(readlink -f "$FFMPEG")" AppDir/usr/bin/ffmpeg
else
  echo "Warning: ffmpeg was not found. Silence analysis will require ffmpeg on the target PATH."
fi

cp "$ROOT/assets/com.decut.App.desktop" AppDir/usr/share/applications/com.decut.App.desktop
cp "$ROOT/assets/decut.appdata.xml" AppDir/usr/share/metainfo/decut.appdata.xml
cp "$ROOT/assets/decut.svg" AppDir/usr/share/icons/hicolor/scalable/apps/decut.svg

if command -v convert >/dev/null 2>&1; then
  convert -background none AppDir/usr/share/icons/hicolor/scalable/apps/decut.svg -resize 256x256 AppDir/decut.png
elif command -v magick >/dev/null 2>&1; then
  magick -background none AppDir/usr/share/icons/hicolor/scalable/apps/decut.svg -resize 256x256 AppDir/decut.png
else
  echo "Warning: ImageMagick not found; continuing with the SVG icon."
  cp AppDir/usr/share/icons/hicolor/scalable/apps/decut.svg AppDir/decut.svg
fi

cp "$ROOT/assets/AppRun" AppDir/AppRun
chmod +x AppDir/AppRun

LINUXDEPLOY="${LINUXDEPLOY:-$ROOT/.cache/linuxdeploy-x86_64.AppImage}"
if [[ ! -x "$LINUXDEPLOY" ]]; then
  mkdir -p "$(dirname "$LINUXDEPLOY")"
  curl -L --fail --output "$LINUXDEPLOY" https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
  chmod +x "$LINUXDEPLOY"
fi
"$LINUXDEPLOY" --appdir AppDir --output appimage
mv ./*.AppImage "${APP}.AppImage"
echo "Built ${APP}.AppImage"

#!/bin/bash
# Build Metadata Viewer.app and package it as a .dmg.
#
# exiftool is bundled: it is a Perl script and macOS ships /usr/bin/perl, so
# the app works on a Mac that has neither Homebrew nor exiftool installed.
set -euo pipefail

cd "$(dirname "$0")"
APP_NAME="Metadata Viewer"
VERSION="${VERSION:-1.0}"
DIST="dist"

echo "==> exiftool meeverpakken"
rm -rf tools
mkdir -p tools
EXIFTOOL_BIN="$(command -v exiftool || true)"
if [[ -z "$EXIFTOOL_BIN" ]]; then
    echo "exiftool niet gevonden. Installeer het eerst: brew install exiftool" >&2
    exit 1
fi
# Homebrew keeps the real script and its modules under libexec.
EXIFTOOL_REAL="$(readlink -f "$EXIFTOOL_BIN" 2>/dev/null || python3 -c \
    "import os,sys; print(os.path.realpath(sys.argv[1]))" "$EXIFTOOL_BIN")"
LIBEXEC="$(dirname "$(dirname "$EXIFTOOL_REAL")")/libexec"
if [[ -d "$LIBEXEC/lib/perl5/Image" ]]; then
    cp "$LIBEXEC/bin/exiftool" tools/
    mkdir -p tools/lib
    cp -R "$LIBEXEC/lib/perl5/Image" tools/lib/
    [[ -d "$LIBEXEC/lib/perl5/File" ]] && cp -R "$LIBEXEC/lib/perl5/File" tools/lib/
else
    # Plain exiftool distribution: script with a sibling lib directory.
    cp "$EXIFTOOL_REAL" tools/exiftool
    cp -R "$(dirname "$EXIFTOOL_REAL")/lib" tools/ 2>/dev/null || true
fi
chmod +x tools/exiftool
echo "    $(du -sh tools | cut -f1) meeverpakt"

echo "==> app bouwen"
rm -rf build "$DIST"
python3 -m PyInstaller \
    --noconfirm --clean --windowed \
    --name "$APP_NAME" \
    --osx-bundle-identifier "local.metadataviewer" \
    --icon icon.icns \
    --add-data "tools:tools" \
    --hidden-import mutagen \
    --hidden-import PIL._tkinter_finder \
    metadata_viewer.py >/dev/null

echo "==> dmg maken"
STAGING="$(mktemp -d)"
cp -R "$DIST/$APP_NAME.app" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
DMG="$DIST/$APP_NAME $VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" \
    -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGING"

echo
echo "Klaar:"
echo "  $DIST/$APP_NAME.app"
echo "  $DMG  ($(du -h "$DMG" | cut -f1))"

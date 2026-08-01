#!/bin/bash
# Baut das .txz-Paket und aktualisiert MD5/Version in der .plg
#
# Aufruf:  ./build.sh [version]
# Ohne Argument wird das aktuelle Datum als Version genutzt.

set -euo pipefail

NAME="fritz.power.monitor"
VERSION="${1:-$(date +%Y.%m.%d)}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/source"
OUT="$ROOT/packages"
PKG="$OUT/${NAME}-${VERSION}.txz"

command -v makepkg >/dev/null 2>&1 || {
    echo "Hinweis: 'makepkg' nicht gefunden - baue mit tar (fuer Unraid ausreichend)."
}

mkdir -p "$OUT"
rm -f "$OUT/${NAME}-"*.txz

# Rechte setzen: Verzeichnisse 755, Dateien 644
find "$SRC" -type d -exec chmod 755 {} \;
find "$SRC" -type f -exec chmod 644 {} \;

echo "Baue $PKG ..."
tar -C "$SRC" -cJf "$PKG" .

MD5=$(md5sum "$PKG" | cut -d' ' -f1)
SIZE=$(du -h "$PKG" | cut -f1)

# Version und MD5 in der .plg nachziehen
sed -i "s|<!ENTITY version   \".*\">|<!ENTITY version   \"${VERSION}\">|" "$ROOT/${NAME}.plg"

if grep -q "<!ENTITY md5" "$ROOT/${NAME}.plg"; then
    sed -i "s|<!ENTITY md5       \".*\">|<!ENTITY md5       \"${MD5}\">|" "$ROOT/${NAME}.plg"
fi

echo ""
echo "Fertig."
echo "  Paket:   $PKG  ($SIZE)"
echo "  Version: $VERSION"
echo "  MD5:     $MD5"
echo ""
echo "Naechste Schritte:"
echo "  git add -A && git commit -m \"Release $VERSION\" && git push"
echo "  Danach in Unraid installierbar ueber:"
echo "  https://raw.githubusercontent.com/ezek1el3000/fritz-unraid-power-monitor/main/${NAME}.plg"

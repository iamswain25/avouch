#!/usr/bin/env bash
# Regenerate site/assets/og.png and og-ko.png (1200x630) from the HTML sources.
# Requires Google Chrome. Renders at 2x then downscales for crisp type.
set -euo pipefail
cd "$(dirname "$0")"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for lang in "" "-ko"; do
  name="og${lang}"
  "$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1200,630 --screenshot="$TMP/${name}@2x.png" \
    "file://$PWD/${name}.html" >/dev/null 2>&1
  sips -z 630 1200 "$TMP/${name}@2x.png" --out "../../site/assets/${name}.png" >/dev/null
  echo "wrote site/assets/${name}.png"
done

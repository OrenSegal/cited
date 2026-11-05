#!/bin/sh
# Re-run the sample check and re-render its preview. Needs network and Google
# Chrome (macOS path); pngquant is optional and only shrinks the PNG.
# Exit 1 from cited is expected: the sample has claims that are not on the page.
set -u
here=$(cd "$(dirname "$0")" && pwd -P)
"$here/../../bin/cited" "$here/sample-draft.md" --certificate "$here/sample-certificate.html" --json > "$here/sample-report.json"
chrome=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
"$chrome" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=900,1400 \
  --screenshot="$here/sample-certificate.png" "file://$here/sample-certificate.html"
if command -v pngquant >/dev/null 2>&1; then
  pngquant --force --quality=70-90 --output "$here/sample-certificate.png" "$here/sample-certificate.png"
fi

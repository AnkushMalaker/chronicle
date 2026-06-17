#!/usr/bin/env bash
#
# Build (and code-sign) the Chronicle Wearable .app bundle via py2app.
#
# Why sign: macOS TCC ties Screen Recording / Accessibility grants to a code
# signature. An ad-hoc signature (`-s -`) changes its cdhash on every rebuild,
# so TCC forgets your grants each time you rebuild. To keep grants across
# rebuilds, sign with a stable self-signed identity and pass it via CODESIGN_ID:
#
#   1. Keychain Access -> Certificate Assistant -> Create a Certificate
#        Name: "Chronicle Dev"   Identity Type: Self Signed Root
#        Certificate Type: Code Signing
#   2. CODESIGN_ID="Chronicle Dev" ./build_app.sh
#
# Without CODESIGN_ID it falls back to ad-hoc signing (fine for a one-off test).
set -euo pipefail
# Build from the isolated bundle/ dir (no pyproject.toml there -> py2app is happy).
cd "$(dirname "$0")/bundle"

APP_NAME="Chronicle Wearable.app"
SIGN_ID="${CODESIGN_ID:--}"   # default: ad-hoc

echo "==> Cleaning previous build"
rm -rf build dist

echo "==> Building with py2app"
uv run --with py2app python setup_app.py py2app

# py2app names the bundle after the script (menu_app.app); give it a friendly name.
if [ -d "dist/menu_app.app" ]; then
  rm -rf "dist/${APP_NAME}"
  mv "dist/menu_app.app" "dist/${APP_NAME}"
fi

echo "==> Code-signing (identity: ${SIGN_ID})"
codesign --force --deep --sign "${SIGN_ID}" "dist/${APP_NAME}"

echo
echo "Built: extras/local-wearable-client/bundle/dist/${APP_NAME}"
echo "Run:   open \"bundle/dist/${APP_NAME}\""
echo
echo "First launch will prompt for Screen Recording (re-open the app once after"
echo "approving) and you must toggle Accessibility on manually in"
echo "System Settings -> Privacy & Security -> Accessibility."

#!/bin/sh
# Install nimcode from its latest GitHub Release (Linux / macOS).
#   curl -LsSf https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.sh | sh
# NIMCODE_VERSION=v0.7.0rc1 installs a specific tag (incl. pre-releases).
set -eu
REPO="PrathamPai-2007/nimcode"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  PLAT="manylinux_2_17_x86_64" ;;
  Darwin-arm64)  PLAT="macosx_11_0_arm64" ;;
  Darwin-x86_64) PLAT="macosx_10_12_x86_64" ;;
  *) echo "No prebuilt nimcode for $(uname -s)-$(uname -m); see the README for a source install." >&2; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi

if [ -n "${NIMCODE_VERSION:-}" ]; then
  API="https://api.github.com/repos/$REPO/releases/tags/$NIMCODE_VERSION"
else
  API="https://api.github.com/repos/$REPO/releases/latest"
fi
URL=$(curl -fsSL "$API" | grep -o '"browser_download_url": *"[^"]*"' | grep "$PLAT" | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')
[ -n "$URL" ] || { echo "No $PLAT wheel found in release ($API)." >&2; exit 1; }

uv tool install --force --python 3.12 "$URL"
echo
echo "Installed. Set NVIDIA_API_KEY (or put it in ~/.nimcode/.env), then run: nimcode"

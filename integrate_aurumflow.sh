#!/usr/bin/env bash
# Safe stager for aurumflow → Omni. Does NOT touch the live bot.
# It locates the zip, unpacks to a sandbox, and reports what's inside so the
# integration can be reviewed before anything copies into python/.
#
#   usage: ./integrate_aurumflow.sh [path-to-zip]
set -uo pipefail
OMNI="$HOME/Omni-full-ALGO-Trading-Bot"
STAGE="$OMNI/_staging/aurumflow"

# 1. find the zip
ZIP="${1:-}"
if [ -z "$ZIP" ]; then
  for c in "$HOME/aurumflow_v1.0.zip" "$HOME/Downloads/aurumflow_v1.0.zip" \
           "/tmp/aurumflow_v1.0.zip" "$OMNI/aurumflow_v1.0.zip"; do
    [ -f "$c" ] && ZIP="$c" && break
  done
fi
if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  echo "✗ aurumflow zip not found. Place it at ~/aurumflow_v1.0.zip (or pass the path)."
  echo "  If it lives on a remote box:  scp user@host:/home/team/shared/aurumflow_v1.0.zip ~/"
  exit 1
fi
echo "✓ found zip: $ZIP ($(du -h "$ZIP" | cut -f1))"

# 2. unpack to sandbox (never into the live bot)
rm -rf "$STAGE"; mkdir -p "$STAGE"
if ! unzip -q "$ZIP" -d "$STAGE"; then
  echo "✗ unzip failed — is it a valid zip?"; exit 1
fi

# 3. report contents for review
echo
echo "=== contents ==="
find "$STAGE" -type f | sed "s|$STAGE/||" | head -60
echo
echo "=== python strategy files detected ==="
find "$STAGE" -name "*.py" | xargs grep -l -iE "class .*strategy|def (generate_signal|on_bar|scan)" 2>/dev/null | sed "s|$STAGE/||" || echo "(none obvious — review manually)"
echo
echo "=== requirements / config in the package ==="
find "$STAGE" -iname "requirements*.txt" -o -iname "*.json" -o -iname "config*" 2>/dev/null | sed "s|$STAGE/||" | head
echo
echo "→ Staged at: $STAGE"
echo "→ NOTHING copied into the live bot yet. Review, then integrate the reviewed files into python/."

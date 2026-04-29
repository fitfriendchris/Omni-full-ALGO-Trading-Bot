#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  scan_omni_files.sh — locate every OMNI-related file on this Mac.
#
#  Searches: home dir, Desktop, Documents, Downloads, iCloud Drive, Library,
#  and the MT5 sandboxes for files that look like they belong to OMNI-ICT
#  (by name pattern OR by content keyword). Reports anything outside the
#  canonical workspace at /Users/owner/omni-ict so you can spot drift /
#  duplicates / missing additions.
#
#  Run:
#      bash scan_omni_files.sh > /tmp/omni_scan.txt
#      open -e /tmp/omni_scan.txt
# ═══════════════════════════════════════════════════════════════════════════

set -u
WORKSPACE="/Users/owner/omni-ict"
HOME_DIR="$HOME"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
MT5_DIR="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"

# Patterns that strongly imply OMNI-ICT
NAME_PATTERNS=(
  "*OmniExport*"
  "*OmniExecutor*"
  "*OmniSignalOverlay*"
  "*omni_data*"
  "*omni_cmd*"
  "*omni_result*"
  "*omni_leader*"
  "omni_pine_overlay*"
  "tv_pine_alert*"
  "omni-ict*"
  "watchdog_state.json"
  "trader_state*.json"
  "trade_journal*.log"
  "trade_memory*.json"
  "feature_store.db*"
)

CONTENT_PATTERNS='(OMNI-ICT|OmniExport|omni-ict|OMNI_LICENSE_KEY|OMNI_TELEGRAM_TOKEN|com\.omni\.ict)'

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
hdr() { echo -e "${CYAN}${BOLD}$*${RESET}"; }
note() { echo -e "${YELLOW}$*${RESET}"; }

echo
hdr "═══ OMNI-ICT file inventory — $(date) ═══"
echo "workspace: $WORKSPACE"
echo

# ── 1. Workspace contents (sanity) ─────────────────────────────────────────
hdr "── workspace top-level (1 deep) ──"
ls -la "$WORKSPACE" 2>/dev/null | grep -v '^d.*\(__pycache__\|venv\)$' | head -60
echo

hdr "── workspace Python files ──"
find "$WORKSPACE/python" -maxdepth 2 -name "*.py" -not -path "*venv*" -not -path "*__pycache__*" 2>/dev/null \
  | sed "s|$WORKSPACE/||" | sort
echo

hdr "── workspace MQL5 files ──"
find "$WORKSPACE/mql5" -maxdepth 3 \( -name "*.mq5" -o -name "*.mq4" -o -name "*.ex5" -o -name "*.mqh" \) 2>/dev/null \
  | sed "s|$WORKSPACE/||" | sort
echo

# ── 2. OMNI files OUTSIDE the workspace (drift detector) ──────────────────
hdr "── OMNI files outside the workspace (drift / duplicates) ──"
SEARCH_ROOTS=(
  "$HOME_DIR/Desktop"
  "$HOME_DIR/Documents"
  "$HOME_DIR/Downloads"
  "$ICLOUD"
)
for root in "${SEARCH_ROOTS[@]}"; do
  [[ -d "$root" ]] || continue
  for pat in "${NAME_PATTERNS[@]}"; do
    # -path excludes results inside the workspace mirror
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      [[ "$f" == "$WORKSPACE/"* ]] && continue
      sz=$(stat -f%z "$f" 2>/dev/null || echo "?")
      mt=$(stat -f%Sm -t %Y-%m-%d "$f" 2>/dev/null || echo "?")
      printf "  %s  %8s bytes  %s\n" "$mt" "$sz" "$f"
    done < <(find "$root" -name "$pat" 2>/dev/null | head -50)
  done
done
echo

# ── 3. MT5 EA + data file locations ───────────────────────────────────────
hdr "── MT5 sandbox: where omni_data.json + EAs live ──"
if [[ -d "$MT5_DIR" ]]; then
  find "$MT5_DIR" -maxdepth 8 \( -name "omni_data.json*" -o -name "omni_cmd.txt" -o -name "omni_result.txt" -o -name "omni_leader.lock" -o -name "OmniExport*" -o -name "OmniExecutor*" -o -name "OmniSignalOverlay*" \) 2>/dev/null \
    | while IFS= read -r f; do
        sz=$(stat -f%z "$f" 2>/dev/null || echo "?")
        mt=$(stat -f%Sm -t "%Y-%m-%d %H:%M:%S" "$f" 2>/dev/null || echo "?")
        printf "  %s  %10s bytes  %s\n" "$mt" "$sz" "$f"
      done
else
  note "  MT5 wine sandbox not found at $MT5_DIR"
fi
echo

# ── 4. Compare workspace EA vs installed EA (drift detector) ──────────────
hdr "── drift check: workspace EA vs installed EA ──"
for ea in OmniExport_v4.mq5 OmniExecutor.mq5 OmniSignalOverlay.mq5; do
  src="$WORKSPACE/mql5/$ea"
  [[ -f "$src" ]] || continue
  installed=$(find "$MT5_DIR" -name "$ea" 2>/dev/null | head -1)
  if [[ -z "$installed" ]]; then
    note "  $ea: NOT INSTALLED (workspace has it; MT5 doesn't)"
  elif cmp -s "$src" "$installed"; then
    echo "  $ea: ✓ in sync"
  else
    sz_src=$(stat -f%z "$src" 2>/dev/null)
    sz_ins=$(stat -f%z "$installed" 2>/dev/null)
    mt_src=$(stat -f%Sm -t %Y-%m-%d "$src" 2>/dev/null)
    mt_ins=$(stat -f%Sm -t %Y-%m-%d "$installed" 2>/dev/null)
    note "  $ea: DIFFERS — workspace=${sz_src}B/${mt_src}, installed=${sz_ins}B/${mt_ins}"
    note "    installed at: $installed"
  fi
done
echo

# ── 5. Python venv + installed package quick-list ─────────────────────────
hdr "── Python venv health ──"
VENV_PY="$WORKSPACE/venv/bin/python3"
if [[ -x "$VENV_PY" ]]; then
  echo "  python: $($VENV_PY --version 2>&1)"
  echo "  installed packages (top 30):"
  "$VENV_PY" -m pip list 2>/dev/null | head -32 | sed 's/^/    /'
else
  note "  venv python not found at $VENV_PY"
fi
echo

# ── 6. LaunchAgent + plist ────────────────────────────────────────────────
hdr "── LaunchAgent ──"
PLIST="$HOME/Library/LaunchAgents/com.omni.ict.autonomy.plist"
if [[ -f "$PLIST" ]]; then
  echo "  installed: $PLIST"
  echo "  launchctl: $(launchctl list 2>/dev/null | grep -i omni || echo 'not loaded')"
  echo "  src/installed sync: $(cmp -s "$WORKSPACE/com.omni.ict.autonomy.plist" "$PLIST" && echo 'in sync' || echo 'DIFFERS')"
else
  note "  not installed at $PLIST"
fi
echo

# ── 7. Recent log activity ────────────────────────────────────────────────
hdr "── Recent log activity (sizes + last modified) ──"
if [[ -d "$WORKSPACE/logs" ]]; then
  ls -lah "$WORKSPACE/logs"/*.log 2>/dev/null | awk '{printf "  %s %s %s %s\n", $5, $6, $7, $9}'
fi
echo

# ── 8. Optional content scan (slow) ───────────────────────────────────────
if [[ "${1:-}" == "--content" ]]; then
  hdr "── content scan (--content): files mentioning OMNI keywords ──"
  for root in "${SEARCH_ROOTS[@]}"; do
    [[ -d "$root" ]] || continue
    grep -rlE "$CONTENT_PATTERNS" "$root" 2>/dev/null \
      | grep -v "$WORKSPACE" \
      | head -50 \
      | while IFS= read -r f; do
          echo "  $f"
        done
  done
fi

hdr "═══ scan complete ═══"

#!/bin/bash
# Installs: `soma` wrapper in ~/bin, sync LaunchAgents, MCP server.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
LABEL="com.soma.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/bin" "$HOME/.soma"

# --- wrapper ---------------------------------------------------------------
cat > "$HOME/bin/soma" <<EOF
#!/bin/bash
cd "$REPO" && exec "$PY" -m soma "\$@"
EOF
chmod +x "$HOME/bin/soma"
echo "✓ ~/bin/soma"

# --- two cadences ---------------------------------------------------------
# Freshness is NOT limited by our polling: measured on 2026-08-07, Google's
# watch→phone→cloud chain lags 2-3.5 hours. Syncing every minute would cut
# total delay by 7% while burning quota.
#
#   fast  every 15 min, 3-day window  → minimal cost, data as soon as it lands
#   deep  once a day, 90 days + 14 days of per-minute HR → late corrections,
#         backfill, recomputation
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>soma</string>
    <string>sync</string><string>--fast</string><string>--cloud</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/.soma/sync.log</string>
  <key>StandardErrorPath</key><string>$HOME/.soma/sync.log</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$UID" "$PLIST"
echo "✓ LaunchAgent $LABEL (fast: every 15 min, 3 days)"

DEEP="com.soma.deep"
DEEP_PLIST="$HOME/Library/LaunchAgents/$DEEP.plist"
launchctl bootout "gui/$UID/$DEEP" 2>/dev/null || true
cat > "$DEEP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$DEEP</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>soma</string>
    <string>sync</string><string>--days</string><string>90</string>
    <string>--hr-days</string><string>14</string><string>--cloud</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer>
    <key>Minute</key><integer>20</integer></dict>
  <key>StandardOutPath</key><string>$HOME/.soma/sync.log</string>
  <key>StandardErrorPath</key><string>$HOME/.soma/sync.log</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$UID" "$DEEP_PLIST"
echo "✓ LaunchAgent $DEEP (deep: every night at 4:20)"

# --- always-on dashboard --------------------------------------------------
# A tiny process listening on loopback only, so the address is stable and
# bookmarkable instead of having to run a command to look.
DASH="com.soma.dashboard"
DASH_PLIST="$HOME/Library/LaunchAgents/$DASH.plist"
launchctl bootout "gui/$UID/$DASH" 2>/dev/null || true
cat > "$DASH_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$DASH</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>soma</string>
    <string>dashboard</string><string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.soma/dashboard.log</string>
  <key>StandardErrorPath</key><string>$HOME/.soma/dashboard.log</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$UID" "$DASH_PLIST"
echo "✓ LaunchAgent $DASH — http://127.0.0.1:4748"

# --- MCP: the data inside any Claude session --------------------------------
if command -v claude >/dev/null 2>&1; then
  # register the wrapper, not `python3 -m soma`: the wrapper carries the repo
  # directory with it, so it works from whatever folder Claude starts in
  claude mcp remove soma --scope user 2>/dev/null || true
  claude mcp add soma --scope user -- "$HOME/bin/soma" mcp >/dev/null 2>&1 \
    && echo "✓ MCP server 'soma' registered for Claude" \
    || echo "  (register manually: claude mcp add soma --scope user -- ~/bin/soma mcp)"
else
  echo "  claude not found: skipping MCP registration"
fi

echo
echo "Next step:  soma doctor"

#!/bin/bash
# Removes automation and integrations. Data in ~/.soma stays: delete it
# yourself if you really want to lose it.
set -uo pipefail

for LABEL in com.soma.sync com.soma.deep com.soma.dashboard; do
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && echo "✓ LaunchAgent $LABEL removed"
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
done
rm -f "$HOME/bin/soma" && echo "✓ wrapper removed"
command -v claude >/dev/null 2>&1 && claude mcp remove soma --scope user 2>/dev/null \
  && echo "✓ MCP server unregistered"

echo
echo "Remaining: ~/.soma (data) and the credentials in Keychain."
echo "To delete those too:"
echo "  rm -rf ~/.soma"
for s in coros.email coros.password google_health.tokens fitbit.tokens supabase; do
  echo "  security delete-generic-password -a soma -s soma.$s"
done

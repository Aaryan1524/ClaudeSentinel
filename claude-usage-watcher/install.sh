#!/usr/bin/env bash
# Deploys claude_usage_watcher.py to ~/bin, which is what the registered
# hooks and the launchd job actually execute. Run this after any edit to
# the script in this repo -- the two copies are NOT auto-synced.
#
# Why a separate deployed copy exists at all, instead of pointing hooks/
# launchd straight at this repo: ~/Desktop (and ~/Documents, ~/Downloads)
# are TCC-protected on macOS. launchd-run background processes get a
# silent "Operation not permitted" trying to read a script there, even
# though your interactive Terminal can read it fine. ~/bin sidesteps that
# entirely.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p ~/bin
cp "$SCRIPT_DIR/claude_usage_watcher.py" ~/bin/claude_usage_watcher.py
chmod +x ~/bin/claude_usage_watcher.py
echo "deployed $SCRIPT_DIR/claude_usage_watcher.py -> ~/bin/claude_usage_watcher.py"

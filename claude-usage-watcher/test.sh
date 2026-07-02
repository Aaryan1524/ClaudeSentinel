#!/usr/bin/env bash
# Local, side-effect-free test pass for claude_usage_watcher.py.
# Uses a scratch state file (never touches ~/.claude-usage-watcher/) and
# never sends a real ntfy push (only exercises --dry-run and hit-limit,
# neither of which calls send_ntfy).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER="$SCRIPT_DIR/claude_usage_watcher.py"

export CLAUDE_NOTIFIER_STATE=/tmp/watcher_test_state.json
rm -f "$CLAUDE_NOTIFIER_STATE"
rm -f "$(dirname "$CLAUDE_NOTIFIER_STATE")/stop_failure_events.jsonl"

echo "== fresh window starts, ~5h out =="
python3 "$WATCHER" record
python3 "$WATCHER" status

echo
echo "== simulate an elapsed window, confirm check --dry-run previews without consuming it =="
PAST=$(python3 -c "from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())")
python3 "$WATCHER" correct five_hour "$PAST"
echo "-- first check (expect: would notify) --"
python3 "$WATCHER" check --dry-run
echo "-- second check (expect: would notify AGAIN -- --dry-run never sets notified=True on purpose," \
     "so a later real check still fires for real; idempotency of the real path is only provable" \
     "with a live send, exercised in the README's end-to-end test) --"
python3 "$WATCHER" check --dry-run

echo
echo "== simulate a StopFailure(rate_limit) event with an unknown payload shape =="
echo '{"session_id":"test","hook_event_name":"StopFailure","error_type":"rate_limit"}' \
  | python3 "$WATCHER" hit-limit
echo "-- status (expect: confirmed blocked at) --"
python3 "$WATCHER" status
echo "-- logged raw payload --"
cat "$(dirname "$CLAUDE_NOTIFIER_STATE")/stop_failure_events.jsonl"

rm -f "$CLAUDE_NOTIFIER_STATE"
rm -f "$(dirname "$CLAUDE_NOTIFIER_STATE")/stop_failure_events.jsonl"

echo
echo "All scenarios ran. Review the output above against README.md's expectations."

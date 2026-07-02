# Build brief: local Claude usage-reset notifier

You're picking up a project from a planning conversation on claude.ai. Read this whole brief before changing anything — the constraints below came from actually checking Anthropic's support docs and Claude Code's hooks reference, not assumptions, and a couple of them rule out approaches that would otherwise look reasonable.

## Goal

A fully local tool — no browser, no browser extension, no manually checking claude.ai — that watches Claude Code activity, tracks when the 5-hour usage window and the weekly cap will reset, and pushes a phone notification via ntfy.sh the moment a window resets. The point is to stop needing to check anything: get pinged, go back to work.

## Confirmed facts — don't re-derive these

- The 5-hour session window is a fixed timer anchored to the first message of a new window. It resets exactly 5 hours later regardless of how much is used inside that window — usage volume does not move the reset time.
- A separate weekly cap runs in parallel, tracked separately for Opus vs. all other models. Its exact reset mechanics (rolling-from-first-use like the 5-hour window, vs. a fixed weekly boundary) are **not publicly documented**. Do not auto-infer it — only accept it via explicit correction.
- Usage on claude.ai, in Claude Code, and in Claude Desktop all draws from the same pool. A tool that only watches Claude Code activity has a blind spot for usage that happens purely through the web/mobile app.
- There is no push/webhook API for "your usage window just reset." This has to be local inference plus polling, not an event subscription.
- Claude Code has a `StopFailure` hook event (added in CLI v2.1.78) with a documented `rate_limit` matcher — it fires the instant a turn ends because of a rate limit. Its exact JSON payload fields are **not documented** (open doc-gap issue: github.com/anthropics/claude-code/issues/35620). Treat every field name in the code below as an unverified guess until proven otherwise against a real payload.
- Explicitly out of scope, by request: browser extensions, DOM or page scraping of claude.ai. Local only.

## Current state of the code

`claude_usage_watcher.py` below is tested — dry-run/idempotency, corrupted-state-file recovery, and failed-send retry have all been verified — but `hit-limit`'s field-guessing has never seen a real `StopFailure` payload, since that requires actually hitting a live rate limit.

```python
#!/usr/bin/env python3
"""
claude_usage_watcher.py
========================
Tracks Claude's rolling usage window(s) locally and pushes a phone
notification (via ntfy.sh) the moment one resets.

WHY IT WORKS THIS WAY
----------------------
There is no push/webhook API for "your usage window just reset" -- the two
ground-truth signals (claude.ai/settings/usage, and Claude Code's own
"limit reached, resets at <time>" banner) are built for humans to read, not
machines to poll. So this tool infers the 5-hour reset time locally using
Anthropic's documented rule:

    The 5-hour window starts on your FIRST message after the previous
    window expired, and resets exactly 5 hours later -- regardless of how
    much (or little) you use inside that window.

That means the reset time is fully computable with zero scraping, AS LONG
AS this tool sees your first message of each window. It sees Claude Code
activity via a hook (see SETUP). It will NOT see messages sent purely
through the claude.ai web/mobile app -- if you split usage across surfaces,
the inferred time can drift. Use `correct` to feed it a real observed
value (e.g. by typing in the reset time you see in a CLI warning banner
or the claude.ai/settings/usage page) -- corrections always win until
they themselves expire.

There is also a `hit-limit` command wired to Claude Code's `StopFailure`
hook (matcher "rate_limit"), which fires the instant a turn ends because
you actually got rate-limited -- a real, documented, fully local signal,
no browser and no output-scraping involved. Anthropic added this event in
Claude Code v2.1.78 but hasn't published its exact JSON fields yet
(github.com/anthropics/claude-code/issues/35620), so `hit-limit` always
logs the raw payload to stop_failure_events.jsonl for you to inspect once
for real, and only updates reset_at if it recognizes a timestamp or
retry-delay field in there. Worst case it's a no-op beyond the log line
and a "confirmed blocked at this exact moment" note in `status` -- which
`record`'s inference alone can't give you.

The weekly cap is NOT auto-inferred, on purpose: the docs don't publish
its exact mechanics (e.g. whether it's rolling-from-first-use like the
5-hour window, or a fixed weekly boundary), so a guess here would just be
a guess. Seed it with `correct weekly <timestamp>` whenever you see a real
value and this tool will track it from there.

SETUP
-----
1. Save this file somewhere permanent, e.g. ~/bin/claude_usage_watcher.py

2. Pick a private ntfy.sh topic name (anyone who knows the exact topic
   string can read it, so make it unguessable) and export it:

     export CLAUDE_NOTIFIER_NTFY_TOPIC="aaryan-claude-resets-x7q2"

3. Register two hooks in ~/.claude/settings.json -- one marks activity,
   the other confirms the exact moment you actually get capped:

     {
       "hooks": {
         "UserPromptSubmit": [
           { "hooks": [ { "type": "command",
                           "command": "python3 ~/bin/claude_usage_watcher.py record" } ] }
         ],
         "StopFailure": [
           { "matcher": "rate_limit",
             "hooks": [ { "type": "command",
                           "command": "python3 ~/bin/claude_usage_watcher.py hit-limit" } ] }
         ]
       }
     }

4. Add a cron entry that checks for a reset every 5 minutes:

     */5 * * * * CLAUDE_NOTIFIER_NTFY_TOPIC=aaryan-claude-resets-x7q2 python3 ~/bin/claude_usage_watcher.py check

5. Install the ntfy app on your phone and subscribe to the same topic.

USAGE
-----
  claude_usage_watcher.py record                    call this from the UserPromptSubmit hook
  claude_usage_watcher.py hit-limit                  call this from the StopFailure(rate_limit) hook
  claude_usage_watcher.py check [--dry-run]          call this from cron
  claude_usage_watcher.py correct five_hour <ISO8601 timestamp>
  claude_usage_watcher.py correct weekly <ISO8601 timestamp>
  claude_usage_watcher.py status                     human-readable dump
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(
    os.environ.get("CLAUDE_NOTIFIER_STATE", "~/.claude-usage-watcher/state.json")
).expanduser()

FIVE_HOUR_WINDOW = timedelta(hours=5)
NTFY_TOPIC_ENV = "CLAUDE_NOTIFIER_NTFY_TOPIC"
NTFY_SERVER = os.environ.get("CLAUDE_NOTIFIER_NTFY_SERVER", "https://ntfy.sh")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %I:%M %p %Z")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_record(_args) -> None:
    """Call from a Claude Code hook on every prompt. Cheap no-op if a
    5-hour window is already active -- extra usage never moves the reset
    time, so there's nothing to update."""
    state = load_state()
    window = state.get("five_hour", {})
    reset_at = window.get("reset_at")
    now = now_utc()

    if reset_at is None or now >= parse_iso(reset_at):
        state["five_hour"] = {
            "window_start": now.isoformat(),
            "reset_at": (now + FIVE_HOUR_WINDOW).isoformat(),
            "notified": False,
            "source": "inferred",
        }
        save_state(state)
    # else: window already open, nothing to do.


def cmd_correct(args) -> None:
    """Overwrite a tracked window with an observed ground-truth timestamp.
    Use this whenever you have a real value (a CLI banner, /status
    output, hit-limit's captured payload, etc). Manual corrections take
    priority and persist until that reset time itself passes."""
    kind = args.kind
    target_time = parse_iso(args.timestamp)

    state = load_state()
    state[kind] = {
        "reset_at": target_time.isoformat(),
        "notified": False,
        "source": "observed",
    }
    save_state(state)
    print(f"{kind}: reset_at set to {fmt_local(target_time)}")


def cmd_hit_limit(_args) -> None:
    """Call from a StopFailure hook (matcher: "rate_limit"). Fires the
    instant a turn ends because you hit the rate limit -- the earliest,
    most reliable local signal available, and it needs no browser and no
    output-scraping. Anthropic added this event in Claude Code v2.1.78 but
    (as of this writing) hasn't published its exact JSON fields --
    tracked at github.com/anthropics/claude-code/issues/35620. So this
    always logs the raw payload for you to inspect once for real, and
    only touches reset_at if it recognizes a timestamp or retry-delay
    field in it. If none of the guessed keys exist, it's a no-op beyond
    the log line and a "you were confirmed blocked at this moment" note
    -- which is still useful: it's a fact `record`'s inference can't see."""
    raw = sys.stdin.read()
    now = now_utc()

    log_path = STATE_PATH.parent / "stop_failure_events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps({"seen_at": now.isoformat(), "raw": raw}) + "\n")

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    state = load_state()
    five = state.setdefault("five_hour", {})
    five["confirmed_blocked_at"] = now.isoformat()

    updated = False
    for key in ("reset_at", "resets_at", "reset_time"):
        if key in payload:
            try:
                five["reset_at"] = parse_iso(payload[key]).isoformat()
                five["notified"] = False
                five["source"] = f"stop_failure:{key}"
                updated = True
            except (ValueError, TypeError):
                pass
            break

    if not updated:
        for key, is_ms in (("retry_after_ms", True), ("retry_after_seconds", False), ("retry_after", False)):
            if key in payload:
                try:
                    seconds = float(payload[key]) / (1000 if is_ms else 1)
                    five["reset_at"] = (now + timedelta(seconds=seconds)).isoformat()
                    five["notified"] = False
                    five["source"] = f"stop_failure:{key}"
                except (ValueError, TypeError):
                    pass
                break

    save_state(state)


def cmd_check(args) -> None:
    """Call from cron every few minutes. Fires an ntfy.sh push exactly
    once per window when the stored reset_at time has passed."""
    state = load_state()
    now = now_utc()
    changed = False

    labels = {
        "five_hour": "5-hour Claude usage window",
        "weekly": "Weekly Claude usage cap",
    }

    for kind, label in labels.items():
        entry = state.get(kind)
        if not entry or not entry.get("reset_at"):
            continue
        if entry.get("notified"):
            continue

        reset_at = parse_iso(entry["reset_at"])
        if now >= reset_at:
            message = f"{label} just reset. Go."
            if args.dry_run:
                # Preview only -- never mutate state, so a real check
                # afterwards still fires for real.
                print(f"[dry-run] would notify: {message}")
                continue

            if send_ntfy(message, title="Claude usage reset"):
                entry["notified"] = True
                changed = True
            # else: leave notified=False so the next cron tick retries.

    if changed:
        save_state(state)


def cmd_status(_args) -> None:
    state = load_state()
    now = now_utc()
    if not state:
        print("No state recorded yet. Run `record` from a Claude Code hook first.")
        return

    for kind in ("five_hour", "weekly"):
        entry = state.get(kind)
        print(f"\n{kind}:")
        if not entry or not entry.get("reset_at"):
            print("  no data yet")
            continue
        reset_at = parse_iso(entry["reset_at"])
        remaining = reset_at - now
        status = "PASSED (waiting for check to notify)" if remaining.total_seconds() <= 0 else "active"
        print(f"  source:    {entry.get('source', 'unknown')}")
        print(f"  resets at: {fmt_local(reset_at)}  ({status})")
        if remaining.total_seconds() > 0:
            mins = int(remaining.total_seconds() // 60)
            print(f"  time left: {mins // 60}h {mins % 60}m")
        print(f"  notified:  {entry.get('notified', False)}")
        if entry.get("confirmed_blocked_at"):
            print(f"  confirmed blocked at: {fmt_local(parse_iso(entry['confirmed_blocked_at']))} (via StopFailure)")


# --------------------------------------------------------------------------
# ntfy.sh
# --------------------------------------------------------------------------

def send_ntfy(message: str, title: str = None) -> bool:
    topic = os.environ.get(NTFY_TOPIC_ENV)
    if not topic:
        print(
            f"error: set {NTFY_TOPIC_ENV} to your ntfy.sh topic name",
            file=sys.stderr,
        )
        return False

    url = f"{NTFY_SERVER.rstrip('/')}/{topic}"
    headers = {"Title": title} if title else {}
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.URLError as e:
        print(f"error: failed to reach ntfy ({e})", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track and notify on Claude usage window resets.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("record", help="Mark activity now (call from a Claude Code hook)")

    sub.add_parser("hit-limit", help="Call from a StopFailure(rate_limit) hook")

    p_check = sub.add_parser("check", help="Check for a reset and notify (call from cron)")
    p_check.add_argument("--dry-run", action="store_true", help="Print instead of sending a push")

    p_correct = sub.add_parser("correct", help="Feed in an observed reset timestamp")
    p_correct.add_argument("kind", choices=["five_hour", "weekly"])
    p_correct.add_argument("timestamp", help="ISO 8601, e.g. 2026-07-01T14:00:00-04:00")

    sub.add_parser("status", help="Print current tracked state")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "record": cmd_record,
        "hit-limit": cmd_hit_limit,
        "check": cmd_check,
        "correct": cmd_correct,
        "status": cmd_status,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
```

## What to do, in order

1. Write the script above to disk (e.g. `~/bin/claude_usage_watcher.py`) and `chmod +x` it.

2. Re-run its test scenarios locally to confirm nothing broke in transit — these are the exact checks it was built against:

   ```bash
   export CLAUDE_NOTIFIER_STATE=/tmp/watcher_test_state.json
   rm -f "$CLAUDE_NOTIFIER_STATE"

   # fresh window starts, ~5h out
   python3 claude_usage_watcher.py record
   python3 claude_usage_watcher.py status

   # simulate an elapsed window, confirm check --dry-run fires exactly once
   PAST=$(python3 -c "from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())")
   python3 claude_usage_watcher.py correct five_hour "$PAST"
   python3 claude_usage_watcher.py check --dry-run   # should print "would notify"
   python3 claude_usage_watcher.py check --dry-run   # should print nothing (idempotent)

   # simulate a StopFailure(rate_limit) event with an unknown payload shape
   echo '{"session_id":"test","hook_event_name":"StopFailure","error_type":"rate_limit"}' \
     | python3 claude_usage_watcher.py hit-limit
   python3 claude_usage_watcher.py status   # should now show "confirmed blocked at"
   cat "$(dirname "$CLAUDE_NOTIFIER_STATE")/stop_failure_events.jsonl"

   rm -f "$CLAUDE_NOTIFIER_STATE"
   ```

3. Register both hooks in `~/.claude/settings.json`. **Merge with whatever hooks config is already there — do not overwrite the file.**

   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [ { "type": "command", "command": "python3 ~/bin/claude_usage_watcher.py record" } ] }
       ],
       "StopFailure": [
         { "matcher": "rate_limit",
           "hooks": [ { "type": "command", "command": "python3 ~/bin/claude_usage_watcher.py hit-limit" } ] }
       ]
     }
   }
   ```

4. Ask me for a private ntfy.sh topic name (something unguessable — anyone who knows the exact string can read the topic), export it as `CLAUDE_NOTIFIER_NTFY_TOPIC`, and make sure it's set wherever the cron job will actually read it from — cron does not inherit your shell's exported env vars, so put it directly in the crontab line or source a file from within it.

5. Detect the OS and set up the recurring `check` job the native way: cron on Linux, cron or `launchd` on macOS, Task Scheduler on Windows. Every 5 minutes:

   ```
   */5 * * * * CLAUDE_NOTIFIER_NTFY_TOPIC=<topic> python3 ~/bin/claude_usage_watcher.py check
   ```

6. Do one real end-to-end test: `correct five_hour` to a timestamp 2 minutes in the future, wait for the next cron tick, confirm a real ntfy push actually lands on my phone.

7. The next time a real `StopFailure(rate_limit)` event fires during normal use, read `~/.claude-usage-watcher/stop_failure_events.jsonl` and tell me exactly what fields the real payload contains. Update `cmd_hit_limit`'s field-matching to use whatever's actually there instead of the current guesses (`reset_at` / `resets_at` / `reset_time` / `retry_after*`). This is the one part of the design that was built on a documented gap rather than a confirmed schema — closing it is the highest-value next step.

8. Whenever the weekly cap's reset time becomes visible anywhere (a CLI banner, `/status` output, claude.ai), feed it in with `correct weekly <timestamp>` at least once, so real resets can be compared against it over the following weeks to figure out its actual mechanics.

## Definition of done

- Both hooks fire without errors during normal Claude Code use, with no visible disruption to the interactive session.
- `status` accurately reflects an active window after normal use, and updates correctly after a real reset.
- A forced near-future reset via `correct` produces a real notification on my phone within one cron cycle.
- `stop_failure_events.jsonl` has at least one real entry, and `hit-limit`'s field-matching has been updated to match reality rather than guesses.

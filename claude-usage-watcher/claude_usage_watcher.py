#!/usr/bin/env python3
"""
claude_usage_watcher.py
========================
Tracks Claude's rolling usage window(s) locally and pushes a phone
notification (via a Telegram bot) the moment one resets.

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

DELIVERY: A CLOUD ONE-SHOT TIMER, NOT LOCAL POLLING
----------------------------------------------------
The moment a reset_at becomes known (record opening a new window, correct,
or hit-limit recognizing a real field), this tool schedules a single
delayed message with Upstash QStash that fires *directly* at the Telegram
Bot API at that exact timestamp (Upstash-Not-Before). That means delivery
does not depend on this machine being awake at reset time -- only on it
being awake at the moment reset_at was *computed*, which is inherently
true anyway since that's when a Claude Code hook just fired. If reset_at
changes (a correction, or hit-limit narrowing an inferred time), the old
QStash message is cancelled and a new one scheduled -- state tracks the
active `qstash_message_id` per window.

`check` (still run periodically by launchd) confirms actual delivery via
QStash's logs API before marking `notified=True` -- scheduling succeeding
is not treated as proof of delivery. It falls back to sending directly
(the old ntfy/Telegram-polling behavior) if QStash reports a permanent
failure, if delivery isn't confirmed within 30 minutes of reset_at, or if
no QStash message was ever successfully scheduled for that window (e.g.
this machine was offline at record time) -- see cmd_check.

SETUP
-----
See ../README.md (repo root) for full setup instructions (hooks, launchd
job, Telegram bot token + chat id, QStash token).

USAGE
-----
  claude_usage_watcher.py record                    call this from the UserPromptSubmit hook
  claude_usage_watcher.py hit-limit                  call this from the StopFailure(rate_limit) hook
  claude_usage_watcher.py check [--dry-run]          call this from the scheduler (fallback + status bookkeeping)
  claude_usage_watcher.py correct five_hour <ISO8601 timestamp>
  claude_usage_watcher.py correct weekly <ISO8601 timestamp>
  claude_usage_watcher.py status                     human-readable dump
"""

import argparse
import contextlib
import fcntl
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(
    os.environ.get("CLAUDE_NOTIFIER_STATE", "~/.claude-usage-watcher/state.json")
).expanduser()

LOCK_PATH = STATE_PATH.with_suffix(".lock")

SECRETS_PATH = Path(
    os.environ.get("CLAUDE_NOTIFIER_SECRETS", "~/.claude-usage-watcher/secrets.env")
).expanduser()

FIVE_HOUR_WINDOW = timedelta(hours=5)
TELEGRAM_BOT_TOKEN_ENV = "CLAUDE_NOTIFIER_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "CLAUDE_NOTIFIER_TELEGRAM_CHAT_ID"
QSTASH_TOKEN_ENV = "CLAUDE_NOTIFIER_QSTASH_TOKEN"
QSTASH_URL_ENV = "CLAUDE_NOTIFIER_QSTASH_URL"

RESET_LABELS = {
    "five_hour": "5-hour Claude usage window",
    "weekly": "Weekly Claude usage cap",
}

# StopFailure's exact JSON payload shape is undocumented (see cmd_hit_limit).
# These are educated guesses, normalized to lower_snake_case before matching
# so "resetAt", "reset-at", and "reset_at" are all recognized the same way.
RESET_TIMESTAMP_KEYS = {"reset_at", "resets_at", "reset_time", "resetat", "resetsat", "resettime"}
RETRY_AFTER_MS_KEYS = {"retry_after_ms", "retryafterms"}
RETRY_AFTER_SECONDS_KEYS = {"retry_after_seconds", "retry_after", "retryafterseconds", "retryafter"}


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def iter_keys_values(node):
    """Depth-first walk of a parsed JSON structure, yielding every
    (key, value) pair at any nesting depth. Since the real field name(s)
    are unconfirmed, searching the whole payload -- not just its top
    level -- costs nothing on a payload this small and means a nested
    shape (e.g. {"error": {"reset_at": ...}}) isn't silently missed just
    because the guess about nesting was wrong too."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from iter_keys_values(v)
    elif isinstance(node, list):
        for item in node:
            yield from iter_keys_values(item)


def load_secrets_into_env() -> None:
    """Read KEY=VALUE lines from SECRETS_PATH and set them into os.environ,
    without overriding anything already explicitly set. This is the primary
    way secrets reach the process -- Claude Code hooks and launchd don't
    reliably share the same environment, so a fixed local file is more
    robust than depending on either one's env propagation."""
    if not SECRETS_PATH.exists():
        return
    for line in SECRETS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_flexible_timestamp(value) -> datetime:
    """Like parse_iso, but also accepts a Unix epoch number (or numeric
    string) -- at least as likely a real shape for StopFailure's
    unconfirmed payload as an ISO string. >10**12 is treated as
    milliseconds, otherwise seconds."""
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            value = float(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10**12 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    raise TypeError(f"unrecognized timestamp shape: {value!r}")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and rename over the real one -- rename is atomic
    # on POSIX, so a crash or a concurrent reader never sees a half-written
    # state.json, only the old version or the new one, never a torn mix.
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, default=str))
    tmp_path.replace(STATE_PATH)


@contextlib.contextmanager
def state_lock():
    """Serialize every command's read-modify-write of state.json across
    concurrent invocations -- e.g. two Claude Code sessions on this
    machine both submitting a prompt at nearly the same instant, each
    firing `record`. Without this, both processes could read the same
    pre-update state, both schedule their own QStash alarm, and the
    second process's save_state() would silently clobber the first's
    qstash_message_id -- leaving an orphaned real cloud alarm with no
    local record of it (a duplicate notification later, not data loss,
    but still worth closing). fcntl.flock blocks until the lock is free,
    so callers never interleave; combined with save_state's atomic
    rename, a fully torn or lost update becomes structurally impossible."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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
        # A brand new window that replaces one that just legitimately
        # expired is trustworthy: this hook fires on every prompt, so
        # "now" genuinely is the first message after expiry, matching
        # Anthropic's own rule for when a window starts. But if there was
        # no tracked five_hour window at all before this call, "now" is
        # just whenever this tool happened to first see a prompt -- which
        # is only the true window start if hooks were registered exactly
        # at a window boundary. This distinction is the exact bug hit
        # during original setup (README/SYSTEMDESIGN): the first inferred
        # reset_at was off from the real countdown by over an hour.
        is_cold_start = "five_hour" not in state
        new_reset_at = now + FIVE_HOUR_WINDOW
        state["five_hour"] = {
            "window_start": now.isoformat(),
            "reset_at": new_reset_at.isoformat(),
            "notified": False,
            "source": "inferred_cold_start" if is_cold_start else "inferred",
            "qstash_message_id": schedule_alarm("five_hour", new_reset_at),
        }
        save_state(state)
        if is_cold_start:
            print(
                "note: first-ever window recorded, anchored to right now -- "
                "this is a guess, not a confirmed window start. Compare "
                "against Claude Code's own countdown or the claude.ai usage "
                "page, and run `correct five_hour <timestamp>` if they "
                "differ (see `status`).",
                file=sys.stderr,
            )
    # else: window already open, nothing to do -- alarm already scheduled.


def cmd_correct(args) -> None:
    """Overwrite a tracked window with an observed ground-truth timestamp.
    Use this whenever you have a real value (a CLI banner, /status
    output, hit-limit's captured payload, etc). Manual corrections take
    priority and persist until that reset time itself passes."""
    kind = args.kind
    target_time = parse_iso(args.timestamp)

    state = load_state()
    old_message_id = (state.get(kind) or {}).get("qstash_message_id")
    if old_message_id:
        cancel_alarm(old_message_id)

    state[kind] = {
        "reset_at": target_time.isoformat(),
        "notified": False,
        "source": "observed",
        "qstash_message_id": schedule_alarm(kind, target_time),
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

    new_reset_at = None
    new_source = None
    for key, value in iter_keys_values(payload):
        if _normalize_key(key) in RESET_TIMESTAMP_KEYS:
            try:
                new_reset_at = parse_flexible_timestamp(value)
                new_source = f"stop_failure:{key}"
                break
            except (ValueError, TypeError):
                continue

    if new_reset_at is None:
        for key, value in iter_keys_values(payload):
            normalized = _normalize_key(key)
            if normalized not in RETRY_AFTER_MS_KEYS and normalized not in RETRY_AFTER_SECONDS_KEYS:
                continue
            try:
                seconds = float(value) / (1000 if normalized in RETRY_AFTER_MS_KEYS else 1)
                new_reset_at = now + timedelta(seconds=seconds)
                new_source = f"stop_failure:{key}"
                break
            except (ValueError, TypeError):
                continue

    if new_reset_at is None and payload:
        seen_keys = sorted({k for k, _ in iter_keys_values(payload)})
        print(
            "note: StopFailure payload had no recognized reset/retry field "
            f"anywhere in it (searched all nesting levels). Keys seen: {seen_keys}. "
            "Inspect stop_failure_events.jsonl for the full raw payload and "
            "update RESET_TIMESTAMP_KEYS/RETRY_AFTER_*_KEYS in "
            "claude_usage_watcher.py once you spot the real field name.",
            file=sys.stderr,
        )

    if new_reset_at is not None:
        old_message_id = five.get("qstash_message_id")
        if old_message_id:
            cancel_alarm(old_message_id)
        five["reset_at"] = new_reset_at.isoformat()
        five["notified"] = False
        five["source"] = new_source
        five["qstash_message_id"] = schedule_alarm("five_hour", new_reset_at)

    save_state(state)


DELIVERY_CONFIRM_GRACE = timedelta(minutes=30)
QSTASH_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELLED"}


def cmd_check(args) -> None:
    """Call from the scheduler every few minutes. In the common case the
    push was already fired directly by QStash at the exact reset moment,
    independent of this machine being awake -- so this just confirms
    delivery via QStash's logs API and mirrors notified=True locally, for
    accurate `status` output. It sends directly (the old always-local
    behavior) in two cases: a window that never got a qstash_message_id
    scheduled at all (e.g. this machine was offline when record/correct/
    hit-limit ran), or one where QStash confirms the send permanently
    failed (bad chat id, revoked bot token, etc) or where delivery still
    isn't confirmed DELIVERY_CONFIRM_GRACE after reset_at -- so a scheduled
    alarm that silently never arrives doesn't mean silence forever."""
    state = load_state()
    now = now_utc()
    changed = False

    for kind, label in RESET_LABELS.items():
        entry = state.get(kind)
        if not entry or not entry.get("reset_at"):
            continue
        if entry.get("notified"):
            continue

        reset_at = parse_iso(entry["reset_at"])
        if now >= reset_at:
            covered_by_qstash = bool(entry.get("qstash_message_id"))
            message = f"{label} just reset. Go."

            if args.dry_run:
                # Preview only -- never mutate state, so a real check
                # afterwards still fires for real.
                if covered_by_qstash:
                    delivery_state = qstash_delivery_state(entry["qstash_message_id"])
                    print(f"[dry-run] QStash delivery state: {delivery_state or 'unknown'} -- {message}")
                else:
                    print(f"[dry-run] would notify directly (no QStash alarm was scheduled): {message}")
                continue

            if covered_by_qstash:
                delivery_state = qstash_delivery_state(entry["qstash_message_id"])
                past_grace = (now - reset_at) > DELIVERY_CONFIRM_GRACE
                if delivery_state == "DELIVERED":
                    entry["notified"] = True
                    changed = True
                elif delivery_state in QSTASH_TERMINAL_FAILURE_STATES or past_grace:
                    # QStash confirmed it'll never deliver, or we've waited
                    # long enough without confirmation to stop trusting it
                    # silently -- send directly so this never goes quiet.
                    if send_telegram(f"*Claude usage reset*\n{message}"):
                        entry["notified"] = True
                        changed = True
                    # else: leave notified=False, retry next tick.
                # else: still in flight (CREATED/ACTIVE/RETRY/ERROR/
                # IN_PROGRESS/unknown) and within grace -- check again
                # next tick rather than assuming success or failure.
            elif send_telegram(f"*Claude usage reset*\n{message}"):
                entry["notified"] = True
                changed = True
            # else: leave notified=False so the next tick retries.

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
        if not entry:
            print("  no data yet")
            continue
        if not entry.get("reset_at"):
            print("  no reset time tracked yet")
            if entry.get("confirmed_blocked_at"):
                print(f"  confirmed blocked at: {fmt_local(parse_iso(entry['confirmed_blocked_at']))} (via StopFailure)")
            continue
        reset_at = parse_iso(entry["reset_at"])
        remaining = reset_at - now
        status = "PASSED (waiting for check to notify)" if remaining.total_seconds() <= 0 else "active"
        print(f"  source:    {entry.get('source', 'unknown')}")
        if entry.get("source") == "inferred_cold_start":
            print("  ⚠  unverified first-run guess -- anchored to whenever this tool first")
            print("     saw a prompt, not a confirmed window start. Compare against Claude")
            print("     Code's own countdown or claude.ai/settings/usage, then run:")
            print(f"       correct {kind} <real timestamp>")
        print(f"  resets at: {fmt_local(reset_at)}  ({status})")
        if remaining.total_seconds() > 0:
            mins = int(remaining.total_seconds() // 60)
            print(f"  time left: {mins // 60}h {mins % 60}m")
        print(f"  notified:  {entry.get('notified', False)}")
        if entry.get("confirmed_blocked_at"):
            print(f"  confirmed blocked at: {fmt_local(parse_iso(entry['confirmed_blocked_at']))} (via StopFailure)")


# --------------------------------------------------------------------------
# QStash -- schedules a one-shot delayed delivery straight to Telegram,
# so the actual push doesn't depend on this machine being awake at
# reset_at, only at the moment reset_at was computed.
# --------------------------------------------------------------------------

def schedule_alarm(kind: str, reset_at: datetime):
    """Schedule a QStash message that hits the Telegram Bot API directly
    at reset_at (Upstash-Not-Before, an absolute unix timestamp -- not a
    relative delay, so it's immune to any gap between computing reset_at
    and this call actually firing). Returns the QStash messageId on
    success, or None (never raises) -- callers store None just like a
    real id; cmd_check's fallback path treats a missing id as "never
    successfully scheduled" and sends directly instead."""
    token = os.environ.get(QSTASH_TOKEN_ENV)
    base_url = os.environ.get(QSTASH_URL_ENV)
    bot_token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    if not all((token, base_url, bot_token, chat_id)):
        print(
            f"warning: missing one of {QSTASH_TOKEN_ENV}/{QSTASH_URL_ENV}/"
            f"{TELEGRAM_BOT_TOKEN_ENV}/{TELEGRAM_CHAT_ID_ENV} -- "
            "no cloud alarm scheduled, `check` will fall back to direct send",
            file=sys.stderr,
        )
        return None

    message = f"*Claude usage reset*\n{RESET_LABELS.get(kind, kind)} just reset. Go."
    destination = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    publish_url = f"{base_url.rstrip('/')}/v2/publish/{destination}"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }).encode("utf-8")

    req = urllib.request.Request(
        publish_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Upstash-Forward-Content-Type": "application/x-www-form-urlencoded",
            "Upstash-Not-Before": str(int(reset_at.timestamp())),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return result.get("messageId")
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"warning: failed to schedule QStash alarm ({e})", file=sys.stderr)
        return None


def qstash_delivery_state(message_id: str):
    """Query QStash's logs API for the latest delivery state of a
    scheduled message: DELIVERED, FAILED, RETRY, ERROR, ACTIVE, etc (see
    Upstash's /v2/logs docs for the full enum). Returns None if the query
    itself failed, secrets are missing, or no log entry exists yet --
    callers must treat None as "unknown, try again later," never as a
    stand-in for any particular delivery outcome. This is what lets
    cmd_check tell "QStash actually delivered it" apart from "QStash
    accepted the schedule call," which scheduling success alone can't."""
    token = os.environ.get(QSTASH_TOKEN_ENV)
    base_url = os.environ.get(QSTASH_URL_ENV)
    if not token or not base_url:
        return None
    url = f"{base_url.rstrip('/')}/v2/logs?messageId={urllib.parse.quote(message_id)}&count=10"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    # Despite what Upstash's docs page for this endpoint says, the live
    # API returns the array under "events", not "logs" -- verified against
    # a real account's response during development.
    events = result.get("events") or []
    if not events:
        return None
    return max(events, key=lambda entry: entry.get("time", 0)).get("state")


def cancel_alarm(message_id: str) -> None:
    """Best-effort delete of a previously scheduled QStash message (e.g.
    superseded by a `correct` or a hit-limit update). Safe to call on an
    id that already fired -- QStash 404s, which we swallow."""
    token = os.environ.get(QSTASH_TOKEN_ENV)
    base_url = os.environ.get(QSTASH_URL_ENV)
    if not token or not base_url:
        return
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v2/messages/{message_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.URLError:
        pass


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    if not token or not chat_id:
        print(
            f"error: set {TELEGRAM_BOT_TOKEN_ENV} and {TELEGRAM_CHAT_ID_ENV}",
            file=sys.stderr,
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.URLError as e:
        print(f"error: failed to reach Telegram ({e})", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track and notify on Claude usage window resets.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("record", help="Mark activity now (call from a Claude Code hook)")

    sub.add_parser("hit-limit", help="Call from a StopFailure(rate_limit) hook")

    p_check = sub.add_parser("check", help="Check for a reset and notify (call from the scheduler)")
    p_check.add_argument("--dry-run", action="store_true", help="Print instead of sending a push")

    p_correct = sub.add_parser("correct", help="Feed in an observed reset timestamp")
    p_correct.add_argument("kind", choices=["five_hour", "weekly"])
    p_correct.add_argument("timestamp", help="ISO 8601, e.g. 2026-07-01T14:00:00-04:00")

    sub.add_parser("status", help="Print current tracked state")

    return parser


def main() -> None:
    load_secrets_into_env()
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "record": cmd_record,
        "hit-limit": cmd_hit_limit,
        "check": cmd_check,
        "correct": cmd_correct,
        "status": cmd_status,
    }
    with state_lock():
        dispatch[args.command](args)


if __name__ == "__main__":
    main()

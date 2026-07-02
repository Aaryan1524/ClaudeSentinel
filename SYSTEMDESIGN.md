# System Design: claude-usage-watcher

This document explains, in plain language, how this whole system actually
works under the hood — every moving piece, why it exists, and the real
problems that shaped the design. If you're picking this project back up
after a while, read this first.

## The problem, restated simply

Anthropic doesn't offer a "your usage limit just reset" webhook. The only
ground truth is two things built for humans to read: the claude.ai usage
page, and a banner Claude Code shows you when you actually hit a limit.
Nothing you can subscribe to.

But there's one fact Anthropic *does* publish: the 5-hour usage window is a
fixed timer. It starts on your first message after the previous window
expired, and resets exactly 5 hours later — no matter how much or little
you use inside it. That one fact is enough to build the whole system: if
you can detect "a new window just started," you can compute exactly when
it will end, without ever polling Anthropic for anything.

## The two problems this system actually solves

There are two genuinely separate problems bundled into this tool, and
keeping them separate is the key to understanding the design:

1. **"When does my window reset?"** — a *computation* problem, solved
   locally, the instant a Claude Code hook tells us a new window started.
2. **"Tell me the moment it happens, even if I'm not at my laptop"** — a
   *delivery* problem, solved by handing a scheduled alarm to a cloud
   service the instant problem #1 is solved, so delivery no longer depends
   on this machine at all.

Early versions of this tool conflated these two problems (see
[Architecture history](#architecture-history-and-why-each-pivot-happened)
in the README) — treating "detect the reset" and "notify me" as one
continuous local process. That's what caused the biggest limitation this
system had until recently: notifications only fired while the laptop was
awake.

## High-level architecture

```
┌─────────────────┐      UserPromptSubmit hook       ┌──────────────────────┐
│  Claude Code     │ ───────────────────────────────▶ │ claude_usage_        │
│  (any project,   │      StopFailure(rate_limit)     │ watcher.py           │
│   this machine)  │ ───────────────────────────────▶ │  (~/bin, deployed)   │
└─────────────────┘                                   └──────────┬───────────┘
                                                                  │
                                    reads/writes                 │ schedules alarm
                              ┌───────────────────┐              │ (once per window)
                              │ ~/.claude-usage-  │              ▼
                              │ watcher/           │      ┌──────────────────┐
                              │  state.json         │      │  Upstash QStash  │
                              │  secrets.env        │      │  (cloud timer)   │
                              │  stop_failure_       │      └────────┬─────────┘
                              │   events.jsonl       │               │
                              └───────────────────┘               │ fires at
                                       ▲                            │ exact reset_at
                                       │ every 5 min                ▼
                              ┌───────────────────┐      ┌──────────────────┐
                              │  launchd            │      │  Telegram Bot    │
                              │  (fallback +         │      │  API             │
                              │   status sync only)  │◀ ─ ─ ┤  (direct call,   │
                              └───────────────────┘  no    │   bypasses this  │
                                                       double│  script entirely)│
                                                       -fire └────────┬─────────┘
                                                                       │
                                                                       ▼
                                                              ┌──────────────────┐
                                                              │  Your phone       │
                                                              │  (Telegram app)   │
                                                              └──────────────────┘
```

The critical thing this diagram is trying to show: **once QStash has the
alarm, the path from "5 hours pass" to "phone buzzes" never touches this
laptop, this script, or `launchd` again.** QStash calls Telegram directly.

## Every component, and exactly what it's for

### `claude_usage_watcher.py` — the core script

One Python file, zero third-party dependencies (only the standard
library — `urllib` for HTTP, no `requests`, no SDKs). It's a single CLI
with five subcommands, each mapped to a specific trigger:

| Command | Triggered by | Job |
|---|---|---|
| `record` | Claude Code's `UserPromptSubmit` hook, on every prompt | If no 5-hour window is currently tracked (or the last one expired), start a new one and schedule its QStash alarm. Otherwise, no-op — this is why it's safe to run on *every* prompt, not just the first one of a session. |
| `hit-limit` | Claude Code's `StopFailure` hook, matcher `rate_limit` | Log the raw payload (for later inspection), and if it recognizes a real reset-time field in that payload, cancel the old alarm and schedule a corrected one. |
| `check` | `launchd`, every 5 minutes | Fallback + bookkeeping only — see [`check`'s actual job](#checks-actual-job-fallback-not-primary-delivery) below. |
| `correct` | You, manually, whenever you see a real reset time somewhere | Overwrite the tracked reset time with ground truth, cancel any existing alarm, schedule a new one. |
| `status` | You, manually | Print what's currently tracked, in plain English. |

Why zero dependencies: this script has to run reliably from a `launchd`
job and from Claude Code hooks, both of which are non-interactive contexts
where a missing `pip install` would fail silently and invisibly. Standard
library only means nothing to install, ever, on this or any future
machine.

### State: `~/.claude-usage-watcher/state.json`

A small JSON file, structured like this:

```json
{
  "five_hour": {
    "reset_at": "2026-07-02T05:44:09.306396+00:00",
    "notified": false,
    "source": "observed",
    "qstash_message_id": "msg_..."
  },
  "weekly": { }
}
```

- `reset_at` — the ISO 8601 timestamp this tool believes the window ends.
- `source` — how we know that: `inferred` (computed from a hook firing),
  `observed` (you told it via `correct`), or `stop_failure:<field>` (learned
  from a real rate-limit event).
- `notified` — whether this window's reset has been delivered. In the
  QStash-covered case, `check` confirms this against QStash's own logs API
  (`DELIVERED` state) before setting it — it's not just "the reset time
  passed." If delivery is never confirmed within 30 minutes of `reset_at`,
  or QStash reports a terminal failure, `check` sends directly instead and
  only then marks this `true`.
- `qstash_message_id` — the ID of the currently-scheduled cloud alarm for
  this window, if scheduling succeeded. This is the field that determines
  whether `check` treats a passed reset as already-handled or falls back
  to sending directly.

It lives outside the repo (under your home directory, not under version
control) because it's runtime state, not source code — same reasoning
that keeps build artifacts out of git.

### Secrets: `~/.claude-usage-watcher/secrets.env`

Four key-value pairs (Telegram bot token, Telegram chat ID, QStash token,
QStash region URL), `chmod 600` so only your user account can read it. The
script parses this file itself, at the top of `main()`, and only fills in
environment variables that aren't already set (`os.environ.setdefault`) —
so an explicit env var still wins if you ever want to override without
touching the file.

**Why a file the script reads itself, instead of environment variables set
by whoever launches it:** this was a deliberate fix partway through
building this. `launchd` lets you bake env vars into a plist's
`EnvironmentVariables` block — that part's easy. But Claude Code hooks are
a different execution context entirely, launched by the Claude Code
process itself, and there's no guaranteed shared environment between "the
shell you're typing in," "the process Claude Code hooks run under," and
"the launchd job." Rather than debug three different environment
propagation paths, the script owns its own secret-loading, so it behaves
identically no matter what invoked it.

### Claude Code hooks

Two entries in `~/.claude/settings.json`:

- **`UserPromptSubmit`** → `record`. Fires on literally every prompt you
  send, in any project, on this machine. It's intentionally cheap to call
  this often — the no-op path (window already open) is just one file read
  and an `if` check.
- **`StopFailure`**, matcher `rate_limit` → `hit-limit`. Fires only when a
  turn ends specifically because you got rate-limited. This is a newer
  Claude Code hook event (added CLI v2.1.78) with an undocumented payload
  shape — see [Known gaps](#known-gaps-that-are-genuinely-open) below.

Both commands are pinned to `/usr/bin/python3` (Apple's system Python),
never a bare `python3` — see the SSL gotcha below for why that specific
choice matters.

### `launchd` — the fallback, not the delivery mechanism

A macOS `LaunchAgent` (`~/Library/LaunchAgents/com.aaryan.claude-usage-
watcher.plist`) that runs `check` every 300 seconds. This used to be the
*entire* delivery mechanism (see history below); today its job is narrower
and deliberately so:

#### `check`'s actual job: fallback, not primary delivery

For each tracked window, if its `reset_at` has passed and it hasn't been
marked notified yet:
- **If it has a `qstash_message_id`** (the common case — scheduling
  succeeded when the window was recorded): query QStash's logs API
  (`qstash_delivery_state`) for that message's actual delivery state.
  `DELIVERED` → mark `notified = true`, nothing else to do. A terminal
  failure (`FAILED`/`CANCELLED` — e.g. revoked bot token, bad chat id) *or*
  no confirmation within 30 minutes of `reset_at` → send the Telegram
  message directly, right now, from this machine, so a scheduled alarm
  that silently never arrives doesn't mean silence forever. Anything else
  (still `ACTIVE`/`RETRY`/unknown, within the 30-minute window) → leave it
  alone and check again next tick.
- **If it has no `qstash_message_id`** (scheduling failed — e.g. this
  machine was offline or misconfigured at the moment `record` ran): send
  the Telegram message directly immediately, same as the failure case above.

In rare cases where QStash's delivery-log telemetry lags behind the actual
send past the 30-minute grace window, this can produce one duplicate
notification. That's an accepted tradeoff — never silent beats never
duplicate.

`launchd` chosen over `cron`: this is macOS, and macOS's own scheduler
underneath `cron` is `launchd` anyway — going straight to a `LaunchAgent`
avoids `cron`'s known unreliability on modern macOS (permission prompts,
inconsistent firing through sleep) without adding anything.

### Upstash QStash — the actual delivery mechanism

A message queue / delayed-delivery service. The relevant feature here is
a one-shot HTTP publish with an `Upstash-Not-Before` header: you give it a
destination URL, a body, and an absolute Unix timestamp, and it delivers
that exact HTTP request to that exact URL at that exact time — durably,
independent of anything on your end after the publish call succeeds.

The destination URL used here is Telegram's own Bot API endpoint
(`api.telegram.org/bot<token>/sendMessage`) — QStash is told to POST
directly to Telegram, with the message body and an
`Upstash-Forward-Content-Type` header telling it what content-type to
forward. **No intermediate server was built for this** — QStash talks to
Telegram directly, this script's only job is the one-time publish call.

`schedule_alarm()` and `cancel_alarm()` in the script are the only two
functions that talk to QStash. Scheduling returns a `messageId`, stored in
state; cancelling deletes that message by ID (used before rescheduling, so
a `correct` or a `hit-limit` update never results in two pending alarms
for the same window).

### Telegram — the delivery channel

A bot (created via @BotFather) that messages one specific chat (yours).
Two things call it: `send_telegram()` (the direct-fallback path inside
`check`), and QStash (the primary path, calling Telegram's API directly
without going through this script at all). Both use the same bot token
and chat ID, sourced from the same secrets file.

### The deployed copy: `~/bin/claude_usage_watcher.py`

The repo (`claude-usage-watcher/claude_usage_watcher.py`) is where you
edit the script. It is **not** what actually runs — hooks and `launchd`
both point at `~/bin/claude_usage_watcher.py`, a plain copy kept in sync
by `./install.sh`. See the TCC section below for why this split exists;
it's not stylistic, it's a hard macOS requirement.

## Every real problem hit while building this, and the actual fix

This section exists because each of these looked like a different kind of
bug at first, and each one turned out to be the same root cause wearing a
different costume, or a genuinely separate gotcha worth remembering.

### 1. `launchd` couldn't read the script — `Operation not permitted`

**Symptom:** the LaunchAgent's error log showed the interpreter failing to
open the script file, despite the exact same path working fine when run
by hand in Terminal.

**Root cause:** macOS TCC (Transparency, Consent, and Control) protects
`~/Desktop`, `~/Documents`, and `~/Downloads` specifically — background
processes need explicit, granted consent to read inside them, and
`launchd` jobs don't get it by default even though your interactive
Terminal session does (Terminal itself has been granted broad access at
some point, most `launchd` jobs haven't).

**Fix:** deploy the script to `~/bin` (not TCC-protected) and point every
non-interactive invocation (hooks, `launchd`) at that copy instead of the
repo.

### 2. A bare `python3` silently broke every HTTPS call

**Symptom:** `SSL: CERTIFICATE_VERIFY_FAILED` errors calling Telegram/QStash.

**Root cause:** this Mac has more than one `python3` on `$PATH`. A
`python.org`-installer build of Python ships its own CA certificate
bundle, which isn't populated until you separately run its
`Install Certificates.command` — until then, `urllib`'s HTTPS calls fail
verification. Apple's own `/usr/bin/python3` uses the system trust store
and doesn't have this problem, but which one you get depends entirely on
`$PATH` order at the moment `python3` (unqualified) is resolved.

**Fix:** every invocation of the watcher, everywhere (hooks, plist,
`test.sh`), uses the absolute path `/usr/bin/python3`, never a bare
`python3`. This removes the ambiguity entirely rather than relying on
`$PATH` staying a particular way.

### 3. QStash's generic endpoint didn't route correctly

**Symptom:** `user not found in this region (eu-central-1)` from the
generic `qstash.upstash.io` gateway, for a perfectly valid, freshly
created account.

**Fix:** use the account-specific regional URL Upstash's dashboard gives
you (`QSTASH_URL` in their console) instead of the generic multi-region
gateway. Not a bug in this tool — just a detail worth knowing before
assuming a token is invalid when it's actually the endpoint.

### 4. `python3 -c "..."` failing under a TCC-protected working directory

**Symptom:** an inline one-liner (`python3 -c "from datetime import ..."`)
crashed with `PermissionError` during Python's own interpreter startup,
even though running the *actual script file* from the exact same directory
worked fine.

**Root cause:** Python's import system inserts different things into
`sys.path[0]` depending on how it's invoked. Running a script file inserts
that script's own directory. Running `python3 -c "..."` inserts the
current working directory (as an empty string, resolved at import time).
If that cwd happens to be under `~/Desktop`, Python's own startup import
scan hits the same TCC wall as problem #1 — but only for `-c` one-liners,
not for real script invocations. This makes it narrower than it first
looks: hooks and `launchd` (which always invoke the actual `.py` file, not
`-c`) are unaffected. Only ad hoc one-liners run from a Desktop-rooted
terminal are at risk.

### 5. Mid-session loss of Desktop folder access entirely

**Symptom:** every file that existed in the repo before a certain point in
the session started returning `Operation not permitted` on both read and
write — `cat`, `touch`, this tool's own file-reading — while brand new
files were completely unaffected.

**Root cause:** a TCC grant for the app running the shell (Terminal, or
whatever's hosting the session) got revoked or reset mid-session — this is
an OS-level permission, not something any of this tool's code controls.
The asymmetry (old files blocked, new files fine) is consistent with the
app's TCC bookmark for the Desktop tree being invalidated while it
retained implicit access to files it had just created itself.

**Fix:** none possible from inside the tool — this requires the user to
re-grant Files & Folders / Full Disk Access to the relevant app in System
Settings → Privacy & Security. Documented here because it's a real
operational hazard for anyone editing files under `~/Desktop` on macOS,
not specific to this project.

**The practical lesson across all five:** macOS treats `~/Desktop`,
`~/Documents`, and `~/Downloads` as meaningfully more restricted than the
rest of your home directory, in ways that are easy to not notice until a
background process (not your interactive shell) tries to touch them. If
you're building anything that runs unattended on macOS, keep its runtime
files (scripts it executes, state it reads/writes) outside those three
folders from the start.

## Design decisions, and the reasoning behind each

- **Standard-library-only Python.** No dependency installation step, ever,
  on any future machine this gets deployed to. A `launchd` job or hook
  failing because a `pip` package silently isn't importable is a bad class
  of bug to have possible at all.
- **State and secrets outside the repo.** Runtime data doesn't belong in
  version control; secrets doubly don't. Keeping them at
  `~/.claude-usage-watcher/` means the repo can be freely shared, deleted,
  or re-cloned without touching what's actually running.
- **The repo isn't required for the system to keep working.** Once
  deployed (`~/bin` copy, hooks registered, `launchd` loaded, secrets
  written), nothing references the repo's path. It exists purely as the
  editable source of truth for future changes.
- **Corrections always cancel-then-reschedule, never just overwrite.**
  Every place that changes `reset_at` (`correct`, `hit-limit`) explicitly
  cancels any existing QStash alarm before scheduling the new one. Without
  this, a correction after a window already had an alarm scheduled would
  leave two alarms live — one for the stale time, one for the corrected
  time — and you'd get notified twice.
- **`check` confirms actual delivery via QStash's logs API, not just
  scheduling success.** Upstash's `/v2/logs?messageId=...` (undocumented
  response shape — it returns `events`, not `logs`, despite what Upstash's
  own docs page says) reports each message's real state (`DELIVERED`,
  `FAILED`, etc). `check` treats scheduling success alone as "trust it, but
  verify" — it only marks `notified` after confirming `DELIVERED`, and
  falls back to a direct send if QStash reports a terminal failure or 30
  minutes pass with no confirmation. This closes what used to be a silent
  failure mode: a revoked bot token or bad chat id would previously mark a
  window "notified" with nothing ever actually delivered.
- **All state.json reads/writes are serialized with an `flock`-based lock
  (`state.lock`), and writes are atomic (temp file + rename).** Claude Code
  hooks can fire from more than one session on the same machine at nearly
  the same instant; without this, two concurrent `record`/`hit-limit`/
  `check` calls could race on the same read-modify-write cycle and the
  loser's update (e.g. a `qstash_message_id`) would be silently clobbered.

## Known gaps that are genuinely open

- **`StopFailure` payload schema is unconfirmed** — `hit-limit` searches the
  entire payload recursively (any nesting depth, case/hyphen-insensitive
  key matching) and accepts either ISO 8601 strings or Unix epoch numbers
  for the timestamp fields, but the actual field *names* Anthropic uses are
  still a guess until a real payload is captured and inspected. When
  nothing matches, it prints every key it saw, to make that inspection
  fast the next time a real rate limit hits.
- **Weekly cap reset mechanics are unpublished** — never inferred, only
  ever set by explicit `correct weekly`.
- **The 5-hour window's `inferred` source can drift from Anthropic's true
  window start.** This tool only learns a window started when a hook
  fires — if that's not the actual first message of the window (e.g. hooks
  were registered mid-window, or usage happened via claude.ai's web/mobile
  app, which doesn't fire local hooks), the computed `reset_at` will be
  wrong until corrected. A genuine window rollover (the previous one
  legitimately expired) is trustworthy and gets `source: inferred`; a
  window created because no prior one was tracked at all gets
  `source: inferred_cold_start` instead, with an explicit warning in
  `record`'s output and `status`. This is exactly what happened during
  original setup: the tool's `inferred` time was
  off from the real app's countdown by over an hour. `correct five_hour
  <timestamp>` is the fix whenever you notice a mismatch — always trust
  what Claude Code/claude.ai itself shows you over
  this tool's inference.

See `FUTUREWORK.md` for possible improvements to any of these.

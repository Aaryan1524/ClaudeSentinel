# claude-usage-watcher

A fully local tool that watches Claude Code activity, tracks when your 5-hour
usage window (and, once seeded, the weekly cap) will reset, and pushes a
phone notification the moment it does — via [ntfy.sh](https://ntfy.sh). No
browser, no browser extension, no manually checking claude.ai. Get pinged,
go back to work.

## How it works

**The 5-hour window** is a fixed timer anchored to the first message of a
new window: it resets exactly 5 hours later, no matter how much you use
inside that window. That makes the reset time fully computable *if* this
tool sees your first message of each window — which it does, via a Claude
Code hook. It has a blind spot for usage that happens purely through the
claude.ai web/mobile app, since those don't fire local hooks; use `correct`
to hand it a real observed timestamp whenever you have one, and that
correction wins until it itself expires.

**The weekly cap** is tracked separately for Opus vs. all other models, and
its exact reset mechanics aren't publicly documented (rolling-from-first-use
like the 5-hour window? fixed weekly boundary? unknown). So this tool never
guesses at it — it only moves when you explicitly `correct weekly <timestamp>`
with a real value you've seen somewhere.

**There is no push/webhook API** for "your usage window just reset." This is
local inference plus polling, not an event subscription — a scheduled job
calls `check` every few minutes and only sends a push once the locally
tracked `reset_at` has actually passed.

**`hit-limit`** is wired to Claude Code's `StopFailure` hook (matcher
`rate_limit`, added in CLI v2.1.78) — it fires the instant a turn ends
because you actually got rate-limited. That's a real, fully local signal
with no scraping involved, but Anthropic hasn't published the exact JSON
payload shape yet ([anthropics/claude-code#35620](https://github.com/anthropics/claude-code/issues/35620)).
So `hit-limit` always logs the raw payload to `stop_failure_events.jsonl`
and only updates `reset_at` if it recognizes a timestamp or retry-delay key
in it — everything in that key list is an educated guess until a real
payload proves otherwise (see [Known gaps](#known-gaps)).

## Directory layout

```
claude-usage-watcher/
├── claude_usage_watcher.py   the tool itself (record / hit-limit / check / correct / status)
├── install.sh                deploys the script to ~/bin (see "Why two copies" below)
├── test.sh                   local, side-effect-free test pass (no real ntfy pushes)
├── hooks.snippet.json        the two hook entries to merge into ~/.claude/settings.json
├── launchd/
│   └── com.aaryan.claude-usage-watcher.plist   LaunchAgent that runs `check` every 5 min
└── README.md                 this file
```

### Why two copies of the script exist

This repo is the source of truth and where you edit the script. But the
hooks and the launchd job don't execute it from here — they execute a
deployed copy at `~/bin/claude_usage_watcher.py`. Reason: `~/Desktop` (and
`~/Documents`, `~/Downloads`) are TCC-protected on macOS. A background
`launchd` process trying to read a script under `~/Desktop/...` fails with
a silent `Operation not permitted`, even though your interactive Terminal
reads the same path fine — this bit us once already setting this up.
`~/bin` isn't a protected location, so it sidesteps the issue entirely.

**After editing `claude_usage_watcher.py` in this repo, run `./install.sh`**
to redeploy it to `~/bin` — the two copies are not auto-synced.

State lives outside this repo, at `~/.claude-usage-watcher/`:

| File | Contents |
|---|---|
| `state.json` | current `five_hour` / `weekly` tracked windows |
| `stop_failure_events.jsonl` | every raw `StopFailure` payload ever seen, one JSON object per line |
| `launchd.out.log` / `launchd.err.log` | stdout/stderr from the scheduled `check` runs |

## Setup

### 1. Deploy the script to `~/bin`

```bash
./install.sh
```

See [Why two copies of the script exist](#why-two-copies-of-the-script-exist)
above. Re-run this any time you edit `claude_usage_watcher.py`.

### 2. Run the test pass

```bash
./test.sh
```

Confirms fresh-window inference, `check --dry-run` previewing without
mutating state, and `hit-limit` logging + `confirmed_blocked_at` — all
against a scratch state file, no real state or network calls touched.

### 3. Register the Claude Code hooks

Merge the two hook entries from `hooks.snippet.json` into
`~/.claude/settings.json` under its `hooks` key. **Merge, don't overwrite** —
your `settings.json` likely has other config (MCP servers, `effortLevel`,
etc.) that must survive untouched. Both commands point at the `~/bin`
deployed copy, not this repo.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /Users/aaryan/bin/claude_usage_watcher.py record" } ] }
    ],
    "StopFailure": [
      { "matcher": "rate_limit",
        "hooks": [ { "type": "command",
          "command": "python3 /Users/aaryan/bin/claude_usage_watcher.py hit-limit" } ] }
    ]
  }
}
```

### 4. Pick an ntfy.sh topic

Anyone who knows the exact topic string can read it, so make it an
unguessable random string, not something like `aaryan-claude` — generate
one with, e.g., `python3 -c "import secrets; print(secrets.token_urlsafe(12))"`.
Subscribe to it in the ntfy app on your phone. **Don't commit the real
topic to this repo** — the checked-in plist template keeps a `REPLACE_ME`
placeholder on purpose; only the deployed copy in `~/Library/LaunchAgents/`
(untracked by git) should hold the real value.

### 5. Load the launchd job

```bash
cp launchd/com.aaryan.claude-usage-watcher.plist ~/Library/LaunchAgents/
sed -i '' 's/REPLACE_ME/<your real topic>/' ~/Library/LaunchAgents/com.aaryan.claude-usage-watcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aaryan.claude-usage-watcher.plist
launchctl list | grep claude-usage-watcher   # should show it loaded, exit status 0
```

launchd, like cron, does **not** inherit your interactive shell's exported
env vars — that's why the topic lives in the plist's `EnvironmentVariables`
block instead of relying on an export elsewhere. (`launchctl load` still
works but is the deprecated form; `bootstrap` is the modern equivalent.)

To reload after editing the plist:

```bash
launchctl bootout gui/$(id -u)/com.aaryan.claude-usage-watcher 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aaryan.claude-usage-watcher.plist
```

### 6. Real end-to-end test

```bash
python3 claude_usage_watcher.py correct five_hour "$(python3 -c "from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())")"
```

Wait for the next launchd tick (≤5 min) and confirm a real push lands on
your phone. This is the only step that sends a real network request and
mutates real state — do it deliberately.

## Usage

| Command | When it runs | What it does |
|---|---|---|
| `record` | `UserPromptSubmit` hook | Starts a new 5-hour window if none is active. No-op otherwise — usage volume never moves the reset time. |
| `hit-limit` | `StopFailure(rate_limit)` hook | Logs the raw payload, marks `confirmed_blocked_at`, updates `reset_at` if it recognizes a field. |
| `check [--dry-run]` | launchd, every 5 min | Sends one ntfy push per window the instant its `reset_at` passes. `--dry-run` prints instead of sending and never mutates state. |
| `correct <five_hour\|weekly> <ISO8601>` | manual, whenever you see a real value | Overwrites the tracked reset time with ground truth. Always wins over inference until that time passes. |
| `status` | manual | Human-readable dump of both tracked windows. |

## Known gaps

- **`StopFailure` payload schema is unconfirmed.** `hit-limit` currently
  guesses at `reset_at` / `resets_at` / `reset_time` / `retry_after_ms` /
  `retry_after_seconds` / `retry_after`. The next time a real rate limit
  hits during normal use, read `stop_failure_events.jsonl`, find the real
  field names, and update the key lists in `cmd_hit_limit` in
  `claude_usage_watcher.py`.
- **Weekly cap mechanics are unknown.** Only ever set via `correct weekly`;
  never inferred. Feed it real observed values over time to eventually
  figure out whether it's rolling or fixed-boundary.
- **Blind to claude.ai web/mobile usage.** If you split usage across
  surfaces, the 5-hour inference can drift from reality until corrected.

## Troubleshooting

```bash
# is the job loaded?
launchctl list | grep claude-usage-watcher

# force a run right now, bypassing the 5-min interval
launchctl start com.aaryan.claude-usage-watcher

# check what actually happened on the last few ticks
tail -f ~/.claude-usage-watcher/launchd.out.log ~/.claude-usage-watcher/launchd.err.log

# current tracked state
python3 claude_usage_watcher.py status
```

**`launchd.err.log` shows `Operation not permitted` opening the script:**
you're pointing the plist at a script under `~/Desktop`, `~/Documents`, or
`~/Downloads` — all TCC-protected on macOS, silently blocking background
processes even though Terminal can read the same file fine. Point
`ProgramArguments` at the `~/bin` deployed copy instead (run `./install.sh`
first if you haven't).

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.aaryan.claude-usage-watcher
rm ~/Library/LaunchAgents/com.aaryan.claude-usage-watcher.plist
rm ~/bin/claude_usage_watcher.py
# then remove the two hook entries from ~/.claude/settings.json by hand
rm -rf ~/.claude-usage-watcher
```

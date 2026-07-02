# Future Work: claude-usage-watcher

Ideas for extending this system, roughly ordered by how much value they'd
add relative to the effort. None of this is required — the system works
end-to-end today. This is a menu, not a roadmap.

## Correctness & data quality

**Confirm the real `StopFailure` payload schema.** The single highest-value
next step. `hit-limit` currently guesses at field names
(`reset_at`/`resets_at`/`reset_time`/`retry_after*`). The next real rate
limit hit will log the actual payload to `stop_failure_events.jsonl` —
read it, update the key list in `cmd_hit_limit`, and the tool goes from
"educated guess" to "confirmed" for its most reliable signal.

**Reduce reliance on hook-timing for the 5-hour window.** Right now
`inferred` windows can drift from Anthropic's true window start by however
long it takes for the first hook to fire after setup, or if usage happens
via claude.ai's web/mobile app. If Claude Code ever exposes the real
window start/end anywhere machine-readable (a `/status` JSON output, a
local session file, a future CLI flag), reading that directly would
eliminate the drift problem structurally instead of relying on `correct`
as a manual patch.

**Figure out the weekly cap's actual mechanics.** Every real weekly reset
observed via `correct weekly` is a data point. After a few, the pattern
(rolling-from-first-use vs. fixed weekly boundary) should become obvious
from the timestamps themselves — worth a quick script over the history
once there's enough data, then potentially auto-inferring it the same way
the 5-hour window is.

## Delivery & UX

**Push a notification the moment you actually get rate-limited, not just
on reset.** `hit-limit` currently only updates state silently. Since it
already recognizes a real rate-limit event, it could fire an immediate
"you just got capped, back at `<time>`" push — genuinely useful
information `record`'s inference alone can't give you, and the
infrastructure (`send_telegram`) already exists.

**Richer Telegram messages.** Telegram supports inline keyboard buttons —
e.g. a "Snooze 30 min" button that re-schedules the alarm, or a button
that deep-links back into a specific project. Currently the message is
plain bold text; this is a small addition on top of the existing
`sendMessage` call.

**A macOS menu bar countdown.** `state.json` already has everything needed
to show a live "resets in 3h12m" in the menu bar via a tool like
[xbar](https://xbarapp.com)/SwiftBar — a small script reading `status`
output every minute. Would make "when does it reset" visible at a glance
without opening Telegram or asking Claude.

**Multi-channel delivery for redundancy.** Fan the same QStash-scheduled
alarm out to a second channel (email via a simple SMTP relay, a Discord
webhook, Pushover) so a Telegram-specific outage doesn't mean total
silence. Low priority — Telegram has been fully reliable since the switch
from ntfy — but cheap insurance given how much debugging the original
delivery-channel problem took.

## Reliability & operations

**A `doctor` command.** Bundle the five real gotchas documented in
`SYSTEMDESIGN.md` (TCC folder access, bare `python3` SSL certs, QStash
region routing, `python3 -c` cwd issues, secrets file presence) into one
diagnostic command that checks each and reports actionable fixes. Would
turn "read the troubleshooting section and match your symptom" into "run
one command."

**Reconciliation between local state and QStash.** If `state.json` ever
gets wiped or manually edited while a QStash alarm is still pending (e.g.
you delete state to "reset" the tool), the orphaned alarm will still fire
with no local record of it. A `list-alarms`/`purge-alarms` command
(QStash's API supports listing/deleting by ID) would let you audit and
clean these up.

**Structured logging + `status --json`.** Right now `status` is
human-readable only. A `--json` flag would make it easy to pipe into the
menu bar idea above, or into any other tooling, without scraping text
output.

**Automated tests / CI.** `test.sh` is a manual shell script you run by
hand. Porting the same scenarios to `pytest` and running them in GitHub
Actions on every push would catch regressions (like the QStash-test
network leak this project actually hit once) automatically instead of
relying on remembering to run `./test.sh`.

## Platform & scale

**Cross-platform scheduler support.** The fallback poller is currently
`launchd`-only (macOS). A Linux `systemd` timer unit or Windows Task
Scheduler equivalent would be a small, mostly-mechanical port — the
Python script itself has no macOS-specific code outside of the TCC
workarounds, which don't apply on other platforms anyway.

**Multi-machine support.** If you use Claude Code from more than one
computer, each machine tracks its own local state independently right
now — a window started on your desktop won't be visible to your laptop's
`state.json`. Since an Upstash account is already in the stack, adding
Upstash Redis (same provider, same free-tier philosophy) as a shared state
store — instead of a local JSON file — would let every machine read/write
the same tracked windows, with QStash scheduling staying exactly as-is.

**Team / multi-account support.** If more than one person wanted this,
each tracked window would need to be keyed by user identity, and secrets
would need to move from a single flat file to something keyed per-user.
Not worth building until there's an actual second user.

## Security hardening

**Move secrets out of a plaintext file and into macOS Keychain.** Today,
`secrets.env` is a `chmod 600` plaintext file — reasonably safe (only your
user account can read it), but the `security` CLI would let the script
pull credentials from Keychain instead, which is encrypted at rest and a
strictly stronger guarantee. Worth doing if this ever runs on a shared or
higher-risk machine.

**Rotate the Telegram bot token and QStash token periodically**, same as
any long-lived credential — there's currently no expiry or rotation
reminder built in.

**Verify QStash's signing keys if a custom webhook receiver is ever
added.** Not applicable today (QStash calls Telegram directly, this script
never receives a callback) — but if any future feature adds a receiving
endpoint, Upstash's `QSTASH_CURRENT_SIGNING_KEY`/`QSTASH_NEXT_SIGNING_KEY`
(already in `.env`, unused today) exist specifically to verify that
incoming requests really came from QStash.

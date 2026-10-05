---
name: qtc-api_test
description: Run QtC's QA plan through the --test-api control socket — pick which groups and transports to cover, verify the right modem is up, run the offline harness and the scripted tests, update qa_tests/test_v0.15.0.txt as each one passes, and finish with a report of what passed, what was fixed, and what still needs Bill. Use when Bill asks to run the QA plan, run tests, check quality before a release, or invokes /qtc-api_test.
---

# QtC QA run

Bill built this because doing it by hand takes a long time and most of it is
mechanical. The point is **higher-quality software before a release**, not
just ticking boxes — so when something looks wrong, chase it rather than
recording a FAIL and moving on.

Bill is the licensed control operator. He sets up the radio, checks audio
levels and confirms the antenna or dummy load. Claude drives the software.
Never invert that.

## Hard rules

1. **Never send `KM`.** Not in any selection, not "just the cancel path", not
   to clean up test messages. It deletes every message still held for Bill on
   the BBS. D17 is the only test that goes near it and it runs on **the Pi
   BBS only** — never the main BBS. See the `feedback-never-send-km` memory.
2. **Never transmit without Bill's explicit OK**, every RF test, not once per
   session. Say what is about to go out and roughly how long it will key up.
3. **Never start a modem, change audio levels, or touch the radio.** If a
   modem is not running, stop and tell Bill exactly what to start.
4. **Clean up test artifacts in QtC's local database**, never with a BBS-side
   delete. Messages that actually flew and came back are real mail — leave
   them in the Inbox.
5. **Restore anything you change.** Config edits (e.g. clearing a
   `visited_bbs` key for D14) get a backup first and a byte-for-byte diff
   afterwards.

## Step 1 — read the plan

`~/vara_bbs_client/qa_tests/test_v0.15.0.txt` is both the test plan and the
run state. Parse `[P]` / `[U]` / `[F]` per group. A run that gets interrupted
resumes from whatever is still `[U]` — there is no separate state file to
drift out of sync.

Groups: A offline harness · B GUI/config · C reaching Direwolf · D on-air link
layer · E mail over RF · F YAPP · G PTT · H Mail-Call · I regression (VARA).

## Step 2 — ask what to run

Show the outstanding count per group first, then offer:

- **All (never KM)** — everything still `[U]` except D17
- **GUI / no airtime** — Group A and B, plus `tests/run_all.py`
- **Transports** — all, or individually: Telnet · Direwolf · VARA HF · VARA FM

## Step 3 — modem preflight

Check before anything, and report what you find rather than assuming:

| Transport | Check |
|---|---|
| Telnet | TCP to the BBS entry's host:port |
| Direwolf | TCP 127.0.0.1:8000 (AGW) |
| VARA HF / FM | TCP 127.0.0.1:8300 (command port) |

**VARA HF and VARA FM both use 8300 on this station, so a probe cannot tell
which one is running — ASK BILL which is up.** Do not guess and do not report
it as confirmed. (Bill accepted this limit deliberately; 8300/8301 are what
other software expects.)

If a modem is down: stop, name it, and wait. Bill starts it and confirms
levels and antenna.

## Step 4 — ordering

**Telnet first, always.** It costs no airtime and catches step-file and logic
bugs before they waste RF. Then the RF transport Bill has marked safe.

Within a transport, cheapest first: offline harness → GUI → Telnet-runnable →
RF.

## Step 5 — run

```bash
cd ~/vara_bbs_client/QtC

# 1. Offline harness first — 302 checks in ~3 s, free, run it every time.
python3 tests/run_all.py

# 2. Is one already running? Never launch a second on the same port.
ps -eo pid,cmd | grep "[p]ython3 main_window.py --test-api"

# 3. Launch, then POLL for the socket — never a fixed sleep.
setsid nohup python3 main_window.py --test-api=8787 \
    > /tmp/qtc-api.log 2>&1 < /dev/null & disown
./qtc_ctl.py -w 45 status          # -w waits up to 45 s for it to come up

# 4. Run a step file.
./qtc_ctl.py -f ../qa_tests/steps/<file>.txt
```

**Do not sleep a fixed number of seconds and then check.** Startup is
~2 s warm and ~15 s cold, so a fixed wait is either wasteful or declares
QtC dead too early — and the natural next move is to launch a SECOND
instance, which collides on the port. `-w` polls instead. If the port is
already taken, QtC now says so in one line and exits 2, leaving the
running instance alone, rather than dying on a traceback.

Launch QtC on Bill's display (not offscreen) so he can watch and hit
Disconnect. Step files live in `qa_tests/steps/`. The socket's commands and
the `!`/`{now}`/`seen`/`expect` step syntax are documented in **CLAUDE.md →
TESTING APPROACH → Driving the QA plan**.

Watch the BBS side too when it helps: `bbslog` follows LinBPQ's log over SSH
and handles the UTC filename rollover.

**Before each RF test:** stop. State the test, what goes on the air, the
estimate. Wait for Bill's OK.

**Show progress** as you go: which test, elapsed, estimate, how many left in
the selected set. Grounded estimates from real runs:

| | |
|---|---|
| Telnet connect + LM | ~5 s |
| Direwolf connect → BBS prompt | ~20 s |
| Direwolf full Mail-view sweep | ~3.5 min |
| A paged listing at 300 baud | ~90 s (19 lines; that is transmission, not a stall) |
| D16 round trip, two RF connects | ~10 min |

## Step 6 — when something fails

**Bill's rule: fix it if it would affect the rest of the testing; if the rest
can carry on with valid results, log it and continue.**

So: a bug in the code path under test, or anything that corrupts later state
(a stuck flag, a message left queued) — stop and fix. A cosmetic issue, or a
failure isolated to one test — record it and keep going.

Before blaming QtC, check whether the harness is at fault. On 2026-09-20
three "failures" were step-file and control-socket bugs, not app bugs. Read
the actual log, and correlate with the BBS's own log when RF is involved.

When a test passes, update its entry in the plan: `[U]` → `[P]`, with the
date, what was observed, and the log filenames. Save logs into `qa_tests/`.

## Step 7 — report

Four buckets, in this order:

1. **Passed** — test id, one line of what proved it
2. **Failed then fixed** — what broke, why, the commit
3. **Needs Bill's approval** — design calls not to be made alone
4. **Needs Bill manually** — audio levels, PTT, the Pi BBS, anything at the radio

Then: what is still `[U]`, and the cheapest next step.

## Before a release

This is a pre-release quality gate. Run it before the pack checklist in
CLAUDE.md, not after. `tests/run_all.py` must be green before any tarball.

## Ask when unsure

Bill would rather be asked than have a test run against the wrong BBS, the
wrong transport, or the radio unattended.

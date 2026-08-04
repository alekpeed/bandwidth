# Architecture

## The shape of it

Five layers, each of which can be understood without the ones above it:

```
  ui/                GTK 4 windows and dialogs
    |
  core/              scheduling, execution, parsing, the data model
    |
  engines/           adapters for speed-test programs
    |
  storage/           SQLite schema, migrations, CSV export
    |
  system/            file locations, logging, network introspection
```

Dependencies point downwards only. Nothing in `core/`, `storage/`,
`engines/` or `system/` imports GTK, which is why the whole of the
application's behaviour is testable without opening a window — and why the
background timer can run a test in a process that has no display at all.

### Modules

| Module | Responsibility |
|---|---|
| `application.py` | GTK application object. Opens the database, re-asserts the schedule, shows the window. |
| `cli.py` | Headless entry point. What the systemd timer runs; also the developer command. |
| `ui/main_window.py` | Status area, log table, bottom controls, export and delete dialogs. |
| `ui/details_dialog.py` | The complete stored record for one test. |
| `ui/settings_dialog.py` | Settings, and the first-run engine setup screen. |
| `core/models.py` | `TestRun`, statuses, trigger types, error categories, timestamp and unit helpers. |
| `core/test_runner.py` | Runs one test. The only place a test is ever run. |
| `core/scheduler.py` | Creates, updates and inspects the systemd user timer. |
| `core/result_parser.py` | Engine output → normalised measurement. |
| `engines/base.py` | The engine interface and error normalisation. |
| `engines/ookla.py`, `engines/speedtest_cli.py` | The two adapters. |
| `engines/registry.py` | Which engine to use. |
| `storage/database.py` | Connection, settings, queries, transactional writes. |
| `storage/migrations.py` | Versioned, transactional, non-destructive schema changes. |
| `storage/export_csv.py` | CSV export. |
| `system/paths.py` | XDG directories, all redirectable for testing. |
| `system/logging_setup.py` | The rotating diagnostic log. |
| `system/network_info.py` | Which interface the test went out through. |

---

## Why the scheduler is a systemd user timer

This was the decision with the most consequences, so it is worth setting out
the reasoning.

The specification requires three things that pull in the same direction:

1. Testing must continue **after the window is closed**.
2. Enabled scheduling must **resume after a restart and login**.
3. The window must show the **real** scheduler state — "derived from the
   actual scheduler state, not merely the last saved switch value" — and show
   *Scheduler error* with a Details button when the timer has failed.

An in-application timer satisfies none of them honestly. It dies when the
window closes unless a daemon is left behind; a hand-rolled daemon has to
reimplement start-at-login, restart-on-failure and crash recovery; and it can
only report on itself, so if it has died there is nothing left to notice or
say so. "The switch is on" would become the only available answer, which is
exactly the failure mode requirement 3 forbids.

A systemd **user** timer answers all three directly:

* The timer lives in the user's session manager, not in the application
  process. Closing the window is irrelevant to it.
* `systemctl --user enable` makes it start at login, which is the whole of
  requirement 2 with no code.
* `systemctl --user show` reports `ActiveState`, `Result` and
  `NextElapseUSecRealtime`. The window's "Next test" is read from systemd,
  not calculated hopefully, and when the timer is dead the window can say so
  and show the real `systemctl status` output behind the Details button.

The units are **user** units in `~/.config/systemd/user/`. Nothing is
installed system-wide and nothing needs a password — the application never
requires elevated privileges.

### The two units

`bandwidth-logger-test.service` is a `oneshot` running
`bandwidth-logger-run --scheduled`.

`bandwidth-logger-test.timer` drives it:

```ini
OnActiveSec=30min        # first run one interval after the timer starts
OnUnitActiveSec=30min    # then one interval after each run
AccuracySec=30s
Persistent=false         # never replay missed runs
```

`OnActiveSec` is what makes "enabling schedules the next run one interval
from now" and "changing the interval reschedules from now" both true: the
scheduler restarts the timer on every change, so the countdown always begins
at the moment of the change.

`Persistent=false` is what stops a laptop that was asleep for two days from
waking up and running two days' worth of catch-up tests. The gap is instead
*recorded* — see below.

### A failed test is not a failed unit

`bandwidth-logger-run` exits 0 when a test fails, and non-zero only when the
application itself fails (essentially: the database could not be written). If
a failed test exited non-zero, systemd would mark the unit failed, and one
bad test on a flaky connection — the exact situation this application exists
to document — could stop all future testing.

### Recording an interruption

When a scheduled run starts, and when the window opens, the scheduler
compares the time since the last attempt against twice the interval. A longer
gap means the machine was off, asleep or logged out. One record is written
(`trigger_type = startup_recovery`, `status = skipped`) naming the gap and
saying that missed tests are not replayed, and then the single due test
proceeds as normal.

Exactly one record per gap, and it triggers no extra tests. The point is that
a hole in the history is *explained* rather than merely absent.

---

## One test at a time, across processes

The window and the timer are **separate processes**, both able to start a
test. An in-process lock would not see the other one.

The lock is therefore an advisory `flock` on
`~/.local/state/bandwidth-logger/test-runner.lock`, taken non-blocking. The
kernel releases it when the holding process exits, so a crashed test cannot
wedge the scheduler permanently.

**Overlap policy: skip and record.** When the lock is already held, the new
attempt is written as `status = skipped`, `error_category = skipped_overlap`,
with the message "Skipped because the previous test was still running."

The alternative — queueing one deferred test — was rejected because it shifts
the schedule and adds state that has to be right across a crash, and because
skipping is the more honest record: it says what happened rather than
quietly moving a test to a time the user did not choose.

Covered by `tests/integration/test_test_runner.py::TestOverlapPrevention`,
including a check that only one engine process is ever started, and a
cross-process check that a second process sees the lock.

---

## Running the engine

`TestRunner._spawn` runs the engine with `subprocess.Popen`, an argument
**list**, and no shell. No element of the command is ever derived from user
input, so there is nothing to inject.

The child gets `start_new_session=True`, so stopping it means signalling the
whole process group: `SIGTERM`, five seconds' grace, then `SIGKILL`. Engines
spawn helpers, and a helper left holding the connection would poison the next
test. The fake engine deliberately spawns a sleeping child so the test suite
can prove the child dies too.

Duration is measured on the **monotonic** clock while the stored timestamps
stay wall-clock, so an NTP correction mid-test cannot produce a negative or
absurd duration.

Default timeout is 180 seconds, clamped to 30–3600. It is in Settings under
Advanced rather than in the main window, because it is a safeguard, not a
normal control.

---

## Missing data

The rule that shapes the data model: **unknown is `None` and stored as NULL;
zero is a measurement.**

A connection that transferred nothing measured 0 bits per second. An engine
that does not report packet loss at all measured nothing. Collapsing those
into the same cell would silently corrupt any average taken later.

So `coerce_float` returns `None` for absent input and `0.0` for a reported
zero, and it refuses to treat JSON booleans as numbers. `bps_to_mbps(None)`
is `None`, not `0.0`. CSV writes a blank cell for `None` and `0` for zero.
The details window says "Not measured" rather than showing an empty space.

`speedtest-cli` cannot measure jitter, packet loss or per-direction latency;
those columns stay NULL for its results rather than being filled with zeros.

---

## Storage

SQLite, in WAL mode with `synchronous = FULL` and a 30-second busy timeout —
because two processes write to it. Every result write is a transaction.

A write that fails does not vanish: `DatabaseError` is raised, the full record
is written to the diagnostic log, `RunOutcome.stored` is `False`, and the
window tells the user the result could not be saved. The one guarantee this
application makes is that no attempted test disappears, and a silent save
failure would break it.

Settings live in the database rather than a separate file, so one backup of
the data directory carries both the history and the configuration.

Migrations are append-only and run inside an explicit `BEGIN IMMEDIATE`.
Python's `sqlite3` does not open an implicit transaction for DDL — a
migration that created a table and then failed would otherwise leave the
table behind while recording the migration as unapplied. For the same reason
migrations use individual `execute()` calls rather than `executescript()`,
which commits before it runs. See [`SCHEMA.md`](SCHEMA.md).

---

## Threading in the interface

A test runs on a worker thread; every update returns to the main loop through
`GLib.idle_add`, because GTK widgets may only be touched from the thread that
owns the loop.

The window also re-reads the database every 10 seconds. Scheduled tests
happen in another process, so the window cannot rely on having seen them —
it shows what is actually stored.

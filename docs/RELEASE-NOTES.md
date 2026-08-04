# Release notes

## 1.6.0 — 4 August 2026

### The next run time, computed rather than asked for

1.5.0 read the next run from `systemctl list-timers --json`. systemd 255
rejects that outright:

```
$ systemctl --user list-timers bandwidth-logger-test.timer --all --json=short
systemctl: unrecognized option '--json=short'
```

so the window fell back to **Scheduled (next run time unavailable)** — true,
but not useful. Two things changed.

**The listing is now requested with `--timestamp=unix`**, which renders every
time as `@<seconds>`. That is supported where `--json` is not, and being a
bare number it is immune both to locale and to the pretty-printing that made
the `show` properties unusable in the first place.

**And there is now an answer that needs no systemd at all.**

Rather than chase a third systemd interface, this release computes the answer
from data the application already owns. `OnUnitActiveSec` fires one interval
after the service last ran, and every run is recorded with its start time, so
the next run is simply *last run + interval*.

It is shown as **about 10:12:18**. An estimate is labelled as one; presenting
a calculation as though systemd had reported it would repeat exactly the
overconfidence that produced "Not scheduled" for a timer that was running
perfectly.

The order is: ask `list-timers`, then the `show` properties, then compute.
When systemd does give a straight answer it is still preferred and shown
without the "about".

A stale history produces no estimate at all. If the expected time is well
past, the schedule was interrupted and the recorded history is the wrong
basis for a prediction — better to say the time is unavailable than to
display a confident wrong one.

### Test suite

The cross-process lock test now takes the lock directly instead of starting a
slow test and hoping the probe looks while it is still running. That race
failed under load for reasons unrelated to locking. The test is deterministic
and the suite is faster for it.

---

## 1.5.0 — 4 August 2026

Finishes the fix 1.4.0 got wrong.

### The next run time now comes from list-timers

1.4.0 assumed `systemctl show` would report the next firing in
`NextElapseUSecMonotonic`. On systemd 255 it does not. With the timer active
and firing on schedule, `show` reported:

```
ActiveState=active
NextElapseUSecMonotonic=infinity
LastTriggerUSec=Tue 2026-08-04 09:51:17 EDT
```

Three separate problems in four lines: the monotonic property reads
`infinity` for an `OnUnitActiveSec` timer whose next firing is computed from
the service's last activation; `NextElapseUSecRealtime` is absent entirely;
and the timestamps are pretty-printed as locale-dependent dates rather than
raw microseconds.

Meanwhile `systemctl list-timers` reported the next run correctly to the
second — because that is the code path systemd itself uses to answer this
question. The application now asks it the same way, via `--json`, and keeps
the `show` properties only as a fallback for older versions.

### An active timer is never called "Not scheduled"

Underneath the parsing bug was a worse habit: when the next run time could
not be read, the window said **Not scheduled** — denying that any test was
queued, while tests kept arriving on time.

It now says **Scheduled (next run time unavailable)**, which is true. An
unknown detail is not the same as an absent schedule, and the whole reason
this application uses systemd is to report what is really happening rather
than what it assumes.

Non-numeric time values like `infinity` are also read as "no time" rather
than failing to parse.

### Test suite

The cross-process lock test could fail under load: it held the lock for two
seconds, and a fresh Python interpreter plus package import sometimes took
longer than that before the probe could look. Widened, and confirmed stable.

---

## 1.4.0 — 4 August 2026

Two bugs found by watching someone use the application.

### "Not scheduled" while the timer was running fine

The window showed **Next test: Not scheduled** with automatic testing on, the
switch on, and the state Idle — while the systemd timer was counting down
correctly the whole time.

The timer is defined with `OnActiveSec`/`OnUnitActiveSec`. Those are
**monotonic** timers, measured from boot rather than from the epoch, and
systemd reports them in `NextElapseUSecMonotonic`, leaving
`NextElapseUSecRealtime` at zero. Only the realtime property was read, so the
answer was always "nothing scheduled".

The irony is pointed: the scheduler was built on systemd precisely so the
window could report the *real* state rather than guess — and then it read the
wrong property and reported the opposite of the truth. Both are now read, and
the monotonic value is converted to wall-clock time.

### The switch could be read backwards

The control was laid out as `Automatic testing [switch] Off`. With the state
word to the *right* of the switch, it reads as a label for whatever comes
next, not as the switch's own state — so a switch sitting in the off position
looked like it was on and merely labelled oddly.

It now reads **Automatic testing is currently Off** with the switch after the
words, so the state attaches to the sentence rather than floating beside an
unrelated control.

The switch is also re-synced from the stored setting on every refresh, so the
toggle and the actual schedule cannot silently disagree.

---

## 1.3.0 — 4 August 2026

Two things found by using the application.

### Delete selected records

Rows can now be multi-selected — click, Ctrl-click, Shift-click — and deleted
on their own, through **Delete selected** beside the existing Delete records.
The confirmation states the count and the dates covered, as range deletion
already did.

Previously the only options were everything or a date range, so removing a
single misleading reading meant taking its neighbours with it. That came up
immediately: after pinning a test server, the handful of earlier results were
worth discarding and the rest were not.

Selecting a row and opening its details are still separate gestures — a
double-click or Enter opens the record, clicking selects it.

### Applying an interval now says what happened

Pressing **Apply** while **Automatic testing** was switched off saved the
interval and scheduled nothing, without a word. The interval control and the
switch sit side by side, so "I set it to 5 minutes and pressed Apply" is a
reasonable thing to believe was enough — and the silence confirmed it.

Apply now explains that the interval was saved, that nothing is scheduled
because automatic testing is off, and that the switch is what starts it.

Toggling the switch off deliberately still says nothing extra; that action is
unambiguous on its own.

---

## 1.2.0 — 4 August 2026

Adds a **Test server** setting, because leaving the choice to the engine was
corrupting the very thing this application exists to produce: a history you
can compare across time.

### Why

Measured on one gigabit connection, minutes apart, over Ethernet:

| Server | Ping | Download |
|---|---|---|
| Pilot Fiber | **9.8 ms** (lowest) | **696 Mbps** (slowest) |
| Uniti | 10.0 ms | 811 Mbps |
| Starry | 10.2 ms | 869 Mbps |
| Spectrum | 10.3 ms | **926 Mbps** |
| Optimum | 34.1 ms | 773 Mbps |

The engine picks by **lowest latency**. On that line the lowest-latency
server was the slowest, and the 0.5 ms that separated it from the fastest is
noise — but it cost 230 Mbps, a 33% error.

Worse than the size of the error is its nature. The engine re-picks on every
run, so an unpinned history records *server changes as if they were
connection changes*. A dip in the graph could mean the connection degraded,
or merely that a different server answered. There is no way to tell them
apart after the fact, which makes the history unfit for the diagnosis it was
collected for.

### What changed

* **Settings → Test server.** Automatic, or pinned to one you choose.
* **Find nearby servers** fetches the list on demand. It contacts Ookla, so
  it happens only when you ask and never during a scheduled test.
* A pinned id is validated as numeric before it reaches the engine.
* A pin that is no longer in the fetched list is kept rather than silently
  dropped, so saving cannot quietly revert your choice.

### Choosing one

The fastest server is often not the closest, so measure rather than guess:

```bash
speedtest --servers | head -12
speedtest --server-id=NUMBER
```

Run each candidate once, pin the quickest, and leave it pinned. From then on
a change in your recorded speed means your connection changed.

### Existing records

Untouched. Every row already stored the server that answered it, so a
history that spans this change can be read correctly — check the Server
column to see where the engine was still choosing for itself.

---

## 1.1.0 — 4 August 2026

A correctness release. One change, for one reason: **the fallback speed-test
engine was reporting about half the real speed on a fast connection.**

### Removed the speedtest-cli fallback

Version 1.0.0 shipped `speedtest-cli` as a dependency so the application
worked the moment it was installed. In use on a gigabit line it reported
474 Mbps where speedtest.net reported 880. It opens far fewer parallel
connections than Ookla's client and cannot saturate a fast link.

For an application whose entire purpose is diagnosing a slow connection, that
is the worst failure available: the wrong number is plausible, it is recorded
permanently, and it reads as evidence against your provider. A number quietly
wrong by half is worse than no number at all.

The Ookla Speedtest CLI is now the only supported engine. When it is not
installed, each attempt is recorded as an `engine_missing` failure — visible
and explained — rather than measured with something less accurate.

### Fixed the Ookla install instructions

The commands shipped in 1.0.0 could not work on Ubuntu 24.04. Both problems
were found by running them:

* Ookla publishes **no repository for Ubuntu 24.04** ("noble") — that path
  returns 404 — so the upstream script reported the system as unsupported.
  The commands now force the jammy repository, which runs correctly on 24.04.
* Ubuntu's `speedtest-cli` package **owns `/usr/bin/speedtest`**, exactly
  where Ookla's package installs its binary, so `dpkg` refused to unpack it.
  Because 1.0.0 declared `speedtest-cli` as a dependency, this application
  was installing the one package that blocked its own preferred engine. The
  package now declares `Conflicts: speedtest-cli`.

The corrected sequence is verified end to end on Ubuntu 24.04.

### Existing records are kept

Nothing is deleted or rewritten. Results measured by the old engine keep
their `engine_name`, and the record details now carry an explanation of why
the figure may be low, so an old reading cannot be misread as a real
slowdown.

### Upgrading

Install the new `.deb` over the old one, by double-clicking it.

**If you already installed Ookla's engine while on 1.0.0**, `apt` will report
`bandwidth-logger : Depends: speedtest-cli but it is not installed` and refuse
to proceed. That is not a fault in the new package: installing Ookla's engine
removes `speedtest-cli`, which the *old* version depended on, so the system is
left in a broken state that `apt` will not resolve on its own. Recover with:

```bash
sudo dpkg -i bandwidth-logger_1.1.0-1_all.deb
sudo apt-get -f install
```

Verified on Ubuntu 24.04: this upgrades cleanly, leaves the Ookla engine in
place, and preserves every stored record.

Once on 1.1.0 the situation cannot recur, because the package conflicts with
`speedtest-cli` rather than depending on it.

You then need the Ookla engine if you do not already have it — see the README,
or the **Set up speed-test engine** screen.

---

## 1.0.0 — 4 August 2026

First release.

### What it does

* Runs internet speed tests manually or on a schedule, from every 5 minutes
  to every 24 hours, plus a custom interval.
* Records **every attempted test** — successes, failures, timeouts,
  cancellations, and attempts skipped because another test was still running.
  A gap in the history always has a stated reason.
* Keeps scheduling running with the window closed, and resumes it after
  logout or a restart.
* Shows the complete stored record for any test, including the raw engine
  output.
* Exports the full history, or a date range, to CSV.
* Keeps everything on your computer. No account, no cloud, no analytics.

### Decisions worth knowing about

**Scheduling uses a systemd user timer.** It survives the window closing,
comes back at login, and — the reason it was chosen over an in-application
timer — can be *asked* what it is really doing, so "Next test" is read from
systemd rather than assumed, and a dead timer is reported as *Scheduler
error* rather than silently shown as healthy.

**Overlapping tests are skipped, not queued.** If a scheduled test comes due
while another is running, a record is written with status `skipped` and
category `skipped_overlap`. Two speed tests at once would measure each other.
Queueing was rejected because it shifts the schedule and hides what happened;
skipping says so plainly.

**Missed runs are never replayed.** A machine that was off or asleep resumes
with one test on the normal schedule. The gap is written into the history as
its own record — explained rather than merely absent.

**Zero is not the same as unknown.** A value the engine did not report is
stored as NULL and exported as a blank cell; a measured zero is stored and
exported as `0`. Averaging a column that conflated the two would be wrong in
a way nobody would notice.

**The Ookla CLI is used but not shipped.** Its licence does not permit
redistribution, so it is not bundled and is not a dependency. The
open-source `speedtest-cli` *is* a dependency, so the application works the
moment it is installed. When Ookla's CLI is present it is preferred, because
it reports jitter, packet loss and per-direction latency, which the fallback
cannot. Every record names the engine that produced it.

---

## Known limitations

**Scheduling needs systemd, and needs you logged in.** The timer is a user
unit, so it runs while your user session exists. It does *not* run when
nobody is logged in — enabling systemd "lingering" would change that, but
requires administrator rights, which the application deliberately never asks
for. On a system without systemd, manual tests still work and the window says
plainly that background scheduling is unavailable.

**No graph.** The first release stores and exports; it does not visualise.
Export to CSV and plot in LibreOffice Calc.

**Interval accuracy is ±30 seconds** (`AccuracySec=30s`), which lets systemd
batch wake-ups and saves battery. Tests do not fire at exactly the displayed
second.

**The Ookla engine must be installed separately.** Its licence forbids
redistribution, so the application cannot ship it and does nothing useful
until it is present. On Ubuntu 24.04 the install needs two non-obvious
workarounds, both documented in the README and built into the setup screen.

**The engine's numbers are the engine's numbers.** A speed test measures the
path to one server at one moment. Since 1.2.0 you can pin that server, and
you should — until you do, the engine re-picks by latency on every run and
results can shift independently of your connection. The server is recorded on
every row either way.

**Interface and connection type are best-effort.** Read from `/proc/net/route`
and `/sys/class/net`. On unusual setups — some VPNs, bridges, containers —
they may be NULL. NULL rather than a guess is the intended behaviour.

**Error categories are pattern-matched** from engine output, which changes
between engine releases. An unrecognised failure becomes `process_error`; the
engine's complete text is always kept on the record regardless.

**No retention limit.** Nothing is ever deleted automatically. A 5-minute
interval is about 105,000 records a year, roughly 100–200 MB with raw engine
output included. Use **Delete records** if that matters to you.

**The window must be open to see live progress.** Scheduled tests run
headless by design; the table refreshes every 10 seconds when the window is
open.

**Deleted records cannot be recovered.** Export first.

---

## Verification status

232 automated tests pass on Ubuntu 24.04.4 with Python 3.12.3 and GTK 4.14.5.
The suite uses a fake engine throughout and never consumes bandwidth.

Of the 17 manual acceptance tests, 16 are now confirmed — including menu
launch and background testing with the window closed, both verified on a real
Ubuntu desktop after release. One remains partial (the exported CSV has not
been opened in LibreOffice Calc) and one is unverified (scheduling resuming
after a reboot).

Full detail, with evidence and a checklist for the remaining items, is in
[`ACCEPTANCE.md`](ACCEPTANCE.md).

---

## Upgrading

`sudo apt install ./bandwidth-logger_<version>_all.deb`. Records and settings
are preserved; this was verified by building a 1.0.1 package and upgrading a
database containing records and a non-default interval.

## Removing

`sudo apt remove bandwidth-logger` leaves your records in place, by design.
See the README for how to delete them deliberately.

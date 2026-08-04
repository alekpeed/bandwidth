# Release notes

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

**The engine's numbers are the engine's numbers.** A speed test measures the
path to one server at one moment. Both supported engines pick the server
themselves and this release does not let you pin one, so a change in chosen
server can shift results independently of your connection. The server is
recorded on every row so you can see when that happened.

**`speedtest-cli` reports less than Ookla's CLI.** No jitter, no packet loss,
no per-direction latency. Those columns stay empty for its results rather
than being filled with zeros.

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

194 automated tests pass on Ubuntu 24.04.4 with Python 3.12.3 and GTK 4.14.5.
The suite uses a fake engine throughout and never consumes bandwidth.

Of the 17 manual acceptance tests, 4 were verified directly, 9 are covered by
the automated suite, 1 is partial, and 3 need a desktop machine with a real
login session — menu launch, testing with the window closed, and scheduling
resuming after a reboot.

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

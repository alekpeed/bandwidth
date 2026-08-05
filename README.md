# Bandwidth Logger

A native Ubuntu desktop application that runs internet speed tests on a
schedule and keeps a permanent, timestamped record of every one.

It exists for a specific problem: an internet connection that is fine when
you check it and bad when you need it. A single reading proves nothing. A
history of readings, with the failures included, proves something.

* No account, no cloud service, no browser dashboard, no Docker.
* No terminal needed after installation.
* Every attempted test is recorded — including tests that failed, timed out,
  or were skipped. A gap in the history always has a stated reason.
* Everything stays on your computer.

---

## Contents

* [Installing](#installing)
* [Using it](#using-it)
* [Where your data lives](#where-your-data-lives)
* [Building from source](#building-from-source)
* [Running the tests](#running-the-tests)
* [Upgrading](#upgrading)
* [Uninstalling](#uninstalling)
* [Documentation](#documentation)

---

## Installing

Requires Ubuntu 24.04 LTS or later, on x86-64.

**Download `bandwidth-logger_1.7.0-1_all.deb` and double-click it.** Your
software installer opens, you press Install, and that is the whole procedure.

Then launch **Bandwidth Logger** from the application menu — press the Super
key and start typing `bandwidth`.

Nothing after this point needs a terminal.

<details>
<summary>Installing from a terminal instead</summary>

```bash
cd ~/Downloads
sudo apt install ./bandwidth-logger_1.7.0-1_all.deb
```

The leading `./` is required; without it `apt` looks for a package of that
name in Ubuntu's archive and reports that it cannot find one. `apt` is used
rather than `dpkg -i` because it pulls in the dependencies in one step.

If `apt` reports `Unsupported file ... given on commandline`, it has not
recognised the file as a Debian archive. Check the download with:

```bash
file ~/Downloads/bandwidth-logger_1.7.0-1_all.deb
```

A good copy reports `Debian binary package (format 2.0)`. Anything else means
the download did not complete or was altered in transit — fetch it again, or
just double-click it in Files, which is the supported route.

</details>

### Installing the speed-test engine — required

Bandwidth Logger does not measure your connection itself. It drives the
**official Ookla Speedtest CLI** — the same engine speedtest.net uses — and
records what it reports. Ookla's licence does not permit anyone else to
redistribute it, so it is not included in this package and you have to
install it once.

The application's **Set up speed-test engine** screen shows these commands.
Until it is installed, every attempt is recorded as a failure rather than
measured with something less accurate.

```bash
sudo apt-get remove -y speedtest-cli
curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh \
  | sudo env os=ubuntu dist=jammy bash
sudo apt-get install -y speedtest
```

Two details in there are not obvious, and both were found by the plain
commands failing:

* **`dist=jammy` is required on Ubuntu 24.04.** Ookla publishes no repository
  for 24.04 ("noble") — that path returns 404 — so the unmodified upstream
  script reports your system as unsupported. The jammy build runs correctly
  on 24.04.
* **`speedtest-cli` must be removed first.** Ubuntu's package for it owns
  `/usr/bin/speedtest`, the very path Ookla's package installs to, so `dpkg`
  refuses to unpack Ookla's engine while it is present.

Verify with `speedtest --version`; it should say *Speedtest by Ookla*.

> **Removed in 1.1.0.** Version 1.0.0 shipped `speedtest-cli` as a fallback so
> the application worked immediately. It was removed because it under-reports
> throughput badly on fast connections — roughly half the real figure on a
> gigabit line, since it cannot open enough parallel connections to fill one.
> For an application whose purpose is diagnosing a slow connection, a number
> that is quietly wrong by half is worse than no number at all. Results already
> recorded by it are kept, and the record details now say so.

---

## Using it

The window has three parts.

**The status area, across the top.**

* **Automatic testing** — on or off.
* **Test interval** — 5, 10, 15 or 30 minutes; 1, 2, 4, 6, 12 or 24 hours; or
  a custom interval in whole minutes or hours. The shortest permitted
  interval is 5 minutes. Press **Apply** and the new interval takes effect
  immediately, counting from that moment.
* **Next test** — the exact local date and time, or *Not scheduled*.
* **Current state** — Idle, Testing, Saving result, Disabled or Error.
* **Test Now** — runs a test immediately.

**The log table, in the middle.** Every attempted test, one row each: local
date and time, status, download and upload in Mbps, ping in ms, server, ISP,
connection, and duration. Sort newest-first or oldest-first. Select a row and
press Enter, or double-click it, to see the complete stored record — every
field, including the raw engine output.

**The controls, along the bottom.** Export CSV, Open data folder, Delete
records, Settings.

### Automatic testing keeps running with the window closed

Scheduling is done by a systemd **user** timer, not by the window. Closing
the window does not stop it, and it comes back after you log out and back in
or restart the computer. The first time you close the window with automatic
testing switched on, the application says so once.

To stop automatic testing, switch **Automatic testing** off. Nothing else
switches it off, and closing the window never silently does.

If a scheduled test comes due while another test is still running, the
scheduled one is **skipped and recorded as skipped** rather than run
alongside. Two speed tests at once would measure each other.

A computer that was switched off or asleep does not replay the tests it
missed. It resumes with one test on the normal schedule, and the interruption
is written into the history as its own record, so the gap is explained rather
than unexplained.

### Exporting

**Export CSV** writes every record, or a date range, as UTF-8 CSV that opens
directly in LibreOffice Calc. Failed and skipped attempts are included, so
the file matches the table exactly.

Raw and display values are separate columns — `download_bps` alongside
`download_mbps` — and a value that was never measured is a blank cell, never
a zero.

Your external IP address is stored with each record but is **excluded from
exports by default**. Tick the box in the export dialog to include it.

### Recording throughput

A speed test measures **capacity** — how fast the line *can* go — by
saturating it. The throughput monitor measures **usage** — how much is
actually flowing — by reading counters the kernel keeps anyway. It sends
nothing, uses no bandwidth, and needs no privileges.

Switch it on in **Settings → Throughput monitor**. A background service then
records one row a minute holding the mean, the peak and the total bytes each
way. The peak is the point: a ten-second burst at full line rate shows up
there even when the minute's average looks quiet.

The window also shows a live **Traffic now** readout, updated every two
seconds, read straight from the counters — so it works whether or not the
background monitor is switched on.

**This also explains odd speed-test results.** A test run while the
connection is already busy measures only the capacity left over, so a
scheduled test that happens to fire during a large download records a low
figure that looks like a fault and is not. With the monitor running, every
result stores what else was in flight at the time.

Throughput samples are kept for 30 days by default, adjustable in Settings.
Speed-test records are never pruned — that permanence is the point of the
application, and nothing about the monitor changes it.

From a terminal, if you want it:

```bash
bandwidth-logger-monitor --once      # one reading, right now
bandwidth-logger-monitor --summary   # recent recorded minutes
bandwidth-logger-run --export-throughput ~/throughput.csv
```

### Choosing a test server

**Settings → Test server** decides which server your results are measured
against. This matters more than it sounds.

The engine picks by **lowest latency**, which is not the same as fastest. On
one gigabit line the lowest-latency server returned 696 Mbps and another
0.5 ms further away returned 926 — a 33% difference from a latency gap that
is pure noise.

Left automatic, the engine also re-picks on every run, so a dip in your
history might mean your connection degraded, or might mean a different server
answered. You cannot tell those apart afterwards, which is exactly the
question the history exists to answer.

Press **Find nearby servers**, then measure the candidates rather than
guessing:

```bash
speedtest --servers | head -12
speedtest --server-id=NUMBER
```

Pin the quickest and leave it. From then on, a change in your recorded speed
means your connection changed.

Fetching the list contacts Ookla, so it happens only when you press the
button — never during a scheduled test.

### Deleting

**Delete selected** removes just the rows you have highlighted. Click to
select, Ctrl-click to add, Shift-click for a run of them. Double-click or
Enter still opens a record's details rather than selecting it.

**Delete records** removes everything, or a date range.

Both confirm first, showing how many records will go and the dates they span.
Nothing is ever deleted automatically and there is no retention limit.

**Switching on automatic testing is the switch, not the Apply button.** Apply
saves the interval; the **Automatic testing** switch starts the schedule. If
you press Apply with the switch off, the application now tells you that
nothing was scheduled.

---

## Where your data lives

| What | Where |
|---|---|
| Records and settings | `~/.local/share/bandwidth-logger/bandwidth-logger.sqlite3` |
| Diagnostic log | `~/.local/state/bandwidth-logger/application.log` |
| Timer and services | `~/.config/systemd/user/bandwidth-logger-test.{timer,service}`, `bandwidth-logger-monitor.service` |

The SQLite database is the authoritative history. The diagnostic log is only
for troubleshooting; it rotates and is not a record of results.

**Privacy.** Nothing is transmitted anywhere except the speed test itself,
which necessarily contacts a test server. No analytics, no telemetry, no
credentials stored. The application never needs administrator privileges to
run — only installing it does, like any other package.

---

## Building from source

```bash
sudo apt install debhelper dh-python pybuild-plugin-pyproject \
                 python3-all python3-setuptools fakeroot dpkg-dev

./packaging/build-deb.sh              # writes dist/bandwidth-logger_*.deb
./packaging/build-deb.sh --lint       # and runs lintian
```

The Debian control files live in `packaging/debian/`. `dpkg-buildpackage`
requires them at `./debian`, so the build script stages a symlink there and
removes it afterwards.

To run from a source checkout without installing:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 speedtest-cli
PYTHONPATH=src python3 -m bandwidth_logger.application
```

---

## Running the tests

```bash
sudo apt install python3-pytest
./packaging/run-tests.sh
```

The suite uses a fake engine and recorded fixture output throughout. **It
never contacts a speed-test server and never consumes internet bandwidth.**
Every test runs against a temporary data directory, so your real database,
diagnostic log and systemd units are untouched.

The GUI smoke test needs GTK 4 and a display, and skips itself when either is
missing. On a headless machine, `sudo apt install xvfb` lets it run.

See [`docs/TESTING.md`](docs/TESTING.md) for what is covered.

---

## Upgrading

Double-click the newer `.deb`, or:

```bash
sudo apt install ./bandwidth-logger_<newer-version>_all.deb
```

**Upgrading from 1.0.0 with Ookla's engine already installed** is the one case
that needs care. `apt` will refuse with `bandwidth-logger : Depends:
speedtest-cli but it is not installed`, because installing Ookla's engine
removed the package 1.0.0 depended on. Recover with:

```bash
sudo dpkg -i bandwidth-logger_1.7.0-1_all.deb
sudo apt-get -f install
```

Your records and settings are kept. Schema changes are applied automatically
as versioned, transactional migrations that only ever add — no migration
drops a table or column, or deletes a row. See
[`docs/SCHEMA.md`](docs/SCHEMA.md).

---

## Uninstalling

Through the App Center, or:

```bash
sudo apt remove bandwidth-logger
```

**Your records are not deleted.** That is deliberate — the history is the
point of the application, and removing a package should not throw away data
you may still want. Export to CSV first if you want a copy in a portable
form.

To stop background testing before removing the package, switch **Automatic
testing** off in the window, or run:

```bash
systemctl --user disable --now bandwidth-logger-test.timer
```

To delete your records and the timer afterwards:

```bash
rm -rf ~/.local/share/bandwidth-logger \
       ~/.local/state/bandwidth-logger
rm -f  ~/.config/systemd/user/bandwidth-logger-test.timer \
       ~/.config/systemd/user/bandwidth-logger-test.service
systemctl --user daemon-reload
```

---

## Documentation

| Document | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module layout, and why the scheduler is a systemd user timer |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Database schema and the migration rules |
| [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) | Third-party dependencies and their licences |
| [`docs/TESTING.md`](docs/TESTING.md) | What the automated suite covers |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | Manual acceptance tests and their current results |
| [`docs/RELEASE-NOTES.md`](docs/RELEASE-NOTES.md) | Release notes and known limitations |

---

## Licence

GPL-3.0-or-later. See [`LICENSE`](LICENSE).

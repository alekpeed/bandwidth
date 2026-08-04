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

Download `bandwidth-logger_1.0.0-1_all.deb` and install it:

```bash
sudo apt install ./bandwidth-logger_1.0.0-1_all.deb
```

Double-clicking the `.deb` in Files, or opening it with the App Center, works
the same way. `apt` is used rather than `dpkg -i` because it pulls in the
dependencies in one step.

Then launch **Bandwidth Logger** from the application menu.

### About the speed-test engine

Bandwidth Logger does not measure your connection itself; it drives a
separate speed-test program and records what that program reports. Two are
supported:

| Engine | Licence | How it arrives |
|---|---|---|
| **Ookla Speedtest CLI** | Proprietary (Ookla EULA) | Preferred when installed. Not included — see below. |
| **speedtest-cli** | Apache-2.0 | Installed automatically as a dependency. |

`speedtest-cli` is a package dependency, so **the application works as soon
as you install it**, with nothing else to do.

The Ookla CLI measures more — jitter, packet loss and per-direction latency,
which `speedtest-cli` cannot report at all — so it is used automatically when
present. Its licence does not permit redistribution by anyone else, so it is
not bundled here and is not a dependency. If you want it, the application's
**Set up speed-test engine** screen shows you the two commands that add
Ookla's own software source and install it. That is Ookla's software, not
part of this application.

Whichever engine measured a given result is recorded on that result, so you
are never unknowingly comparing numbers from two different tools.

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

### Deleting

**Delete records** asks you to confirm, showing how many records will go and
the dates they span. Nothing is ever deleted automatically and there is no
retention limit.

---

## Where your data lives

| What | Where |
|---|---|
| Records and settings | `~/.local/share/bandwidth-logger/bandwidth-logger.sqlite3` |
| Diagnostic log | `~/.local/state/bandwidth-logger/application.log` |
| Timer and service | `~/.config/systemd/user/bandwidth-logger-test.{timer,service}` |

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

```bash
sudo apt install ./bandwidth-logger_<newer-version>_all.deb
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

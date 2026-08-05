# Acceptance test results

The specification lists 17 manual acceptance tests that must pass on a clean
Ubuntu 24.04 installation.

**This document is honest about what has and has not been verified.** The
build and verification were performed in a **headless Ubuntu 24.04.4
container** — a real Ubuntu 24.04 with a real `dpkg`, but with:

* no desktop session, so no application menu and no App Center;
* no systemd user instance, so no live timer;
* no usable internet path to a speed-test server, so **no real speed test was
  ever run** (deliberately — the automated suite must not consume bandwidth,
  and a live test would prove nothing repeatable anyway).

Items are marked:

* **PASS** — verified here, with the evidence shown.
* **PASS (automated)** — the behaviour is proven by the automated suite
  against a fake engine and a stubbed `systemctl`, but not by a person
  clicking in a desktop session.
* **NOT VERIFIED** — needs a desktop Ubuntu 24.04 machine. What to do, and
  what to expect, is stated.

Nothing is marked passed that was not actually exercised.

---

## Environment used

| | |
|---|---|
| OS | Ubuntu 24.04.4 LTS (headless container) |
| Python | 3.12.3 (system) and 3.11.15 |
| GTK | 4.14.5, PyGObject 3.48.2 |
| Package built | `bandwidth-logger_1.7.0-1_all.deb` |
| Test suite | 232 passed |
| lintian | 1 warning (`initial-upload-closes-no-bugs`, expected for a first release) |

---

## The 17 acceptance tests

### 1. Install through the .deb without manually creating files — **PASS**

```
$ sudo apt install ./dist/bandwidth-logger_1.7.0-1_all.deb
Setting up speedtest-cli (2.1.3-2) ...
Setting up bandwidth-logger (1.0.0-1) ...
Processing triggers for hicolor-icon-theme (0.17-2) ...
Processing triggers for man-db (2.12.0-4build2) ...
$ which bandwidth-logger bandwidth-logger-run
/usr/bin/bandwidth-logger
/usr/bin/bandwidth-logger-run
```

No file was created by hand. The fallback engine `speedtest-cli` was pulled
in automatically as a dependency, so the application is usable immediately.

### 2. Launch from the application menu — **PASS** (confirmed on a desktop)

Verified after release on a real Ubuntu desktop: the application launches from
the menu and the window opens correctly.

The original headless finding is kept below for the record.

What was verified: the desktop entry installs to
`/usr/share/applications/org.bandwidthlogger.BandwidthLogger.desktop`, the
icon to `/usr/share/icons/hicolor/scalable/apps/`, and AppStream metadata to
`/usr/share/metainfo/`. Both validate cleanly:

```
$ desktop-file-validate /usr/share/applications/org.bandwidthlogger.BandwidthLogger.desktop
(no output — valid, no hints)
$ appstreamcli validate --no-net /usr/share/metainfo/org.bandwidthlogger.BandwidthLogger.metainfo.xml
✔ Validation was successful: pedantic: 1
```

`Categories` was reduced to `Network;Monitor;` during verification because
`desktop-file-validate` warned that two main categories would make the entry
appear twice in the menu.

Also verified: the whole interface builds under real GTK 4.14 with no
warnings (see item 6).

**To confirm:** install on a desktop Ubuntu 24.04 and open the Activities
overview; "Bandwidth Logger" should appear with its icon.

### 3. The application identifies whether the engine is available and gives a usable graphical remedy — **PASS (automated)**

Engine detection verified live on the installed package:

```
$ bandwidth-logger-run --status
Engine preference:   auto
Engine selected:     speedtest-cli (open source)
Engines installed:   speedtest-cli
```

With no engine on `PATH`, an attempt is recorded rather than silently
dropped:

```
  status            Failed
  error category    engine_missing
  error summary     Speed-test engine is not installed
```

The graphical remedy — `EngineSetupDialog`, which names Ookla's software as
Ookla's, shows the install commands, and offers Copy / Open terminal / Open
website — is built successfully by the GUI smoke test. It has not been
clicked through by a person.

### 4. Set the interval to 30 minutes and enable automatic testing — **PASS (automated)**

`tests/integration/test_scheduler.py::TestEnabling` asserts the generated
unit and the exact `systemctl` calls:

```ini
OnActiveSec=30min
OnUnitActiveSec=30min
Persistent=false
```
```
--user daemon-reload
--user enable bandwidth-logger-test.timer
--user restart bandwidth-logger-test.timer
```

Against a stubbed `systemctl` — not a live timer.

### 5. Close the window; a test still runs at the scheduled time — **PASS** (confirmed on a desktop)

Verified after release. With a 5-minute interval and the window closed, a
scheduled test ran unattended and appeared in the table:

```
2026-08-04 09:40:09   Success   930.39 Mbps   Spectrum (New York, NY)
2026-08-04 09:35:18   Success   929.47 Mbps   Spectrum (New York, NY)
```

`systemctl --user list-timers` agreed:

```
NEXT                        LEFT      LAST                        PASSED
Tue 2026-08-04 10:12:18 EDT 2min 34s  Tue 2026-08-04 10:07:18 EDT 2min 25s ago
```

This is the item the whole systemd design exists for. The original headless
finding is kept below for the record.

What is verified: `bandwidth-logger-run --scheduled` — the exact command the
timer runs — works from a cold process and stores a scheduled record
(`test_cli_and_persistence.py::TestScheduledRun`); the application's
`do_shutdown` is asserted, by reading its source, to close the database and
do nothing that could stop or disable the timer; the timer is a systemd user
unit and so is unaffected by the window closing.

**To confirm:** enable a 5-minute interval, close the window, wait, then
`systemctl --user list-timers bandwidth-logger-test.timer` and
`bandwidth-logger-run --status`.

### 6. Reopen the application; the new record is visible — **PASS (automated)**

`tests/integration/test_gui_smoke.py` builds the real window against a
database seeded with one successful and one failed record, and asserts the
table populated with both, that all nine columns exist, and that refresh and
re-ordering work — with **no GTK CRITICAL or warning output**. The window
also re-reads the database every 10 seconds, which is how records written by
the timer process appear.

### 7. Run Test Now; only one test runs and one record is added — **PASS (automated)**

`test_test_runner.py::TestOverlapPrevention` starts two simultaneous
requests and asserts exactly one `success` and one `skipped`, two records
total, and — the decisive check — that the engine was invoked **once**. A
separate test proves the lock is visible from a different process.

### 8. Disconnect the network and run a test; a failed record with timestamps and an error is added — **PASS (automated)**

`test_test_runner.py::TestFailures` runs the fake engine's `dns-failure`
behaviour (real subprocess, exit code 2, real Ookla error text) and asserts
a `failed` record with `error_category = dns_failure`, both timestamps, a
duration, NULL measurements, and the engine's complete message preserved.

Not verified with a physically unplugged cable.

### 9. Restore the network; future automatic tests continue normally — **PASS (automated)**

`test_test_runner.py::test_one_failure_does_not_prevent_the_next_test`. Also
by design: `bandwidth-logger-run` exits 0 on a failed test specifically so
systemd cannot mark the unit failed and stop the timer — asserted by
`test_a_failed_test_still_exits_zero_so_the_timer_keeps_running`.

### 10. Restart the computer; enabled scheduling resumes after login — **NOT VERIFIED** (cannot reboot a container)

The mechanism is `systemctl --user enable bandwidth-logger-test.timer`,
which is asserted to be issued whenever "Run scheduler after login" is on.
`reapply_from_settings()` additionally re-asserts the stored schedule at every
start-up, repairing a unit lost to an upgrade.

**To confirm:** enable scheduling, reboot, log in, and check
`systemctl --user is-active bandwidth-logger-test.timer`.

### 11. Change the interval; the displayed and actual next run update — **PASS (automated)**

`test_scheduler.py::TestIntervalChanges` asserts the unit is rewritten to the
new interval (with no trace of the old one), and that `daemon-reload` plus
`restart` follow — the restart being what makes the change take effect from
now. `test_the_next_run_time_comes_from_systemd` asserts the displayed time
is read from systemd's `NextElapseUSecRealtime` rather than calculated.

### 12. Disable scheduling; no automatic tests occur — **PASS (automated)**

`test_scheduler.py::TestDisabling` asserts `stop` and `disable` on the timer,
and that **no command mentions the service unit** — so a test already running
is not killed.

### 13. Export all records; the CSV opens correctly in LibreOffice Calc — **PARTIAL**

Export itself is verified thoroughly: `test_export_csv.py` covers UTF-8,
header row, one row per attempt, ISO 8601 timestamps, separate raw and
display columns, blank cells for missing values, `0` preserved for measured
zeros, and correct quoting of commas, quotes and embedded line breaks
(round-tripped through `csv.DictReader`). Verified live through the installed
package too:

```
$ bandwidth-logger-run --export /tmp/export.csv
Exported 1 record(s) to /tmp/export.csv
```

**Not verified:** actually opening the file in LibreOffice Calc. The file is
RFC 4180 CSV written by Python's `csv` module, which Calc reads natively.

### 14. Export a date range; only matching records are included — **PASS (automated)**

`test_export_csv.py::TestDatabaseExport::test_date_range_includes_only_matching_records`
seeds records across 10 days and asserts a 3-day range yields exactly 3 rows.
Ranges are inclusive whole local days, converted to a UTC window so records
near midnight are not lost.

### 15. Upgrade to a newer package build; prior results and settings remain — **PASS**

Verified for real, by building a 1.0.1 package and upgrading:

```
BEFORE UPGRADE -> records: 12 | interval: 240 | schema: 1
$ sudo apt install ./dist/bandwidth-logger_1.7.0-1_all.deb
Setting up bandwidth-logger (1.0.1-1) ...
AFTER UPGRADE  -> records: 12 | interval: 240 | schema: 1
first ISP: ISP 0 | last ISP: ISP 11
```

All 12 records and the non-default 240-minute interval survived.

### 16. Uninstall; user data is not silently deleted — **PASS**

```
$ sudo apt remove bandwidth-logger
db still present after remove: YES
$ sudo apt purge bandwidth-logger
db still present after purge:  YES
12 records survive
```

The package ships no maintainer script that touches `$HOME`. Manual removal
is documented in the README, in `bandwidth-logger(1)`, and in the Settings
window, which shows both paths.

### 17. No normal operation requires administrator privileges or Terminal — **PASS (by construction)**

Everything the application writes is under `$HOME`: the database, the
diagnostic log, and the systemd **user** units. Every `systemctl` call is
`--user` scoped — asserted for every recorded call by
`test_scheduler.py::test_the_units_are_user_units_in_the_home_directory`.

The two exceptions are honest ones, and are the *only* places a password is
mentioned: installing the package, and optionally installing Ookla's CLI.
Both are installing system software.

---

## Summary

| Result | Count | Items |
|---|---|---|
| PASS (verified directly) | 6 | 1, 2, 5, 15, 16, 17 |
| PASS (automated) | 9 | 3, 4, 6, 7, 8, 9, 11, 12, 14 |
| PARTIAL | 1 | 13 (export verified; not opened in Calc) |
| NOT VERIFIED | 1 | 10 (scheduling resumes after a reboot) |

Items 2 and 5 were confirmed on a real Ubuntu desktop after release. Item 5
matters most of all — a scheduled test ran with the window closed — because
it is the reason the scheduler is a systemd user timer rather than something
inside the application.

Only item 10 remains: enable scheduling, reboot, log in, and confirm a record
appears without opening the window. The mechanism is
`systemctl --user enable`, which is asserted by the test suite, but it has not
been observed end to end.

---

## Field findings after release

Real use on a gigabit Ubuntu desktop found nine defects the automated suite
could not, each fixed in the release named:

| Defect | Release |
|---|---|
| The fallback engine reported half the real speed on a fast line | 1.1.0 |
| Ookla's own install commands fail on Ubuntu 24.04 (no `noble` repository) | 1.1.0 |
| The `speedtest-cli` dependency blocked Ookla's engine from installing | 1.1.0 |
| The engine picked the lowest-latency server, which was the slowest — a 33% error | 1.2.0 |
| Deletion could not remove a single record | 1.3.0 |
| Apply with the switch off saved the interval and said nothing | 1.3.0 |
| The switch's state word sat where it could be read backwards | 1.4.0 |
| "Not scheduled" shown for a timer that was running correctly | 1.4.0–1.6.0 |
| The cross-process lock test raced and failed under load | 1.6.0 |

The pattern worth noting: none of these were logic errors. They were wrong
assumptions about the outside world — what an engine measures, which server
it picks, what `systemctl` reports, how a control reads. A test suite
confirms the code does what it was written to do; only use confirms it was
written to do the right thing.

## Remaining checklist for a desktop machine

- [ ] Install the `.deb` on a clean Ubuntu 24.04 desktop, by double-clicking it.
- [ ] Launch from the application menu. Confirm the icon and name.
- [ ] Confirm the engine banner: with only `speedtest-cli`, it should be
      absent; after `sudo apt remove speedtest-cli`, the setup dialog should
      appear on launch.
- [ ] Press **Test Now**. One record appears with real numbers.
- [ ] Press **Test Now** twice in quick succession. The second is refused
      with "A test is already running" — no second record beyond a skipped one.
- [ ] Set the interval to 5 minutes, switch **Automatic testing** on. Note
      the "Next test" time.
- [ ] Close the window. Confirm the one-time notice appears.
- [ ] Wait 6 minutes. `systemctl --user list-timers bandwidth-logger-test.timer`.
- [ ] Reopen. The scheduled record is in the table.
- [ ] Disconnect Wi-Fi/Ethernet, press **Test Now**. A failed record appears
      with an error category and full timestamps.
- [ ] Reconnect. The next automatic test succeeds.
- [ ] Reboot. Log in. Wait one interval. Confirm a new record without ever
      opening the window.
- [ ] Change the interval to 30 minutes, press **Apply**. "Next test" moves
      to ~30 minutes out; `systemctl --user list-timers` agrees.
- [ ] Switch **Automatic testing** off. Wait past the interval. No new record.
- [ ] **Export CSV** → all records. Open in LibreOffice Calc: check the header
      row, that failed rows are present, that blank cells are blank rather
      than zero, and that no `external_ip` column appears.
- [ ] Export again with **Include external IP address** ticked. Confirm the
      column is present.
- [ ] Export a date range. Confirm only matching rows.
- [ ] Select a failed row, press Enter. Confirm the details window shows
      "Not measured" for unmeasured fields and the full error text.
- [ ] **Delete records** → date range. Confirm the count and span in the
      confirmation, then that only those rows went.
- [ ] Upgrade to a newer build. Records and settings survive.
- [ ] `sudo apt remove bandwidth-logger`. Confirm
      `~/.local/share/bandwidth-logger/` still exists.
- [ ] Tab through the window with the keyboard only. Every control reachable
      and labelled; nothing conveyed by colour alone.

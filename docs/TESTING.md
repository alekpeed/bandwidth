# Testing

```bash
sudo apt install python3-pytest
./packaging/run-tests.sh                  # everything
./packaging/run-tests.sh tests/unit       # a subset
PYTHON=python3.12 ./packaging/run-tests.sh
```

**No test contacts a speed-test server or consumes internet bandwidth.**
Every test runs against a temporary `BANDWIDTH_LOGGER_HOME`, so the real
database, diagnostic log and systemd units are never touched.

---

## How the doubles work

**The fake engine is a real program.** `tests/support/fake_engine.py` is
executed as an actual subprocess, printing recorded Ookla-shaped output. It
is not a mock, because the parts of the runner most worth testing exist only
because a subprocess is involved: timeouts, process-group termination,
non-zero exits, garbled output. Patching those away would test the wrong
thing.

Its behaviours: `success`, `sparse` (optional fields absent), `zeros`
(genuine zero measurements), `malformed` (exits 0, prints unusable text),
`failure`, `dns-failure`, `hang` (also spawns a sleeping child, so the suite
can prove the child is killed too).

**`systemctl` is stubbed**, not called. `tests/integration/test_scheduler.py`
records the exact command line the scheduler issues and answers `show`
queries. A container has no systemd user instance, and a real one would leave
units behind.

**The recorded fixtures** are the `SUCCESS_RESULT`, `SPARSE_RESULT` and
`ZERO_RESULT` payloads in `tests/support/fake_engine.py`, shared by the unit
and integration tests.

---

## Coverage against the specification

### Required unit tests

| Requirement | Where |
|---|---|
| Parse complete successful engine output | `test_result_parser.py::TestCompleteOutput` |
| Parse output with optional fields missing | `test_result_parser.py::TestOptionalFieldsMissing` |
| Reject malformed output and create a failed record | `test_result_parser.py::TestMalformedOutput`, `test_test_runner.py::TestFailures` |
| Convert bandwidth units correctly | `test_result_parser.py::TestUnitConversion` |
| Preserve zero separately from missing data | `test_result_parser.py::TestZeroIsNotMissing` |
| ISO 8601 timestamps across local offset changes | `test_timestamps.py` |
| Validate custom intervals, reject below five minutes | `test_intervals.py::TestValidation` |
| Correct CSV quoting and blank fields | `test_export_csv.py::TestQuoting`, `::TestMissingValues` |
| Exclude external IP when requested | `test_export_csv.py::TestExternalIpExclusion` |
| Run every migration from an empty database | `test_migrations.py::TestFromEmpty` |
| Upgrade an older database without losing rows | `test_migrations.py::TestUpgradePreservesData` |

### Required integration tests

| Requirement | Where |
|---|---|
| Successful subprocess result is stored once | `test_test_runner.py::TestSuccessfulRun` |
| Nonzero exit is stored as a failed attempt | `test_test_runner.py::TestFailures` |
| Timeout is stored and the process is terminated | `test_test_runner.py::TestTimeout` |
| Two simultaneous requests do not run concurrently | `test_test_runner.py::TestOverlapPrevention` |
| Scheduled and manual tests use the same runner | `test_test_runner.py::TestSharedCodePath` |
| Scheduler remains enabled after the interface exits | `test_cli_and_persistence.py::TestSchedulerSurvivesTheInterface` |
| Interval change updates the actual timer | `test_scheduler.py::TestIntervalChanges` |
| Disabled scheduler does not run | `test_scheduler.py::TestDisabling` |
| CSV export row count matches selected records | `test_export_csv.py::TestDatabaseExport`, `test_cli_and_persistence.py::TestExportCommand` |

### Beyond the required list

* **Overlap policy is proven, not assumed** — the skipped attempt is
  recorded with `skipped_overlap`, and the engine is confirmed to have been
  invoked exactly once.
* **The lock works across processes** — a second Python process checks it
  while a test is running, because in production the holder is the timer and
  the checker is the window.
* **Killing a test kills its children** — the fake engine spawns a sleeper;
  the test asserts that PID is gone.
* **A failed test exits 0** so the timer's unit is never marked failed and
  scheduling cannot be stopped by one bad test.
* **Disabling never touches the service unit**, only the timer, so a running
  test is not interrupted.
* **Migrations cannot become destructive** — the migrations module is read
  as source and the build fails if destructive SQL appears in it.
* **A failed save is surfaced** rather than swallowed.
* **An explicit engine choice is never silently substituted.**
* **Ookla identity is verified** — a `speedtest` that is really
  `speedtest-cli` is rejected rather than driven with the wrong arguments.
* **Every error category has short, plain wording** for the status area.
* **Commands are argument vectors** with no shell metacharacters.

### The GUI smoke test

`tests/integration/test_gui_smoke.py` builds the main window and every dialog
against a real GTK 4, in a subprocess, with a populated database. It asserts
the table filled, the nine columns exist, refresh and reordering work, and
that GTK emitted no `CRITICAL` or `Gtk-WARNING`.

It drives no clicks and takes no screenshots — it is not a substitute for
using the application. What it catches is the class of mistake unit tests
cannot: a widget method that does not exist, a signal with the wrong
signature, a property GTK rejects — any of which would crash the window on
launch while every other test passed.

It skips itself when GTK or a display is missing. `sudo apt install xvfb`
lets it run on a headless machine.

---

## Results

Run on Ubuntu 24.04.4 with Python 3.12.3, GTK 4.14.5, under Xvfb:

```
194 passed
```

Both `python3.11` and `python3.12` were used; the packaged application runs
under 3.12, Ubuntu 24.04's system Python.

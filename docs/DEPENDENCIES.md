# Third-party dependencies and licences

Bandwidth Logger is licensed **GPL-3.0-or-later**.

**The package bundles no third-party software.** Everything below is either
part of Ubuntu already, or installed separately by the user.

---

## Runtime dependencies

Declared in `packaging/debian/control` as `Depends`. All are in Ubuntu's own
archive; `apt` installs them with the application.

| Package | Provides | Licence |
|---|---|---|
| `python3` (≥ 3.11) | Language runtime | PSF-2.0 |
| `python3-gi` | PyGObject — the Python/GTK bridge | LGPL-2.1-or-later |
| `python3-gi-cairo` | Cairo integration for PyGObject | LGPL-2.1-or-later |
| `gir1.2-gtk-4.0` | GTK 4 introspection data | LGPL-2.1-or-later |
| `libgtk-4-1` | GTK 4 | LGPL-2.1-or-later |

`Recommends: systemd` — needed for background scheduling. Without it the
application still runs manual tests and says plainly that background
scheduling is unavailable.

SQLite arrives through Python's standard-library `sqlite3` module (SQLite
itself is public domain).

**No PyPI packages are required at runtime.** `pyproject.toml` lists an empty
`dependencies` list deliberately: the GUI stack comes from distribution
packages, not from pip, because mixing the two for PyGObject does not work
reliably.

---

## Build dependencies

| Package | Licence |
|---|---|
| `debhelper-compat (= 13)` | GPL-2+ |
| `dh-python` | GPL-2+ |
| `pybuild-plugin-pyproject` | GPL-2+ |
| `python3-all` | PSF-2.0 |
| `python3-setuptools` | MIT |

## Test dependencies

| Package | Licence | Notes |
|---|---|---|
| `python3-pytest` | MIT | |
| `xvfb` | MIT | Optional. Only for the GUI smoke test on a headless machine. |

---

## Speed-test engines

This is the licensing question that shapes how the application is delivered,
so it is set out in full.

### Ookla Speedtest CLI — preferred, **not** distributed here

* **Licence:** proprietary freeware, under Ookla's End User Licence Agreement
  and privacy policy.
* **Source:** Ookla's own apt repository at packagecloud.io.
* **Redistribution:** **not permitted.** The EULA licenses the software to the
  end user for personal use; it does not grant a third party the right to
  redistribute the binary or to include it in another package.

Consequences, and how the application respects them:

1. **The `.deb` does not bundle it**, and does not list it in `Depends` — a
   dependency would direct `apt` at a package that is not in Ubuntu's archive
   and would fail.
2. **The application detects it** and refuses to substitute anything else.
   When it is missing, each attempt is recorded as an `engine_missing`
   failure rather than measured with a less accurate tool.
3. **When it is absent, the application says whose software it is.** The
   setup screen states that it is made and licensed by Ookla, that its licence
   does not allow it to be included here, and that installing it is therefore
   a separate step. It shows the commands, offers to copy them and to open a
   terminal, and links to Ookla's site. It does not present the situation as
   a failed install of a missing component of this application.
4. **Licence acceptance is explicit.** The CLI normally prompts on first run
   for acceptance of the EULA and privacy notice. A prompt would hang a
   scheduled test that has no window attached, so `--accept-license
   --accept-gdpr` are passed on the command line. The user is shown both
   documents in the setup screen before any test runs — the acceptance is
   theirs, made knowingly, not concealed.

Installing it. Ookla's published instructions do **not** work unmodified on
Ubuntu 24.04; both departures below were found by running them and are
covered by `tests/unit/test_engines.py::test_the_install_commands_work_on_ubuntu_24_04`:

```bash
sudo apt-get remove -y speedtest-cli
curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh \
  | sudo env os=ubuntu dist=jammy bash
sudo apt-get install -y speedtest
```

* **`dist=jammy`** — Ookla publishes no repository for Ubuntu 24.04
  ("noble"); `.../ubuntu/dists/noble/Release` returns 404, so the upstream
  script reports the system as unsupported. The jammy build runs correctly on
  24.04.
* **Removing `speedtest-cli` first** — that package owns `/usr/bin/speedtest`,
  the path Ookla's package installs to, so `dpkg` refuses to unpack while it
  is present.

Verified end to end on Ubuntu 24.04: `speedtest --version` reports
*Speedtest by Ookla 1.2.0.84*.

> Speedtest® and Ookla® are trademarks of Ookla, LLC. This application is not
> affiliated with or endorsed by Ookla.

### speedtest-cli — removed in 1.1.0, and now conflicted with

Shipped as a `Depends` in 1.0.0 so the application worked immediately. Removed
for two reasons, both found in use rather than in review:

1. **It under-reports on fast connections.** It opens far fewer parallel
   connections than Ookla's client, so past roughly 300 Mbps it cannot
   saturate the link. On a gigabit line it reported about half the real
   figure. For an application built to diagnose slow connections, a number
   that is quietly wrong by half is the worst possible failure — it reads as
   evidence against your provider when it is an artefact of the tool.
2. **It made the accurate engine impossible to install.** Ubuntu's
   `speedtest-cli` package owns `/usr/bin/speedtest`, exactly where Ookla's
   package installs its binary, so `dpkg` refuses to unpack Ookla's engine
   while it is present. Depending on it meant this application installed the
   one thing blocking its own preferred engine.

The package now declares `Conflicts: speedtest-cli` and `Breaks:
speedtest-cli`, so installing Bandwidth Logger removes it rather than
tripping over it later.

Results measured by it are neither deleted nor rewritten. `engine_name`
identifies them, and the record details attach an explanation of why the
figure may be low.

### Which one ran

Every row records `engine_name` and `engine_version`. Results from different
engines are never silently mixed, and a history that spans a change of engine
shows exactly where the change happened.

### Adding another engine

Implement `SpeedTestEngine` in `src/bandwidth_logger/engines/`, add it to
`ENGINE_CLASSES` in `registry.py`, and add a parser to `result_parser.py`.
Nothing in the database, the scheduler or the interface needs to change —
which is the reason the adapter layer exists, and why removing the fallback
in 1.1.0 touched only these three places.

Before adding one, measure it against Ookla's engine on a fast connection.
The fallback removed in 1.1.0 parsed perfectly and reported half the real
speed; correctness of parsing is not correctness of measurement.

---

## What is not here

No analytics, no telemetry, no crash reporting, no update checker, no
network access of any kind beyond the speed test itself.

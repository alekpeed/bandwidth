# Database schema and migrations

SQLite, at `~/.local/share/bandwidth-logger/bandwidth-logger.sqlite3`.

Opened with:

| Pragma | Value | Why |
|---|---|---|
| `foreign_keys` | `ON` | Required by the specification. |
| `journal_mode` | `WAL` | The window can read while the timer writes. |
| `synchronous` | `FULL` | The history must survive a power cut. |
| `busy_timeout` | `30000` | Two processes write here; wait rather than fail. |

---

## `test_runs`

One row per **attempted** test — successful, failed, timed out, cancelled or
skipped. Nothing filters this table on status anywhere in the application.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | Primary key, autoincrement. |
| `trigger_type` | TEXT | no | `manual`, `scheduled`, `startup_recovery`, `retry`. |
| `scheduled_at_utc` | TEXT | yes | When the test was due. NULL for manual tests. |
| `started_at_utc` | TEXT | no | ISO 8601, UTC. |
| `started_at_local` | TEXT | no | ISO 8601 with the offset in force at that instant. |
| `completed_at_utc` | TEXT | yes | NULL if the attempt never completed. |
| `completed_at_local` | TEXT | yes | |
| `duration_ms` | INTEGER | yes | Measured on the monotonic clock. |
| `status` | TEXT | no | `success`, `failed`, `timeout`, `cancelled`, `skipped`. |
| `error_category` | TEXT | yes | See below. |
| `error_message` | TEXT | yes | The engine's complete text, never truncated for display. |
| `download_bps` | INTEGER | yes | Bits per second. |
| `upload_bps` | INTEGER | yes | Bits per second. |
| `idle_latency_ms` | REAL | yes | Ping. |
| `download_latency_ms` | REAL | yes | Only when the engine supplies it. |
| `upload_latency_ms` | REAL | yes | Only when the engine supplies it. |
| `jitter_ms` | REAL | yes | Only when the engine supplies it. |
| `packet_loss_percent` | REAL | yes | Only when the engine supplies it. |
| `server_id` | TEXT | yes | |
| `server_name` | TEXT | yes | |
| `server_host` | TEXT | yes | |
| `server_city` | TEXT | yes | |
| `server_region` | TEXT | yes | |
| `server_country` | TEXT | yes | |
| `isp_name` | TEXT | yes | |
| `external_ip` | TEXT | yes | Excluded from CSV export unless requested. |
| `interface_name` | TEXT | yes | e.g. `enp3s0`. |
| `connection_type` | TEXT | yes | e.g. `Ethernet`, `Wi-Fi`. NULL when undeterminable. |
| `engine_name` | TEXT | no | Which program produced this row. |
| `engine_version` | TEXT | yes | |
| `application_version` | TEXT | no | |
| `raw_result_json` | TEXT | yes | The engine's structured output, kept for diagnosis. |
| `created_at_utc` | TEXT | no | When the row was written. |

Indexes: `started_at_utc`, `status`, `trigger_type`.

### NULL versus zero

**A NULL means the value was not measured. A zero means it was measured as
zero.** These are different facts and the schema keeps them apart.

A connection that transferred nothing has `download_bps = 0`. An engine that
does not report packet loss has `packet_loss_percent = NULL`. Averaging a
column that conflated them would be wrong in a way nobody would notice.

`speedtest-cli` reports no jitter, no packet loss and no per-direction
latency; those columns are NULL for its rows.

### Error categories

`engine_missing`, `no_network`, `dns_failure`, `server_unavailable`,
`timeout`, `permission_error`, `malformed_output`, `process_error`,
`database_error`, `skipped_overlap`, `cancelled`, `unknown`.

The category is normalised so it is stable across engines and across engine
version changes. The engine's original wording is always kept in
`error_message`, and for failures the unparsed stdout/stderr is kept in
`raw_result_json`. The window shows one short sentence; the record keeps
everything.

---

## `throughput_samples`

One row per minute of observed traffic, written by the throughput monitor.
Added in schema version 2.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | Primary key. |
| `interface_name` | TEXT | no | The interface the traffic crossed. |
| `started_at_utc` | TEXT | no | ISO 8601, UTC. |
| `started_at_local` | TEXT | no | With the offset in force at that instant. |
| `ended_at_utc` | TEXT | no | |
| `duration_ms` | INTEGER | no | Measured, not assumed — a summary can be short. |
| `sample_count` | INTEGER | no | How many readings the summary covers. |
| `rx_bytes` / `tx_bytes` | INTEGER | no | Totals over the window. |
| `rx_bps_mean` / `tx_bps_mean` | REAL | no | Average bits per second. |
| `rx_bps_peak` / `tx_bps_peak` | REAL | no | Highest single reading. |
| `connection_type` | TEXT | yes | e.g. `Ethernet`, `Wi-Fi`. |
| `application_version` | TEXT | no | |
| `created_at_utc` | TEXT | no | |

Indexes: `started_at_utc`, `interface_name`.

**Peak is stored separately from mean** because averaging destroys the event
worth seeing. Ten seconds at full line rate inside an otherwise idle minute
leaves a mean that looks like nothing happened.

**This is the only table with a retention window.** Samples older than the
configured number of days (30 by default) are deleted. `test_runs` has no
retention limit and is never pruned — that guarantee is what the application
exists to provide.

### Concurrent usage on `test_runs`

Schema version 2 also adds `concurrent_rx_bps` and `concurrent_tx_bps` to
`test_runs`. They record how much traffic the link was already carrying when
a speed test began.

A test run over a busy connection measures the capacity that was *left*, so
without this a scheduled test that fired during a large download records a
low figure indistinguishable from a real fault. NULL when the monitor was not
running — which is different from zero, meaning the link was idle.

## `settings`

| Column | Type | Null |
|---|---|---|
| `key` | TEXT | no (primary key) |
| `value` | TEXT | no |
| `updated_at_utc` | TEXT | no |

| Key | Default | Meaning |
|---|---|---|
| `auto_testing_enabled` | `false` | Automatic testing on or off. |
| `interval_minutes` | `30` | Test interval. Minimum 5. |
| `run_scheduler_after_login` | `true` | Enable the user timer at login. |
| `export_include_external_ip` | `false` | Include the IP column in exports. |
| `table_sort_newest_first` | `true` | Table order. |
| `engine_timeout_seconds` | `180` | Clamped to 30–3600. |
| `preferred_engine` | `auto` | `auto` or `ookla`. |
| `preferred_server_id` | *(empty)* | Pinned test server; empty lets the engine choose. |
| `throughput_monitor_enabled` | `false` | Whether the monitor service runs. |
| `throughput_sample_seconds` | `2` | Sampling interval. Clamped to 1–60. |
| `throughput_retention_days` | `30` | How long samples are kept. |
| `close_behaviour_notice_shown` | `false` | The one-time close explanation. |

Settings live in the database rather than a separate file, so one backup of
the data directory carries the history and the configuration together.

New keys are added with `INSERT OR IGNORE` on every open, so an upgrade gains
new settings without overwriting choices the user has already made.

---

## `schema_migrations`

| Column | Type |
|---|---|
| `version` | INTEGER (primary key) |
| `applied_at_utc` | TEXT |

| Version | Description |
|---|---|
| 1 | Initial schema: `settings`, `test_runs`, indexes. |
| 2 | Throughput monitoring: `throughput_samples`, plus `concurrent_rx_bps` and `concurrent_tx_bps` on `test_runs`. |

---

## Migration rules

These are enforced, not merely intended.

**Append-only.** Once a version has shipped its SQL is frozen. Corrections
arrive as a new version. `MIGRATIONS` is an ordered list; nothing is ever
edited in place.

**Non-destructive.** No migration drops a table, drops a column, or deletes
rows. An upgrade must never cost the user history — that history is the whole
point of the application.
`tests/unit/test_migrations.py::TestMigrationDiscipline` reads the migrations
module and fails the build if `drop table`, `drop column`, `delete from
test_runs` or `truncate` ever appears in it.

**Atomic.** Each migration runs inside an explicit `BEGIN IMMEDIATE` and
either commits whole or rolls back whole. Two subtleties make this work:

* Python's `sqlite3` does **not** open an implicit transaction for DDL. Under
  the obvious `with connection:` idiom, a migration that created a table and
  then failed would leave the table behind while recording the migration as
  never applied — a half-applied schema that the next upgrade would trip
  over. SQLite's own DDL is transactional, so an explicit `BEGIN` fixes it.
* Migrations use individual `execute()` calls, never `executescript()`, which
  commits any open transaction before it runs and would silently undo the
  atomicity.

Both are covered by
`tests/unit/test_migrations.py::TestFailureHandling::test_a_failing_migration_leaves_the_previous_version_intact`.

**Automatic.** Migrations run on every open, in a single transaction each,
before the application touches any data.

---

## Adding a migration

1. Write a function taking a `sqlite3.Connection`. Use `execute()`, or
   `_run_statements()` for several statements. Never `executescript()`.
2. Append `(next_version, "short description", the_function)` to
   `MIGRATIONS`.
3. Add or extend a test that builds a database at the previous version with
   real rows in it, migrates, and asserts every row and setting survived.

Only add. If a column is wrong, add a corrected one and leave the old one in
place.

# systemd user units

These files are **reference copies**. The application does not install them
from here; it writes them to `~/.config/systemd/user/` at runtime, because the
timer interval is part of the unit text and only the running application knows
which interval the user chose. See
`src/bandwidth_logger/core/scheduler.py`.

They are kept in the repository so the generated units can be reviewed and
compared without having to run the application, and so a user who wants to
inspect what was installed in their home directory has something to check it
against.

`bandwidth-logger-test.timer` below shows a 30-minute interval; the generated
file substitutes whichever interval is configured.

`bandwidth-logger-monitor.service` is the continuous throughput monitor. It
is a long-running service rather than a timer, because it samples the
interface counters every couple of seconds. Unlike the test service it does
no network activity at all -- it only reads counters the kernel maintains
anyway.

Both units are **user** units. Nothing here runs as root, and neither file is
ever written outside the user's home directory.

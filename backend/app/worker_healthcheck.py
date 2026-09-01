"""Truthful Docker healthcheck for worker containers.

Workers run as ``sh -c "alembic upgrade head && exec python -m app.worker_main"``,
so container PID 1 is python once migrations finish. A process-name/pgrep based
healthcheck fails because the slim image ships no ``pgrep`` (exit 127 -> Docker
reports ``unhealthy`` for healthy workers). Instead this probes ``/proc/1/cmdline``:

- healthy  -> cmdline contains ``app.worker_main``
- unhealthy -> cmdline still shows ``alembic`` (startup in progress) or anything else

The pure decision lives in ``is_worker_healthy`` so it can be unit-tested; the
`/proc` I/O shell is verified by the live smoke (the container runs it).
"""

from __future__ import annotations

import sys


def is_worker_healthy(cmdline: str) -> bool:
    """Return True iff *cmdline* is a running worker process, not a migration."""
    if not cmdline:
        return False
    if "alembic" in cmdline:
        return False
    return "app.worker_main" in cmdline


def read_pid1_cmdline(path: str = "/proc/1/cmdline") -> str:
    """Read PID 1's command line, joining argv entries with single spaces."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.strip().replace("\x00", " ").strip()


def main() -> None:
    cmdline = read_pid1_cmdline()
    if is_worker_healthy(cmdline):
        print(f"worker_healthcheck: ok ({cmdline!r})")
        raise SystemExit(0)
    print(f"worker_healthcheck: unhealthy ({cmdline!r})", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()

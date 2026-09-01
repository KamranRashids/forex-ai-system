"""Unit tests: worker Docker healthcheck decision logic."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.worker_healthcheck import is_worker_healthy, read_pid1_cmdline


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ("python -m app.worker_main", True),
        ("/usr/local/bin/python -m app.worker_main", True),
        ("python -m app.worker_main --some-flag", True),
        ("sh -c alembic upgrade head && exec python -m app.worker_main", False),
        ("sh -c alembic upgrade head", False),
        ("/bin/bash", False),
        ("", False),
    ],
)
def test_is_worker_healthy(cmdline: str, expected: bool) -> None:
    assert is_worker_healthy(cmdline) is expected


def test_is_worker_healthy_is_case_sensitive_substring() -> None:
    # A genuinely different process must never be reported healthy.
    assert is_worker_healthy("python -m app.worker_healthcheck") is False


def test_read_pid1_cmdline_joins_argv(tmp_path: Path) -> None:
    probe = tmp_path / "cmdline"
    probe.write_bytes(b"python\x00-m\x00app.worker_main")
    assert read_pid1_cmdline(str(probe)) == "python -m app.worker_main"


def test_read_pid1_cmdline_missing_file(tmp_path: Path) -> None:
    assert read_pid1_cmdline(str(tmp_path / "nope")) == ""

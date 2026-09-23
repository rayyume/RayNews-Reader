import io
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from timestamp_filter import (
    _MAX_LINE_CHARS,
    _strip_existing_prefix,
    _truncate_line,
    format_timestamped_line,
    main,
)


ROOT = Path(__file__).resolve().parents[1]


def test_formats_iso_timestamp_with_aware_offset():
    now = datetime(2026, 8, 9, 20, 15, 32, tzinfo=timezone(timedelta(hours=8)))

    assert format_timestamped_line("web", "hello\n", now=now) == (
        "2026-08-09T20:15:32+08:00 [web] hello\n"
    )


def test_normalizes_naive_supplied_time_to_local_timezone():
    naive_now = datetime(2026, 8, 9, 20, 15, 32)
    expected_timestamp = naive_now.astimezone().isoformat(timespec="seconds")

    assert format_timestamped_line("web", "hello", now=naive_now) == (
        f"{expected_timestamp} [web] hello"
    )


def test_cli_prefixes_every_line_and_keeps_final_line_unterminated():
    result = subprocess.run(
        [sys.executable, "timestamp_filter.py", "web"],
        input="Traceback:\n  帧\nValueError: x",
        text=True,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "TZ": "Asia/Shanghai"},
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.endswith("ValueError: x")
    assert not result.stdout.endswith("\n")
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert all(
        re.match(
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00 \[web\] ", line
        )
        for line in lines
    )
    assert lines[1].endswith("帧")


@pytest.mark.parametrize("service", ["", "web service", "web!", "x" * 33])
def test_cli_rejects_invalid_service_names(service):
    result = subprocess.run(
        [sys.executable, "timestamp_filter.py", service],
        input="hello\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "service" in result.stderr.lower()


class BrokenPipeWriter(io.StringIO):
    def write(self, _text):
        raise BrokenPipeError


def test_main_silently_stops_on_broken_pipe():
    assert main(["web"], stdin=io.StringIO("hello\n"), stdout=BrokenPipeWriter()) == 0


def test_dockerfile_installs_timezone_data_and_copies_filter():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "DEBIAN_FRONTEND=noninteractive" in dockerfile
    assert "tzdata" in dockerfile
    assert "ENV TZ=Asia/Shanghai" in dockerfile
    assert _dockerfile_copies_root_module(dockerfile, "timestamp_filter.py")


def _dockerfile_copies_root_module(dockerfile: str, module: str) -> bool:
    """True when the module reaches the image, however the COPY is written.

    The Dockerfile copies root modules as a glob (`COPY *.py .`); asserting a
    literal filename would only re-test the COPY spelling, so accept either
    form and let the glob path be covered by test_dockerfile_copies_every_root_module.
    """
    import re

    if re.search(rf"COPY\s+\*\.py\s", dockerfile):
        return (ROOT / module).exists()
    return module in dockerfile


def test_main_tolerates_replacement_char_from_bad_bytes():
    out = io.StringIO()
    assert main(["web"], stdin=io.StringIO("bad byte \ufffd here\n"), stdout=out) == 0
    assert "\ufffd" in out.getvalue()


def test_main_tolerates_non_utf8_bytes_via_chunk_decoder():
    class _BinaryStdin:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)

        @property
        def buffer(self):
            return self._buf

    class _CapturingStdout(io.StringIO):
        pass

    out = _CapturingStdout()
    stdin = _BinaryStdin(b"before:\xff:after\n")
    assert main(["web"], stdin=stdin, stdout=out) == 0
    assert "before:" in out.getvalue()
    assert "after" in out.getvalue()


def test_truncate_line_caps_long_lines_and_keeps_newline():
    body = "x" * (_MAX_LINE_CHARS + 10)
    out = _truncate_line(body + "\n")

    assert out.endswith(" ... [line truncated]\n")
    assert out[:_MAX_LINE_CHARS] == "x" * _MAX_LINE_CHARS
    assert len(out) == _MAX_LINE_CHARS + len(" ... [line truncated]") + 1


def test_truncate_line_leaves_budget_sized_line_untouched():
    body = "y" * _MAX_LINE_CHARS
    assert _truncate_line(body + "\n") == body + "\n"
    assert _truncate_line(body) == body


@pytest.mark.parametrize(
    "line,expected",
    [
        ("2026-08-10 12:34:56,789 [refresh] hello\n", "hello\n"),
        ("2026-08-10T12:34:56+08:00 [refresh] hello\n", "hello\n"),
        ("2026-08-10 12:34:56,789 hello\n", "hello\n"),
        ("2026-08-10T12:34:56+08:00 hello\n", "hello\n"),
        ("2026-08-10T12:34:56Z [web] boom\n", "boom\n"),
        ("plain line\n", "plain line\n"),
        ("no newline 2026-08-10T12:34:56+08:00", "no newline 2026-08-10T12:34:56+08:00"),
    ],
)
def test_strip_existing_prefix(line, expected):
    assert _strip_existing_prefix(line) == expected


def test_main_applies_single_prefix_to_self_decorated_lines():
    out = io.StringIO()
    main(
        ["refresh"],
        stdin=io.StringIO(
            "2026-08-10 12:34:56,789 [refresh] doing thing\n"
            "2026-08-10T12:34:56+08:00 bare\n"
            "plain\n"
        ),
        stdout=out,
    )
    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert line.startswith("20") and re.match(
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d \[refresh\] ", line
        )
        # No doubled timestamp or tag survives.
        assert not re.search(r"\] 20\d\d-\d\d-\d\d", line)
    assert lines[0].endswith("[refresh] doing thing")
    assert lines[1].endswith("[refresh] bare")
    assert lines[2].endswith("[refresh] plain")

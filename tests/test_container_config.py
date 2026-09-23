import configparser
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "supervised_pipeline.py"


def _supervisor_config():
    # Supervisor treats whitespace-prefixed ';' and '#' as inline comments.
    # Keep that behavior here so nginx's `daemon off;` argument is verified
    # after INI parsing, not only as raw source text.
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read(ROOT / "supervisord.conf", encoding="utf-8")
    return config


def _extract_shell_function(path, name):
    source = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$",
        source,
    )
    assert match is not None, f"missing shell function: {name}"
    return match.group(0)


def _entrypoint_setup_script(fake_command_body):
    entrypoint_path = ROOT / "entrypoint.sh"
    entrypoint = entrypoint_path.read_text(encoding="utf-8")
    setup_start = entrypoint.index(
        "# Prepare the bind-mounted data directory before supervisor"
    )
    setup_end = entrypoint.index(
        "\n# ─── Configuration warning",
        setup_start,
    )
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    fakes = f"""
fake_setup_command() {{
  name="$1"
  shift
  {fake_command_body}
}}
install() {{ fake_setup_command install "$@"; }}
chown() {{ fake_setup_command chown "$@"; }}
function /usr/sbin/runuser {{ fake_setup_command runuser "$@"; }}
"""
    return functions + "\n\n" + fakes + "\n" + entrypoint[setup_start:setup_end]


def _injected_wrapper_command(filter_command, producer_command, *, grace=0.1):
    driver = (
        "import json, sys; "
        "from supervised_pipeline import main; "
        "raise SystemExit(main(sys.argv[2:], "
        "filter_command=json.loads(sys.argv[1]), "
        f"term_grace={grace!r}))"
    )
    return [
        sys.executable,
        "-u",
        "-c",
        driver,
        json.dumps([str(part) for part in filter_command]),
        "test",
        "--",
        *(str(part) for part in producer_command),
    ]


def _wait_for_path(path, process, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(
                f"wrapper exited before {path.name} appeared: {process.returncode}"
            )
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _pid_is_running(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except FileNotFoundError:
        return False
    return state != "Z"


def _wait_until_not_running(pid, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} is still running")


def test_supervised_service_commands_exec_pipeline_wrapper_with_exact_argv():
    config = _supervisor_config()
    expected_producers = {
        "program:refresh": ["python3", "-u", "/app/refresh_server.py"],
        "program:web": ["python3", "-u", "/app/web_server.py"],
        "program:nginx": ["nginx", "-g", "daemon off;"],
    }

    for section, producer in expected_producers.items():
        service = section.removeprefix("program:")
        command = config[section]["command"]
        outer_argv = shlex.split(command)
        assert outer_argv[:4] == ["/bin/bash", "-o", "pipefail", "-c"]
        assert len(outer_argv) == 5
        assert shlex.split(outer_argv[4]) == [
            "exec",
            "python3",
            "-u",
            "/app/supervised_pipeline.py",
            service,
            "--",
            *producer,
        ]


def test_supervised_service_process_groups_and_container_fds_are_preserved():
    config = _supervisor_config()

    for section in ("program:refresh", "program:web", "program:nginx"):
        program = config[section]
        assert program["stopasgroup"] == "true"
        assert program["killasgroup"] == "true"
        assert program["stdout_logfile"] == "/dev/fd/1"
        assert program["stdout_logfile_maxbytes"] == "0"
        assert program["stderr_logfile"] == "/dev/fd/2"
        assert program["stderr_logfile_maxbytes"] == "0"


def test_filter_failure_terminates_and_reaps_idle_term_ignoring_producer(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    failing_filter = tmp_path / "failing_filter.py"
    failing_filter.write_text(
        "import pathlib, sys, time\n"
        "pid_path = pathlib.Path(sys.argv[1])\n"
        "while not pid_path.exists(): time.sleep(0.01)\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = subprocess.run(
        _injected_wrapper_command(
            [sys.executable, "-u", failing_filter, producer_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    pid = int(producer_pid.read_text(encoding="utf-8"))
    assert result.returncode == 23
    assert time.monotonic() - started < 2
    assert not _pid_is_running(pid)


def test_wrapper_stays_alive_with_term_ignoring_members_until_group_kill(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    filter_pid = tmp_path / "filter.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    blocking_filter = tmp_path / "blocking_filter.py"
    blocking_filter.write_text(
        "import os, pathlib, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        _injected_wrapper_command(
            [sys.executable, "-u", blocking_filter, filter_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        start_new_session=True,
    )

    producer_process_id = filter_process_id = None
    try:
        _wait_for_path(producer_pid, process)
        _wait_for_path(filter_pid, process)
        producer_process_id = int(producer_pid.read_text(encoding="utf-8"))
        filter_process_id = int(filter_pid.read_text(encoding="utf-8"))

        process.send_signal(signal.SIGTERM)
        time.sleep(0.2)

        assert process.poll() is None
        assert _pid_is_running(producer_process_id)
        assert _pid_is_running(filter_process_id)

        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=3) == -signal.SIGKILL
        _wait_until_not_running(producer_process_id)
        _wait_until_not_running(filter_process_id)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        for pid in (producer_process_id, filter_process_id):
            if pid is not None and _pid_is_running(pid):
                os.kill(pid, signal.SIGKILL)


def test_wrapper_does_not_kill_remaining_member_after_peer_exits_on_term(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    filter_pid = tmp_path / "filter.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    cooperative_filter = tmp_path / "cooperative_filter.py"
    cooperative_filter.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        _injected_wrapper_command(
            [sys.executable, "-u", cooperative_filter, filter_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        start_new_session=True,
    )

    producer_process_id = None
    try:
        _wait_for_path(producer_pid, process)
        _wait_for_path(filter_pid, process)
        producer_process_id = int(producer_pid.read_text(encoding="utf-8"))

        process.send_signal(signal.SIGTERM)
        time.sleep(0.3)

        assert process.poll() is None
        assert _pid_is_running(producer_process_id)

        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=3) == -signal.SIGKILL
        _wait_until_not_running(producer_process_id)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        if producer_process_id is not None and _pid_is_running(producer_process_id):
            os.kill(producer_process_id, signal.SIGKILL)


def test_producer_nonzero_exit_wins_after_stdout_and_stderr_are_drained():
    producer = (
        "import sys; "
        "print('stdout line', flush=True); "
        "print('stderr line', file=sys.stderr, flush=True); "
        "raise SystemExit(7)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "web",
            "--",
            sys.executable,
            "-u",
            "-c",
            producer,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 7
    assert "[web] stdout line\n" in result.stdout
    assert "[web] stderr line\n" in result.stdout


def test_producer_receives_nginx_foreground_argument_as_one_argv_value():
    producer = "import json, sys; print(json.dumps(sys.argv[1:]), flush=True)"
    result = subprocess.run(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "nginx",
            "--",
            sys.executable,
            "-u",
            "-c",
            producer,
            "-g",
            "daemon off;",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0
    assert '[nginx] ["-g", "daemon off;"]\n' in result.stdout


def test_wrapper_returns_sigterm_after_reaping_cooperative_members(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "web",
            "--",
            sys.executable,
            "-u",
            producer,
            producer_pid,
        ],
        cwd=ROOT,
    )
    try:
        _wait_for_path(producer_pid, process)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == -signal.SIGTERM
        _wait_until_not_running(int(producer_pid.read_text(encoding="utf-8")))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_container_image_copies_pipeline_wrapper():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert _dockerfile_copies_root_module(dockerfile, "supervised_pipeline.py")


def _dockerfile_copies_root_module(dockerfile: str, module: str) -> bool:
    """True when the module reaches the image, however the COPY is written."""
    if re.search(r"COPY\s+\*\.py\s", dockerfile):
        return (ROOT / module).exists()
    return module in dockerfile


def test_dockerfile_copies_every_root_module():
    """Every root module must reach the image.

    A hardcoded COPY list silently dropped digest_engine.py, so web_server.py
    failed to import at startup and nginx answered /auth/* with 502 HTML.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"COPY\s+\*\.py\s", dockerfile), (
        "Dockerfile must copy root modules as a glob; a hardcoded list drops new modules"
    )
    # A .dockerignore listing *.py would silently undo the glob again.
    dockerignore = ROOT / ".dockerignore"
    if dockerignore.exists():
        excluded = {
            line.strip().removeprefix("/")
            for line in dockerignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert "*.py" not in excluded
        assert not {path.name for path in ROOT.glob("*.py")} & excluded


def test_nginx_logs_use_the_timestamp_filter_contract():
    nginx_conf = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    log_format = re.search(
        r"(?ms)^\s*log_format\s+raynews\s+(.+?);",
        nginx_conf,
    )

    assert log_format is not None
    assert log_format.start() < nginx_conf.index("server {")

    format_body = log_format.group(1)
    for variable in (
        "$remote_addr",
        "$request",
        "$status",
        "$body_bytes_sent",
        "$request_time",
        "$http_user_agent",
    ):
        assert variable in format_body
    assert "$time_local" not in format_body
    assert "$time_iso8601" not in format_body

    assert "access_log /dev/stdout raynews;" in nginx_conf
    assert "error_log /dev/stderr warn;" in nginx_conf


def test_compose_defaults_container_timezone_to_asia_shanghai():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "- TZ=${TZ:-Asia/Shanghai}" in compose


def test_entrypoint_log_uses_the_configured_timezone_offset():
    log_function = _extract_shell_function(ROOT / "entrypoint.sh", "log")

    for timezone, expected_offset in (
        ("UTC", timedelta(0)),
        ("Asia/Shanghai", timedelta(hours=8)),
    ):
        result = subprocess.run(
            ["bash", "-c", f'{log_function}\nlog "timezone test"'],
            cwd=ROOT,
            env={**os.environ, "TZ": timezone},
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        timestamp, prefix, message = result.stdout.rstrip("\n").split(" ", 2)
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed.utcoffset() == expected_offset
        assert prefix == "[entrypoint]"
        assert message == "timezone test"
        assert result.stderr == ""


def test_entrypoint_shell_messages_use_log_and_errors_stay_on_stderr():
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    shell_before_python = entrypoint.split("python3 - <<'PY'", 1)[0]
    shell_after_python = entrypoint.split("\nPY\n", 1)[1]
    shell_messages = shell_before_python + shell_after_python

    assert not re.search(r"(?m)^\s*echo\b", shell_messages)
    assert 'log "=== Injecting custom HTML ==="' in shell_messages
    assert re.search(r'(?m)^\s*log "\[entrypoint\] WARNING:', shell_messages) is None
    assert re.search(r'(?m)^\s*log "WARNING:', shell_messages)

    error_lines = [
        line.strip()
        for line in shell_messages.splitlines()
        if "ERROR:" in line
    ]
    assert error_lines
    assert all(line.startswith('log "ERROR:') for line in error_lines)
    assert all(line.endswith(">&2") for line in error_lines)


def test_html_injection_failure_timestamps_every_emitted_line(tmp_path):
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    injection_start = entrypoint.index('log "=== Injecting custom HTML ==="')
    injection_end = entrypoint.index(
        "\n\n# Prepare the bind-mounted data directory",
        injection_start,
    )
    injection_section = entrypoint[injection_start:injection_end]
    script = (
        _extract_shell_function(ROOT / "entrypoint.sh", "log")
        + "\n"
        + injection_section
    )
    missing_html = tmp_path / "missing" / "index.html"

    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "TZ": "UTC",
            "RAYNEWS_INJECT_HTML_PATH": str(missing_html),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    lines = [
        line
        for stream in (result.stdout, result.stderr)
        for line in stream.splitlines()
        if line
    ]
    assert lines
    timestamped = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 \[entrypoint\] .+$"
    )
    assert all(timestamped.fullmatch(line) for line in lines), lines
    assert "[inject] Custom HTML injection failed:" in result.stderr
    assert str(missing_html) in result.stderr
    assert "exit status 1" in result.stderr


@pytest.mark.parametrize(
    ("failed_command", "command_status", "summary"),
    [
        (
            "install",
            23,
            "ERROR: unable to create or set ownership on /app/data for raynews.",
        ),
        (
            "chown",
            37,
            "ERROR: unable to grant raynews ownership of /app/data.",
        ),
        (
            "runuser",
            49,
            "ERROR: /app/data is not writable by raynews after permission setup.",
        ),
    ],
)
def test_entrypoint_setup_failures_timestamp_multiline_command_diagnostics(
    failed_command,
    command_status,
    summary,
):
    script = _entrypoint_setup_script(
        """
printf '%s\n' "$name first diagnostic" "$name second diagnostic" >&2
printf '%s\n' "$name combined stdout diagnostic"
if [ "$name" = "$FAKE_FAILURE" ]; then
  return "$FAKE_STATUS"
fi
return 0
""".strip()
    )

    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "TZ": "UTC",
            "FAKE_FAILURE": failed_command,
            "FAKE_STATUS": str(command_status),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    lines = [line for line in result.stderr.splitlines() if line]
    timestamped = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 \[entrypoint\] .+$"
    )
    assert lines
    assert all(timestamped.fullmatch(line) for line in lines), lines
    assert [line.split("[entrypoint] ", 1)[1] for line in lines[:-1]] == [
        f"[setup:{failed_command}] {failed_command} first diagnostic",
        f"[setup:{failed_command}] {failed_command} second diagnostic",
        f"[setup:{failed_command}] {failed_command} combined stdout diagnostic",
    ]
    assert summary in lines[-1]


@pytest.mark.parametrize(
    ("setup_name", "command_status"),
    [("install", 23), ("chown", 37), ("runuser", 49)],
)
def test_setup_diagnostic_wrapper_preserves_the_command_status(
    setup_name,
    command_status,
):
    entrypoint_path = ROOT / "entrypoint.sh"
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    script = f"""{functions}
fake_failure() {{
  printf '%s\n' 'first diagnostic' 'second diagnostic' >&2
  return "$FAKE_STATUS"
}}
run_setup_command {setup_name} fake_failure
"""

    result = subprocess.run(
        ["bash", "-u", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC", "FAKE_STATUS": str(command_status)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == command_status
    assert result.stdout == ""
    assert f"[setup:{setup_name}] first diagnostic" in result.stderr
    assert f"[setup:{setup_name}] second diagnostic" in result.stderr


def test_entrypoint_setup_success_discards_captured_command_output():
    script = _entrypoint_setup_script(
        """
printf '%s\n' "$name successful detail one" "$name successful detail two" >&2
printf '%s\n' "$name successful stdout detail"
return 0
""".strip()
    )

    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_setup_wrapper_replays_retained_output_when_the_collector_fails():
    entrypoint_path = ROOT / "entrypoint.sh"
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    script = f"""{functions}
capture_setup_output() {{
  printf '%s\n' 'partial retained diagnostic'
  return 67
}}
successful_command() {{ return 0; }}
run_setup_command chown successful_command
"""

    result = subprocess.run(
        ["bash", "-u", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 67
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 2
    assert all(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 \[entrypoint\] .+",
            line,
        )
        for line in lines
    )
    assert lines[0].endswith("[setup:chown] partial retained diagnostic")
    assert lines[1].endswith(
        "ERROR: unable to capture diagnostic output for setup command 'chown'."
    )


def test_setup_failure_diagnostics_are_bounded_by_line_count_and_line_size():
    entrypoint_path = ROOT / "entrypoint.sh"
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    script = f"""{functions}
noisy_failure() {{
  python3 -c 'import sys; print("x" * 5000, file=sys.stderr); [print(f"line {{number}}", file=sys.stderr) for number in range(30)]'
  return 61
}}
run_setup_command install noisy_failure
"""

    result = subprocess.run(
        ["bash", "-u", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC"},
        text=True,
        capture_output=True,
        check=False,
    )

    setup_lines = [
        line for line in result.stderr.splitlines() if "[setup:install]" in line
    ]
    assert result.returncode == 61
    assert result.stdout == ""
    assert len(setup_lines) == 21
    assert max(map(len, setup_lines)) < 2200
    assert setup_lines[-1].endswith("diagnostic output truncated after 20 lines")


def test_setup_diagnostic_truncation_preserves_utf8_at_a_multibyte_boundary():
    entrypoint_path = ROOT / "entrypoint.sh"
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    script = f"""{functions}
unicode_failure() {{
  python3 -c 'import sys; sys.stderr.buffer.write(("中" * 683).encode("utf-8") + b"\\n")'
  return 61
}}
run_setup_command install unicode_failure
"""

    result = subprocess.run(
        ["bash", "-u", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        check=False,
    )

    stderr = result.stderr.decode("utf-8", errors="strict")
    lines = stderr.splitlines()
    marker = "... [line truncated]"
    assert result.returncode == 61
    assert result.stdout == b""
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 "
        r"\[entrypoint\] \[setup:install\] .+",
        lines[0],
    )
    payload = lines[0].split("[setup:install] ", 1)[1]
    assert payload == "中" * 682 + marker
    retained = payload.removesuffix(marker)
    assert len(retained.encode("utf-8")) <= 2048
    assert len(payload.encode("utf-8")) <= 2048 + len(marker.encode("ascii"))


def test_setup_diagnostic_replaces_internal_invalid_utf8_without_hiding_suffix():
    entrypoint_path = ROOT / "entrypoint.sh"
    functions = "\n\n".join(
        _extract_shell_function(entrypoint_path, name)
        for name in ("log", "capture_setup_output", "run_setup_command")
    )
    script = f"""{functions}
invalid_utf8_failure() {{
  python3 -c 'import sys; sys.stderr.buffer.write(b"before:\\xff:after\\n")'
  return 62
}}
run_setup_command runuser invalid_utf8_failure
"""

    result = subprocess.run(
        ["bash", "-u", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        check=False,
    )

    stderr = result.stderr.decode("utf-8", errors="strict")
    lines = stderr.splitlines()
    assert result.returncode == 62
    assert result.stdout == b""
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 "
        r"\[entrypoint\] \[setup:runuser\] before:\ufffd:after",
        lines[0],
    )

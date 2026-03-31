from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence


DEFAULT_RUNTIME_PYTHON = Path(r"C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run official CTP diagnostic first, then start the live paper runner if the diagnostic succeeds."
    )
    parser.add_argument("--gateway", choices=["CTP", "CTPTEST"], default="CTP")
    parser.add_argument("--diagnostic-timeout", type=int, default=15)
    parser.add_argument("--wait-until-local-time", default="")
    parser.add_argument("--all-diagnostic-candidates", action="store_true")
    parser.add_argument("--stop-existing-live-runners", action="store_true")
    return parser.parse_args(argv)


def resolve_runtime_python() -> Path:
    if DEFAULT_RUNTIME_PYTHON.exists():
        return DEFAULT_RUNTIME_PYTHON
    return Path(sys.executable).resolve()


def resolve_launch_time(time_text: str) -> datetime:
    parts = time_text.strip().split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("Use HH:MM or HH:MM:SS for --wait-until-local-time")

    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError("Use a valid 24-hour local time for --wait-until-local-time")

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def wait_until(target_time: datetime) -> None:
    while True:
        now = datetime.now()
        if now >= target_time:
            return

        remaining = target_time - now
        print(
            f"Waiting until {target_time:%Y-%m-%d %H:%M:%S} "
            f"(remaining {str(remaining).split('.', 1)[0]})",
            flush=True,
        )
        time.sleep(min(max(int(remaining.total_seconds()), 1), 60))


def get_latest_diagnostic_summary_path() -> Path:
    diagnostic_root = Path.home() / ".vntrader" / "diagnostics"
    if not diagnostic_root.exists():
        raise FileNotFoundError(f"Diagnostic root not found: {diagnostic_root}")

    candidates = sorted(
        [path for path in diagnostic_root.iterdir() if path.is_dir() and path.name.startswith("official_ctp_diag_")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No official diagnostic directory found under {diagnostic_root}")

    summary_path = candidates[0] / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Diagnostic summary not found: {summary_path}")
    return summary_path


def build_env(repo_root: Path, package_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(repo_root)])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_and_stream(command: list[str], cwd: Path, env: dict[str, str]) -> int:
    stream_encoding = env.get("PYTHONIOENCODING") or locale.getpreferredencoding(False)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=stream_encoding,
        errors="replace",
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")

    return process.wait()


def find_live_runner_pids() -> list[int]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'paper_trading_mvp\\.live_runner' } | Select-Object -ExpandProperty ProcessId",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def stop_live_runner_pids(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if pids:
        time.sleep(2)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    workspace_parent = repo_root.parent
    package_root = workspace_parent / "vnpy-4.3.0-packages"
    runtime_python = resolve_runtime_python()

    if not runtime_python.exists():
        raise FileNotFoundError(f"Python executable not found: {runtime_python}")
    if not package_root.exists():
        raise FileNotFoundError(f"Parallel package root not found: {package_root}")

    if args.wait_until_local_time:
        target_time = resolve_launch_time(args.wait_until_local_time)
        print(f"Delayed retest enabled; target local time is {target_time:%Y-%m-%d %H:%M:%S}")
        wait_until(target_time)

    existing_live_runner_pids = find_live_runner_pids()
    if existing_live_runner_pids:
        if args.stop_existing_live_runners:
            print(f"Stopping existing live runner PIDs: {existing_live_runner_pids}")
            stop_live_runner_pids(existing_live_runner_pids)
        else:
            print(f"Existing live runner PIDs detected: {existing_live_runner_pids}")
            print("Refusing to start another live runner. Re-run with --stop-existing-live-runners for a clean restart.")
            return 1

    env = build_env(repo_root, package_root)
    connect_filename = "connect_ctp.json" if args.gateway == "CTP" else "connect_ctptest.json"
    connect_file = Path.home() / ".vntrader" / connect_filename

    diagnostic_command = [
        str(runtime_python),
        str(repo_root / "examples" / "veighna_trader" / "diagnose_ctp_connect.py"),
        "--connect-file",
        str(connect_file),
        "--timeout",
        str(args.diagnostic_timeout),
    ]
    if args.all_diagnostic_candidates:
        diagnostic_command.append("--all-candidates")

    print(f"Gateway: {args.gateway} | DiagnosticTimeout: {args.diagnostic_timeout}")
    print("Running official diagnostic first...")
    diagnostic_exit_code = run_and_stream(diagnostic_command, workspace_parent, env)

    summary_path = get_latest_diagnostic_summary_path()
    with summary_path.open("r", encoding="utf-8") as handle:
        summary = json.load(handle)

    status = str(summary.get("status", ""))
    contract_count = int(summary.get("contract_count", 0) or 0)
    print(f"Diagnostic summary: {summary_path}")
    print(f"Diagnostic status={status} contract_count={contract_count}")

    if diagnostic_exit_code != 0 or status != "connected" or contract_count <= 0:
        print("Diagnostic did not reach usable contract metadata. Live runner will not start.")
        return 1

    if args.gateway == "CTP":
        config_path = repo_root / "examples" / "paper_trading_mvp" / "live_ctp_paper_config.json"
    else:
        config_path = repo_root / "examples" / "paper_trading_mvp" / "live_ctptest_paper_config.json"

    live_command = [str(runtime_python), "-m", "paper_trading_mvp.live_runner", str(config_path)]
    print("Diagnostic passed. Starting live runner...")
    return run_and_stream(live_command, workspace_parent, env)


if __name__ == "__main__":
    raise SystemExit(main())
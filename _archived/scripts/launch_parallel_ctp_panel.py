from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
PACKAGE_ROOT = REPO_ROOT.parent / "vnpy-4.3.0-packages"
DEFAULT_RUNTIME_PYTHON = Path(r"C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe")
PANEL_SCRIPT = REPO_ROOT / "scripts" / "run_parallel_ctp_panel.py"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"
CHECK_URL = f"{URL}/api/health"
STARTUP_TIMEOUT_SECONDS = 20.0


def panel_is_ready(url: str, timeout: float = 1.5) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 400
    except (OSError, URLError):
        return False


def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_panel_browser(url: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(url)
        return
    webbrowser.open(url)


def resolve_runtime_python() -> Path:
    if DEFAULT_RUNTIME_PYTHON.exists():
        return DEFAULT_RUNTIME_PYTHON
    return Path(sys.executable).resolve()


def launch_panel_server(runtime_python: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT), str(PACKAGE_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    command = [str(runtime_python), str(PANEL_SCRIPT), "--host", HOST, "--port", str(PORT)]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(command, cwd=str(REPO_ROOT), env=env, creationflags=creationflags)


def wait_for_panel(url: str, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if panel_is_ready(url):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    runtime_python = resolve_runtime_python()

    if not runtime_python.exists():
        print(f"Runtime python not found: {runtime_python}")
        return 1
    if not PANEL_SCRIPT.exists():
        print(f"Panel script not found: {PANEL_SCRIPT}")
        return 1

    if panel_is_ready(CHECK_URL):
        print(f"Panel already running at {URL}; opening browser.")
        open_panel_browser(URL)
        return 0

    if port_is_open(HOST, PORT):
        print(f"Port {PORT} is already open but the panel API is not responding. Check the existing process on that port.")
        return 1

    process = launch_panel_server(runtime_python)
    if wait_for_panel(CHECK_URL, STARTUP_TIMEOUT_SECONDS):
        print(f"Panel started successfully at {URL}.")
        open_panel_browser(URL)
        return 0

    if process.poll() is not None:
        print(f"Panel server exited early with code {process.returncode}.")
    else:
        print(f"Panel server did not become ready within {STARTUP_TIMEOUT_SECONDS:.0f} seconds.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
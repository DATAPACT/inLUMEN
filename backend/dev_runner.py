import os
import signal
import subprocess
import sys
import time
from pathlib import Path


WATCH_ROOT = Path(os.getenv("WATCH_ROOT", "/usr/inlumen")).resolve()
WATCH_EXTENSIONS = {".py", ".ini", ".sh"}
IGNORED_DIRS = {"__pycache__", "downloads", "state", ".pytest_cache"}
POLL_INTERVAL_SECONDS = float(os.getenv("WATCH_INTERVAL_SECONDS", "1.0"))


def iter_watched_files():
    for path in WATCH_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in WATCH_EXTENSIONS:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(WATCH_ROOT).parts[:-1]):
            continue
        yield path


def build_signature():
    signature = []
    for path in sorted(iter_watched_files()):
        stat = path.stat()
        signature.append((str(path.relative_to(WATCH_ROOT)), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def stop_process(process):
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start_process():
    print("[dev_runner.py] Starting inLUMEN gateway API...", flush=True)
    return subprocess.Popen(
        ["python", "inlumen_api.py"],
        cwd=WATCH_ROOT,
    )


def main():
    process = start_process()
    last_signature = build_signature()

    def handle_shutdown(signum, _frame):
        print(f"[dev_runner.py] Received signal {signum}, stopping...", flush=True)
        stop_process(process)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    while True:
        if process.poll() is not None:
            return process.returncode

        current_signature = build_signature()
        if current_signature != last_signature:
            print("[dev_runner.py] Source change detected, restarting services...", flush=True)
            stop_process(process)
            last_signature = current_signature
            process = start_process()

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())

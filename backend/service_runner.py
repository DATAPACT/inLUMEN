import signal
import subprocess
import time
from pathlib import Path


WORK_ROOT = Path(__file__).resolve().parent
SERVICE_COMMANDS = [
    ["python", "-u", "minio_api.py"],
    ["python", "-u", "neo4j_api.py"],
    ["python", "-u", "inlumen_api.py"],
]


def stop_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    processes = [
        subprocess.Popen(command, cwd=WORK_ROOT)
        for command in SERVICE_COMMANDS
    ]

    def handle_shutdown(signum, _frame):
        print(f"[service_runner.py] Received signal {signum}, stopping...", flush=True)
        stop_processes(processes)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    stop_processes(processes)
                    return return_code
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_processes(processes)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

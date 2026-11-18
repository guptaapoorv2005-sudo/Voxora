"""Run the API and LiveKit agent together on one Render web instance.

Render free web services can stop entirely when idle. Both children restart on
the next HTTP request; this is not an always-on background worker.
"""
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    port = os.environ.get("PORT", "10000")
    children = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port]),
        subprocess.Popen([sys.executable, "worker.py", "start"]),
    ]
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = 0
    try:
        while not stopping:
            for child in children:
                code = child.poll()
                if code is not None:
                    print(f"A Voxora process exited with status {code}; stopping service.", flush=True)
                    result = code if code else 1
                    stopping = True
                    break
            time.sleep(0.25)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return result


if __name__ == "__main__":
    raise SystemExit(main())

"""Запуск четырёх сервисов; Ctrl+C останавливает все дочерние процессы."""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    port = int(os.environ.get("DEMO_PORT", "8000"))
    for candidate in range(port, port + 4):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                sys.exit(f"Port {candidate} is busy. Close the previous app, or set DEMO_PORT.")
    processes, handles = [], []
    logs = Path(os.environ.get("DEMO_DATA_DIR", str(ROOT / "data"))) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        for service, offset in (("erp", 1), ("wms", 2), ("tms", 3), ("hub", 0)):
            handle = (logs / f"{service}.log").open("a", encoding="utf-8")
            handles.append(handle)
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", f"services.{service}:app",
                "--host", "127.0.0.1", "--port", str(port + offset), "--log-level", "warning"],
                cwd=ROOT, stdout=handle, stderr=handle))
        url = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(f"A service failed. See {logs}")
            try:
                for offset in range(4):
                    with opener.open(f"http://127.0.0.1:{port+offset}/health", timeout=.5) as r:
                        if r.status != 200:
                            raise RuntimeError("Health check failed")
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError(f"Startup timed out. See {logs}")
        print(f"\nLOGOS is ready: {url}\nKeep this terminal open. Press Ctrl+C to stop.\nLogs: {logs}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        while all(p.poll() is None for p in processes):
            time.sleep(.5)
        raise RuntimeError(f"A service stopped. See {logs}")
    except KeyboardInterrupt:
        print("\nStopping services...")
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()

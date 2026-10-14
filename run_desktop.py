#!/usr/bin/env python3
"""
Pantry Planner — Desktop launcher.

Starts the FastAPI backend (main:app) with uvicorn on port 8001 in a
background thread, waits until it answers, then opens the app in a native
pywebview window titled "Pantry Planner" (1280x860).

If pywebview is not installed, it falls back to opening the app in the
default web browser and keeps the server running until Ctrl+C.

Usage:
    python run_desktop.py
"""

import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8001
APP_URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"http://{HOST}:{PORT}/health"

# Make sure `main` is importable no matter where this is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def run_server() -> None:
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")


def wait_for_server(timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> int:
    server_thread = threading.Thread(target=run_server, daemon=True, name="uvicorn")
    server_thread.start()

    if not wait_for_server():
        print(f"⚠️  The server did not come up on {APP_URL} — check the log output above.")
        return 1

    try:
        import webview
    except ImportError:
        print("pywebview is not installed — opening Pantry Planner in your default browser.")
        print(f"Pantry Planner is running at {APP_URL}  (press Ctrl+C to stop)")
        webbrowser.open(APP_URL)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    webview.create_window("Pantry Planner", APP_URL, width=1280, height=860)
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

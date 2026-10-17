#!/usr/bin/env python3
"""
Pantry Planner — Desktop launcher.

Starts the FastAPI backend (main:app) with uvicorn on port 8001 in a
background thread, waits until it answers, then opens the app in a native
pywebview window titled "Pantry Planner" (1280x860).

The server listens on this PC only (127.0.0.1) unless the user has turned on
"phone access" in the app; then it listens on the home network (0.0.0.0) so the
phone app can reach it. Flipping that switch in the app restarts the server
in-process with the new setting (see ServerController). The choice is remembered
in pantry_desktop_config.json - see phone_access.py.

If pywebview is not installed, it falls back to opening the app in the
default web browser and keeps the server running until Ctrl+C.

Usage:
    python run_desktop.py
"""

import secrets
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"  # how THIS PC reaches its own server (window / health check)
PORT = 8001
APP_URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"http://{HOST}:{PORT}/health"

# Make sure `main` is importable no matter where this is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class ServerController:
    """Runs uvicorn in a background thread and can restart it on another host.

    Binding is decided by the phone-access setting: network only when it is on.
    Restarting is how the app applies that setting without the user having to
    close and reopen anything.
    """

    def __init__(self, app, port: int = PORT):
        self.app = app
        self.port = port
        self._server = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self, host: str) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=host,
            port=self.port,
            log_level="info",
            timeout_graceful_shutdown=3,  # don't let an idle connection block a restart
        )
        self._server = uvicorn.Server(config)
        # Let the app (and its UI) know how it is bound and what restart means.
        self.app.state.bind_host = host
        self.app.state.boot_id = secrets.token_hex(4)
        self.app.state.request_restart = self.restart_async
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="uvicorn")
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def restart(self, host: str) -> None:
        with self._lock:
            self.stop()
            self.start(host)

    def restart_async(self, host=None) -> None:
        """Restart shortly, in the background, so the HTTP reply that asked for it can finish."""
        import phone_access

        def _go():
            time.sleep(0.4)
            self.restart(host or phone_access.desired_bind_host())

        threading.Thread(target=_go, daemon=True, name="uvicorn-restart").start()


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
    import main as backend
    import phone_access

    controller = ServerController(backend.app, PORT)
    bind_host = phone_access.desired_bind_host()
    controller.start(bind_host)

    if not wait_for_server():
        print(f"⚠️  The server did not come up on {APP_URL} — check the log output above.")
        return 1

    if bind_host == phone_access.NETWORK_BIND:
        urls = [f"http://{ip}:{PORT}" for ip in phone_access.lan_ipv4_addresses()]
        print("📱 Phone access is ON — enter this address in the phone app:")
        for u in urls or ["(no home-network address found — is this PC on Wi-Fi?)"]:
            print(f"     {u}")
        print("   (Windows may ask you to allow Python through the firewall on first use.)")
    else:
        print("Phone access is off — only this PC can use the app. Turn it on under 'Connect Phone'.")

    try:
        import webview
    except ImportError:
        print("pywebview is not installed — opening Pantry Planner in your default browser.")
        print(f"Pantry Planner is running at {APP_URL}  (press Ctrl+C to stop)")
        webbrowser.open(APP_URL)
        try:
            while True:  # the server may be restarted by the app; keep the process alive
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    webview.create_window("Pantry Planner", APP_URL, width=1280, height=860)
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Phone access for the desktop server (Milestone 1 of the connected system).

The desktop app is the single hub for the shared database. A phone reaches it
over the home Wi-Fi ONLY when the user has turned on "phone access". This
module provides:

  * a tiny machine-local config file (NOT the shared database) that remembers
    - phone_access: None = never asked yet, True = on, False = off
    - access_code:  optional shared code the phone must send (X-Pantry-Token)
  * PhoneAccessMiddleware - gates every non-loopback request:
        phone access off  -> 403 for everything
        phone access on   -> allowed; if an access code is set, /api/* needs it
    Requests from this PC itself (loopback) are always allowed and never need
    the code, so the desktop window keeps working exactly as before.
  * a loopback-only router under /api/desktop/ to read/change the setting and
    to render the QR code for the phone address.

Safe by default: a missing or corrupt config means phone access is OFF.
The access code is a light household guard, not real security - traffic on the
home network is plain HTTP.
"""

import io
import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

DEFAULT_CONFIG = {"phone_access": None, "access_code": ""}

# Visible ASCII only (no spaces): the code travels in an HTTP header, and
# header values with other characters can be rejected by phone WebViews.
CODE_RE = re.compile(r"^[\x21-\x7E]{4,64}$")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
LOOPBACK_BIND = "127.0.0.1"
NETWORK_BIND = "0.0.0.0"


# ------------------------------------------------------------------
# Config file (machine-local, separate from pantry_planner.db)
# ------------------------------------------------------------------


def config_path() -> Path:
    override = os.environ.get("PANTRY_CONFIG_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "pantry_desktop_config.json"


def load_config() -> dict:
    """Read the config; any problem falls back to defaults (= phone access off)."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        if isinstance(data.get("phone_access"), bool):
            cfg["phone_access"] = data["phone_access"]
        code = data.get("access_code")
        # Honour any non-empty stored code as-is (never fail open on odd edits).
        if isinstance(code, str):
            cfg["access_code"] = code
    return cfg


def save_config(cfg: dict) -> None:
    """Atomic write so a crash can never leave a half-written config."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"phone_access": cfg.get("phone_access"), "access_code": cfg.get("access_code", "")},
                f,
                indent=2,
            )
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def desired_bind_host(cfg: Optional[dict] = None) -> str:
    """Where the server SHOULD listen: network only when phone access is on."""
    cfg = cfg if cfg is not None else load_config()
    return NETWORK_BIND if cfg.get("phone_access") is True else LOOPBACK_BIND


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = client.host or ""
    return host in LOOPBACK_HOSTS or host.startswith("::ffff:127.")


def lan_ipv4_addresses() -> List[str]:
    """This PC's home-network IPv4 addresses, best guess (primary first)."""
    found: List[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.3)
        # UDP "connect" only selects the outbound interface; no packet is sent.
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out: List[str] = []
    for ip in found:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip == "0.0.0.0":
            continue
        if ip not in out:
            out.append(ip)
    return out


def _port(request: Request) -> int:
    return request.url.port or 80


def _urls(request: Request) -> List[str]:
    port = _port(request)
    return [f"http://{ip}:{port}" for ip in lan_ipv4_addresses()]


def _restart_needed(state, cfg: dict) -> bool:
    bound = getattr(state, "bind_host", None)
    if bound is None:  # unknown (e.g. started by hand with plain uvicorn)
        return False
    return bound != desired_bind_host(cfg)


def _info(request: Request, cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else load_config()
    state = request.app.state
    bound = getattr(state, "bind_host", None)
    return {
        "phone_access": cfg["phone_access"],  # null until the user has been asked
        "listening_on_network": None if bound is None else bound != LOOPBACK_BIND,
        "restart_required": _restart_needed(state, cfg),
        "can_restart": callable(getattr(state, "request_restart", None)),
        "boot_id": getattr(state, "boot_id", None),
        "access_code": cfg["access_code"] or None,
        "port": _port(request),
        "urls": _urls(request),
    }


def _require_loopback(request: Request) -> None:
    if not is_loopback(request):
        raise HTTPException(status_code=403, detail="This setting can only be changed on the desktop.")


# ------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------


class PhoneAccessMiddleware(BaseHTTPMiddleware):
    """Gate for non-loopback clients. Add it BEFORE CORSMiddleware so the CORS
    layer wraps it and 401/403 replies still carry CORS headers (otherwise a
    phone app would see a bare network error instead of a readable message)."""

    async def dispatch(self, request: Request, call_next):
        if is_loopback(request):
            return await call_next(request)

        cfg = load_config()
        if cfg["phone_access"] is not True:
            return JSONResponse(
                {"detail": "Phone access is turned off in the Pantry Planner desktop app."},
                status_code=403,
            )

        path = request.url.path
        if path.startswith("/api/desktop/"):
            return JSONResponse(
                {"detail": "This setting can only be changed on the desktop."}, status_code=403
            )

        code = cfg["access_code"]
        if code and path.startswith("/api/"):
            supplied = request.headers.get("x-pantry-token", "")
            if not supplied:
                return JSONResponse(
                    {"detail": "This desktop requires an access code. Enter it in the app's server settings."},
                    status_code=401,
                )
            if not secrets.compare_digest(supplied.encode("utf-8"), code.encode("utf-8")):
                return JSONResponse({"detail": "Wrong access code."}, status_code=401)

        return await call_next(request)


# ------------------------------------------------------------------
# Loopback-only API (used by the desktop UI's "Connect Phone" page)
# ------------------------------------------------------------------

router = APIRouter(prefix="/api/desktop", tags=["desktop"])


class PhoneAccessIn(BaseModel):
    # Fields left out of the request are left unchanged.
    enabled: Optional[bool] = None
    access_code: Optional[str] = None  # "" or null clears the code


@router.get("/phone-access")
def get_phone_access(request: Request):
    _require_loopback(request)
    return _info(request)


@router.put("/phone-access")
def put_phone_access(body: PhoneAccessIn, request: Request):
    _require_loopback(request)
    cfg = load_config()
    fields = body.model_fields_set

    if "enabled" in fields and body.enabled is not None:
        cfg["phone_access"] = bool(body.enabled)

    if "access_code" in fields:
        code = (body.access_code or "").strip()
        if code and not CODE_RE.match(code):
            raise HTTPException(
                status_code=422,
                detail="The access code must be 4-64 visible characters with no spaces.",
            )
        cfg["access_code"] = code

    save_config(cfg)

    restarting = False
    restart = getattr(request.app.state, "request_restart", None)
    if callable(restart) and _restart_needed(request.app.state, cfg):
        restart()
        restarting = True

    info = _info(request, cfg)
    info["restarting"] = restarting
    return info


@router.get("/qr.svg")
def qr_svg(request: Request, url: str = Query(..., max_length=200)):
    _require_loopback(request)
    if url not in _urls(request):
        raise HTTPException(status_code=400, detail="Unknown address.")
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        raise HTTPException(
            status_code=501, detail="QR codes need the 'qrcode' package: pip install qrcode"
        )
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(
        buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
    )

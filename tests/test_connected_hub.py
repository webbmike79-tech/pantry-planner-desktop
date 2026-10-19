"""Milestone 1 tests: phone access gate, CORS, access code, QR, clear-purchased,
server restart. Run from the project root:  py -m unittest discover -s tests -v

Everything uses TEMPORARY files (PANTRY_DB_FILE / PANTRY_CONFIG_FILE) - no real data is touched.
A "phone" is simulated by giving the test client a non-loopback source address.
"""
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="pantry_m1_")
os.environ["PANTRY_DB_FILE"] = os.path.join(_TMP, "test.db")  # must be set BEFORE importing main
os.environ["PANTRY_CONFIG_FILE"] = os.path.join(_TMP, "cfg_initial.json")
os.environ.pop("GEMINI_API_KEY", None)

import main  # noqa: E402
import phone_access  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PHONE_ORIGIN = {"Origin": "http://localhost"}


def make_clients():
    local = TestClient(main.app, client=("127.0.0.1", 50000))
    phone = TestClient(main.app, client=("192.168.4.20", 50001))
    return local, phone


def shop_item(name, purchased):
    return {
        "item_name": name, "category": "Proteins", "amount_needed": "1",
        "numeric_quantity": 1.0, "unit": "count", "default_location": "Fridge",
        "inventory_category": "Protein", "reason": "t", "purchased": purchased,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._n = getattr(Base, "_n", 0) + 1
        Base._n = self._n
        os.environ["PANTRY_CONFIG_FILE"] = os.path.join(_TMP, f"cfg_{self._n}.json")
        self.local, self.phone = make_clients()
        main.app.state.bind_host = None
        for attr in ("request_restart", "boot_id"):
            if hasattr(main.app.state, attr):
                delattr(main.app.state, attr)

    def enable(self, code=None):
        body = {"enabled": True}
        if code is not None:
            body["access_code"] = code
        r = self.local.put("/api/desktop/phone-access", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class PhoneAccessGate(Base):
    def test_default_is_off_and_asks_once(self):
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["phone_access"], None)
        r = self.phone.get("/health")
        self.assertEqual(r.status_code, 403)
        self.assertIn("turned off", r.json()["detail"])
        self.assertEqual(self.phone.get("/api/settings").status_code, 403)
        self.assertEqual(self.phone.get("/").status_code, 403)

    def test_explicit_no_is_remembered_and_still_off(self):
        self.local.put("/api/desktop/phone-access", json={"enabled": False})
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["phone_access"], False)
        self.assertEqual(self.phone.get("/health").status_code, 403)

    def test_loopback_always_works_even_when_off(self):
        self.assertEqual(self.local.get("/api/settings").status_code, 200)
        self.assertEqual(self.local.get("/").status_code, 200)

    def test_on_without_code(self):
        self.enable()
        self.assertEqual(self.phone.get("/health").status_code, 200)
        self.assertEqual(self.phone.get("/api/settings").status_code, 200)
        self.assertEqual(self.phone.get("/").status_code, 200)

    def test_desktop_endpoints_are_never_reachable_from_phone(self):
        self.assertEqual(self.phone.get("/api/desktop/phone-access").status_code, 403)
        self.enable()
        self.assertEqual(self.phone.get("/api/desktop/phone-access").status_code, 403)
        self.assertEqual(self.phone.put("/api/desktop/phone-access", json={"enabled": False}).status_code, 403)
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["phone_access"], True)

    def test_corrupt_config_fails_closed(self):
        Path(os.environ["PANTRY_CONFIG_FILE"]).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.phone.get("/health").status_code, 403)
        self.assertIsNone(self.local.get("/api/desktop/phone-access").json()["phone_access"])


class AccessCode(Base):
    def test_code_required_for_api_but_not_health_or_static(self):
        self.enable(code="K7Q2XM")
        h = self.phone.get("/health").json()
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["app"], "pantry-planner")
        self.assertTrue(h["auth_required"])
        self.assertEqual(self.phone.get("/").status_code, 200)
        r = self.phone.get("/api/settings")
        self.assertEqual(r.status_code, 401)
        self.assertIn("access code", r.json()["detail"].lower())
        r = self.phone.get("/api/settings", headers={"X-Pantry-Token": "WRONG1"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], "Wrong access code.")
        self.assertEqual(self.phone.get("/api/settings", headers={"X-Pantry-Token": "K7Q2XM"}).status_code, 200)

    def test_loopback_never_needs_code(self):
        self.enable(code="K7Q2XM")
        self.assertEqual(self.local.get("/api/settings").status_code, 200)

    def test_no_code_means_auth_not_required(self):
        self.enable()
        self.assertFalse(self.phone.get("/health").json()["auth_required"])

    def test_set_clear_and_validation(self):
        self.enable(code="AB12")
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["access_code"], "AB12")
        for bad in ("abc", "has space", "x" * 65, "naïve1"):
            r = self.local.put("/api/desktop/phone-access", json={"access_code": bad})
            self.assertEqual(r.status_code, 422, bad)
        # a rejected change must not alter the stored code
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["access_code"], "AB12")
        # leaving access_code out leaves it unchanged; null clears it
        self.local.put("/api/desktop/phone-access", json={"enabled": True})
        self.assertEqual(self.local.get("/api/desktop/phone-access").json()["access_code"], "AB12")
        self.local.put("/api/desktop/phone-access", json={"access_code": None})
        self.assertIsNone(self.local.get("/api/desktop/phone-access").json()["access_code"])
        self.assertEqual(self.phone.get("/api/settings").status_code, 200)

    def test_config_persists_on_disk(self):
        self.enable(code="PERSIST9")
        data = json.loads(Path(os.environ["PANTRY_CONFIG_FILE"]).read_text(encoding="utf-8"))
        self.assertEqual(data.get("phone_access"), True)
        self.assertEqual(data.get("access_code"), "PERSIST9")
        self.assertTrue(phone_access.load_config()["phone_access"])


class Cors(Base):
    def test_preflight_allows_app_headers(self):
        self.enable(code="K7Q2XM")
        r = self.phone.options(
            "/api/settings",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-gemini-key,x-pantry-token,content-type",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "http://localhost")
        allowed = r.headers["access-control-allow-headers"].lower()
        for h in ("x-gemini-key", "x-pantry-token", "content-type"):
            self.assertIn(h, allowed)

    def test_error_replies_still_carry_cors_headers(self):
        # Otherwise a phone app would see a bare network error instead of a readable message.
        r = self.phone.get("/health", headers=PHONE_ORIGIN)  # phone access off -> 403
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://localhost")
        self.enable(code="K7Q2XM")
        r = self.phone.get("/api/settings", headers=PHONE_ORIGIN)  # no code -> 401
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://localhost")

    def test_unknown_origin_gets_no_cors_headers(self):
        self.enable()
        r = self.phone.get("/health", headers={"Origin": "http://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)

    def test_capacitor_origin_allowed(self):
        self.enable()
        r = self.phone.get("/health", headers={"Origin": "capacitor://localhost"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "capacitor://localhost")


class Qr(Base):
    def test_qr_for_listed_address_only_and_loopback_only(self):
        orig = phone_access.lan_ipv4_addresses
        phone_access.lan_ipv4_addresses = lambda: ["192.168.4.159"]
        try:
            info = self.enable()
            self.assertEqual(info["urls"], ["http://192.168.4.159:80"])
            url = info["urls"][0]
            r = self.local.get("/api/desktop/qr.svg", params={"url": url})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/svg+xml")
            self.assertIn(b"<svg", r.content)
            self.assertEqual(self.local.get("/api/desktop/qr.svg", params={"url": "http://evil.example"}).status_code, 400)
            self.assertEqual(self.phone.get("/api/desktop/qr.svg", params={"url": url}).status_code, 403)
        finally:
            phone_access.lan_ipv4_addresses = orig

    def test_lan_addresses_are_sane(self):
        for ip in phone_access.lan_ipv4_addresses():
            self.assertFalse(ip.startswith("127.") or ip.startswith("169.254."), ip)
            socket.inet_aton(ip)


class RestartSignalling(Base):
    def test_restart_requested_only_when_binding_must_change(self):
        calls = []
        main.app.state.request_restart = lambda: calls.append(1)
        main.app.state.bind_host = "127.0.0.1"
        r = self.local.put("/api/desktop/phone-access", json={"enabled": True}).json()
        self.assertTrue(r["restarting"])
        self.assertEqual(len(calls), 1)
        main.app.state.bind_host = "0.0.0.0"  # server now matches the setting
        r = self.local.put("/api/desktop/phone-access", json={"access_code": "NEWC0DE"}).json()
        self.assertFalse(r["restarting"])
        self.assertEqual(len(calls), 1)
        r = self.local.put("/api/desktop/phone-access", json={"enabled": False}).json()
        self.assertTrue(r["restarting"])
        self.assertEqual(len(calls), 2)

    def test_unknown_binding_never_restarts(self):
        main.app.state.request_restart = lambda: self.fail("must not restart")
        main.app.state.bind_host = None
        r = self.local.put("/api/desktop/phone-access", json={"enabled": True}).json()
        self.assertFalse(r["restarting"])
        self.assertFalse(r["restart_required"])

    def test_desired_bind_host(self):
        self.assertEqual(phone_access.desired_bind_host({"phone_access": None}), "127.0.0.1")
        self.assertEqual(phone_access.desired_bind_host({"phone_access": False}), "127.0.0.1")
        self.assertEqual(phone_access.desired_bind_host({"phone_access": True}), "0.0.0.0")


class ClearPurchased(Base):
    def test_clear_purchased(self):
        self.assertEqual(self.local.post("/api/shopping/clear-purchased").status_code, 404)  # no plan yet
        main.save_plan({"menu": [], "nutrition_notes": "", "shopping_list": [
            shop_item("a", True), shop_item("b", False), shop_item("c", True), shop_item("d", False)]})
        main.add_inventory_items("Fridge", [main.DetectedItem(item_name="x", category="Protein", quantity=1, unit="count")])
        inv_before = main.get_inventory()
        d = self.local.post("/api/shopping/clear-purchased").json()
        self.assertEqual(d["cleared"], 2)
        self.assertEqual([i["item_name"] for i in d["plan"]["shopping_list"]], ["b", "d"])
        self.assertEqual(main.get_inventory(), inv_before)
        self.assertEqual(self.local.post("/api/shopping/clear-purchased").json()["cleared"], 0)
        main.save_plan({"menu": [], "nutrition_notes": "", "shopping_list": [shop_item("z", True)]})
        d = self.local.post("/api/shopping/clear-purchased").json()
        self.assertEqual((d["cleared"], d["plan"]["shopping_list"]), (1, []))


class ExistingEndpointsUnchanged(Base):
    """Regression guard: the pre-existing API behaves as before for the desktop itself."""

    def test_health_keeps_status_ok(self):
        self.assertEqual(self.local.get("/health").json()["status"], "ok")

    def test_inventory_settings_and_errors(self):
        self.local.delete("/api/inventory?confirm_clear=true")
        r = self.local.post("/api/inventory", json={"location": "Fridge", "items": [
            {"item_name": "milk", "category": "Dairy", "quantity": 1, "unit": "gal"}]})
        self.assertEqual((r.status_code, r.json()["added"]), (200, 1))
        inv = self.local.get("/api/inventory").json()
        self.assertEqual([i["item_name"] for i in inv], ["milk"])
        inv[0]["quantity"] = 3
        out = self.local.put("/api/inventory", json=inv).json()["inventory"]
        self.assertEqual((out[0]["id"], out[0]["quantity"]), (inv[0]["id"], 3.0))  # row ids preserved
        s = self.local.get("/api/settings").json()
        s["base_headcount"] = 4
        self.assertEqual(self.local.put("/api/settings", json=s).json()["base_headcount"], 4)
        self.assertEqual(self.local.post("/api/inventory", json={"location": "Moon", "items": []}).status_code, 400)
        self.assertEqual(self.local.post("/api/validate-key").status_code, 400)
        self.assertEqual(self.local.post("/api/plan/generate", json={}).status_code, 400)  # no key
        self.assertIn(self.local.get("/api/plan/latest").status_code, (200,))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _status(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return None  # connection refused / unreachable


class RealServerRestart(Base):
    """Starts a REAL server and flips its binding, proving 'listens on the network only when enabled'."""

    def test_binding_follows_the_switch(self):
        import run_desktop

        ips = phone_access.lan_ipv4_addresses()
        if not ips:
            self.skipTest("no LAN address on this machine")
        lan = ips[0]
        self.enable()  # phone access ON in config, so a LAN-sourced request is allowed
        port = _free_port()
        ctl = run_desktop.ServerController(main.app, port)
        ctl.start("127.0.0.1")
        try:
            for _ in range(50):
                if _status(f"http://127.0.0.1:{port}/health"):
                    break
                import time; time.sleep(0.1)
            self.assertEqual(_status(f"http://127.0.0.1:{port}/health"), 200)
            self.assertIsNone(_status(f"http://{lan}:{port}/health"), "must NOT be reachable from the network yet")

            first_boot = main.app.state.boot_id
            ctl.restart("0.0.0.0")
            self.assertEqual(main.app.state.bind_host, "0.0.0.0")
            self.assertNotEqual(main.app.state.boot_id, first_boot)
            self.assertEqual(_status(f"http://{lan}:{port}/health"), 200, "reachable on the LAN address after enabling")
            self.assertEqual(_status(f"http://{lan}:{port}/api/desktop/phone-access"), 403)

            ctl.restart("127.0.0.1")
            self.assertIsNone(_status(f"http://{lan}:{port}/health"), "closed again after disabling")
            self.assertEqual(_status(f"http://127.0.0.1:{port}/health"), 200)
        finally:
            ctl.stop()


if __name__ == "__main__":
    unittest.main()

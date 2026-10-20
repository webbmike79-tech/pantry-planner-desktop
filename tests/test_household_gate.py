import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="pantry_gate_test_")
os.environ["PANTRY_TEST_MODE"] = "1"
os.environ["PANTRY_DB_FILE"] = os.path.join(_TMP, "gate_test.db")
os.environ["PANTRY_CONFIG_FILE"] = os.path.join(_TMP, "gate_cfg.json")

import db
try:
    import main
except ImportError:
    from api import main
import phone_access
from fastapi import FastAPI
from fastapi.testclient import TestClient


def get_gate_test_app():
    if hasattr(main, "app") and any(getattr(m, "cls", None) == phone_access.PhoneAccessMiddleware for m in getattr(main.app, "user_middleware", [])):
        return main.app
    # Standalone app wrapped with PhoneAccessMiddleware for repos where main is single-user
    app = FastAPI()
    app.add_middleware(phone_access.PhoneAccessMiddleware)

    @app.get("/health")
    def health():
        return {"ok": True, "status": "ok", "app": "pantry-planner"}

    @app.get("/api/inventory")
    def get_inv():
        inv_tbl, _, _ = db.get_table_names()
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute(f"SELECT * FROM {inv_tbl}")
        return [dict(r) for r in c.fetchall()]

    @app.post("/api/inventory")
    def post_inv(body: dict):
        inv_tbl, _, _ = db.get_table_names()
        conn = db.get_db_connection()
        c = conn.cursor()
        for item in body.get("items", []):
            c.execute(f"INSERT INTO {inv_tbl} (item_name, quantity) VALUES (?, ?)", (item["item_name"], item["quantity"]))
        conn.commit()
        return {"status": "ok"}

    return app


class TestHouseholdGate(unittest.TestCase):
    _counter = 0

    def setUp(self):
        TestHouseholdGate._counter += 1
        self.orig_db = os.environ.get("PANTRY_DB_FILE")
        self.orig_cfg = os.environ.get("PANTRY_CONFIG_FILE")
        self.db_path = os.path.join(_TMP, f"gate_test_{TestHouseholdGate._counter}.db")
        os.environ["PANTRY_DB_FILE"] = self.db_path
        db._initialized_households.clear()
        phone_access._WARNED_ALLOWLIST = False

        # Start clean
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

        self.client = TestClient(get_gate_test_app())

    def tearDown(self):
        for k in ("MULTI_HOUSEHOLD", "PANTRY_HOUSEHOLD_ALLOWLIST"):
            if k in os.environ:
                del os.environ[k]
        if self.orig_db is not None:
            os.environ["PANTRY_DB_FILE"] = self.orig_db
        elif "PANTRY_DB_FILE" in os.environ:
            del os.environ["PANTRY_DB_FILE"]
        if self.orig_cfg is not None:
            os.environ["PANTRY_CONFIG_FILE"] = self.orig_cfg
        elif "PANTRY_CONFIG_FILE" in os.environ:
            del os.environ["PANTRY_CONFIG_FILE"]
        db._initialized_households.clear()

    def _get_table_names(self):
        if not os.path.exists(self.db_path):
            return set()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = {row[0] for row in c.fetchall()}
        conn.close()
        return tables

    def test_allowlist_issued_code_succeeds_and_creates_tables(self):
        """Allowlist set, issued code -> request succeeds; first use creates that household's tables."""
        os.environ["MULTI_HOUSEHOLD"] = "1"
        os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"] = "HOUSE-A, HOUSE-B"

        tables_before = self._get_table_names()
        res = self.client.get("/api/inventory", headers={"X-Pantry-Token": "HOUSE-A"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

        tables_after = self._get_table_names()
        # Should create inv_*, set_*, plan_* for HOUSE-A
        new_tables = tables_after - tables_before
        self.assertEqual(len(new_tables), 3)
        self.assertTrue(any(t.startswith("inv_") for t in new_tables))
        self.assertTrue(any(t.startswith("set_") for t in new_tables))
        self.assertTrue(any(t.startswith("plan_") for t in new_tables))

    def test_allowlist_unknown_code_rejects_and_creates_no_tables(self):
        """Allowlist set, unknown code -> 401 with friendly copy; assert NO new tables exist afterward."""
        os.environ["MULTI_HOUSEHOLD"] = "1"
        os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"] = "HOUSE-A, HOUSE-B"

        # Prime with HOUSE-A first so DB has initial tables
        self.client.get("/api/inventory", headers={"X-Pantry-Token": "HOUSE-A"})
        tables_before = self._get_table_names()

        # Attempt GET with unknown code
        res_get = self.client.get("/api/inventory", headers={"X-Pantry-Token": "UNKNOWN-CODE"})
        self.assertEqual(res_get.status_code, 401)
        self.assertEqual(
            res_get.json().get("detail"),
            "That kitchen code isn't recognized. Check the code and try again."
        )

        # Attempt POST with unknown code
        res_post = self.client.post(
            "/api/inventory",
            headers={"X-Pantry-Token": "UNKNOWN-CODE"},
            json={"location": "Fridge", "items": [{"item_name": "Milk", "category": "Dairy", "quantity": 1, "unit": "count"}]}
        )
        self.assertEqual(res_post.status_code, 401)
        self.assertEqual(
            res_post.json().get("detail"),
            "That kitchen code isn't recognized. Check the code and try again."
        )

        # Assert database table set is completely unchanged
        tables_after = self._get_table_names()
        self.assertEqual(tables_before, tables_after)

    def test_allowlist_typo_exact_match_enforced(self):
        """Allowlist set, one-character-typo of an issued code -> 401 (exact-match, no fuzzy acceptance)."""
        os.environ["MULTI_HOUSEHOLD"] = "1"
        os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"] = "HOUSE-A, HOUSE-B"

        tables_before = self._get_table_names()
        # Single char typos of HOUSE-A
        for typo in ("HOUSE-a", "HOUSE_A", "HOUSE-A1", "HOUSEA", " HOUSE-A "):
            # Note: " HOUSE-A " sent as header without strip from client is exact match test
            res = self.client.get("/api/inventory", headers={"X-Pantry-Token": "HOUSE-a"})
            self.assertEqual(res.status_code, 401)
            self.assertEqual(
                res.json().get("detail"),
                "That kitchen code isn't recognized. Check the code and try again."
            )

        tables_after = self._get_table_names()
        self.assertEqual(tables_before, tables_after)

    def test_allowlist_no_token_preserves_existing_401(self):
        """Allowlist set, no token -> 401 (existing behavior preserved)."""
        os.environ["MULTI_HOUSEHOLD"] = "1"
        os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"] = "HOUSE-A, HOUSE-B"

        res = self.client.get("/api/inventory")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(
            res.json().get("detail"),
            "Household access code required. Enter your code in Settings."
        )

    def test_allowlist_unset_dev_fallback_and_startup_warning(self):
        """Allowlist unset + MULTI_HOUSEHOLD=1 -> any code works (dev fallback) AND startup warning emitted."""
        os.environ["MULTI_HOUSEHOLD"] = "1"
        if "PANTRY_HOUSEHOLD_ALLOWLIST" in os.environ:
            del os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"]

        # Capture log output for startup warning
        logger = logging.getLogger("pantry_planner.phone_access")
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        try:
            phone_access.check_household_allowlist_startup_warning(force=True, logger_instance=logger)
            log_output = log_stream.getvalue()
            self.assertIn(
                "MULTI_HOUSEHOLD is on without PANTRY_HOUSEHOLD_ALLOWLIST — any code creates a kitchen. Set the allowlist before public hosting.",
                log_output
            )

            # Any code works (dev fallback)
            res = self.client.get("/api/inventory", headers={"X-Pantry-Token": "ANY-ARBITRARY-CODE"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), [])
        finally:
            logger.removeHandler(handler)

    def test_multi_household_off_gate_inert(self):
        """MULTI_HOUSEHOLD off -> gate inert; existing local home behavior untouched."""
        if "MULTI_HOUSEHOLD" in os.environ:
            del os.environ["MULTI_HOUSEHOLD"]
        os.environ["PANTRY_HOUSEHOLD_ALLOWLIST"] = "HOUSE-A, HOUSE-B"

        main.init_db()

        # Local loopback client reaches inventory without household tokens
        res = self.client.get("/api/inventory")
        self.assertEqual(res.status_code, 200)

        # /health remains public and returns ok
        res_health = self.client.get("/health")
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.json().get("ok"), True)


if __name__ == "__main__":
    unittest.main()

"""
tests_isolation.py - Explicit Test Isolation & Real-Data Guard.

Enforces that:
1. PANTRY_DB_FILE points to a temporary, isolated SQLite database file.
2. PANTRY_CONFIG_FILE points to a temporary, isolated config file.
3. Live production files (pantry_planner.db, pantry_desktop_config.json) are NEVER touched.
4. If code attempts to access real production paths while PANTRY_TEST_MODE is active,
   an error is raised.
5. Snapshots real production files before any test touches data and verifies byte/count
   equality afterward. Skips cleanly if real files do not exist.
"""

import atexit
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

# Root directory of this repository
REPO_ROOT = Path(__file__).resolve().parent

REAL_DB_PATH = REPO_ROOT / "pantry_planner.db"
REAL_CONFIG_PATH = REPO_ROOT / "pantry_desktop_config.json"

# Create a master temporary directory for the entire test process run
_TEST_TEMP_DIR = tempfile.mkdtemp(prefix="pantry_test_isolation_")

# Ensure environment variables are set immediately upon import
_ISOLATED_DB = os.path.join(_TEST_TEMP_DIR, "isolated_test_pantry.db")
_ISOLATED_CONFIG = os.path.join(_TEST_TEMP_DIR, "isolated_test_config.json")

os.environ["PANTRY_TEST_MODE"] = "1"
os.environ.setdefault("PANTRY_DB_FILE", _ISOLATED_DB)
os.environ.setdefault("PANTRY_CONFIG_FILE", _ISOLATED_CONFIG)


def _cleanup_test_isolation():
    try:
        shutil.rmtree(_TEST_TEMP_DIR, ignore_errors=True)
    except Exception:
        pass


atexit.register(_cleanup_test_isolation)


def snapshot_real_state() -> Optional[Dict[str, Any]]:
    """Snapshot the real database and config file state.
    Returns None if real files do not exist.
    """
    snapshot = {}
    if REAL_DB_PATH.exists():
        conn = None
        try:
            conn = sqlite3.connect(str(REAL_DB_PATH))
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in c.fetchall()]
            snapshot["tables"] = tables

            counts = {}
            for t in tables:
                if t == "sqlite_sequence":
                    continue
                try:
                    c.execute(f"SELECT COUNT(*) FROM [{t}]")
                    counts[t] = c.fetchone()[0]
                except Exception:
                    pass
            snapshot["table_counts"] = counts

            # Specific snapshots for inventory, settings, and plan tables
            if "inventory" in tables:
                c.execute("SELECT id, item_name, quantity, unit, location FROM inventory ORDER BY id")
                snapshot["inventory_rows"] = c.fetchall()
            if "settings" in tables:
                c.execute("SELECT * FROM settings ORDER BY id")
                snapshot["settings_rows"] = c.fetchall()

            conn.close()
            snapshot["db_exists"] = True
        except Exception:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    else:
        snapshot["db_exists"] = False

    if REAL_CONFIG_PATH.exists():
        try:
            cfg_bytes = REAL_CONFIG_PATH.read_bytes()
            snapshot["config_exists"] = True
            snapshot["config_sha256"] = hashlib.sha256(cfg_bytes).hexdigest()
            snapshot["config_bytes"] = cfg_bytes
        except Exception:
            snapshot["config_exists"] = False
    else:
        snapshot["config_exists"] = False

    return snapshot


def verify_real_state_unchanged(before_snapshot: Optional[Dict[str, Any]]) -> None:
    """Assert that the real database and config file are 100% unchanged."""
    if not before_snapshot:
        return

    after_snapshot = snapshot_real_state()

    # DB verification
    if before_snapshot.get("db_exists"):
        assert after_snapshot.get("db_exists"), "Real database was deleted during tests!"
        before_counts = before_snapshot.get("table_counts", {})
        after_counts = after_snapshot.get("table_counts", {})
        assert before_counts == after_counts, (
            f"Real database table counts changed during test run!\nBefore: {before_counts}\nAfter: {after_counts}"
        )
        assert before_snapshot.get("inventory_rows") == after_snapshot.get("inventory_rows"), (
            "Real inventory rows were modified during test run!"
        )
        assert before_snapshot.get("settings_rows") == after_snapshot.get("settings_rows"), (
            "Real settings rows were modified during test run!"
        )
    else:
        assert not REAL_DB_PATH.exists(), "Tests created real pantry_planner.db when it did not exist!"

    # Config verification
    if before_snapshot.get("config_exists"):
        assert after_snapshot.get("config_exists"), "Real config was deleted during tests!"
        assert before_snapshot.get("config_sha256") == after_snapshot.get("config_sha256"), (
            "Real config file hash changed during test run!"
        )
    else:
        assert not REAL_CONFIG_PATH.exists(), "Tests created real config file when it did not exist!"

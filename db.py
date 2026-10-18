import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pantry_planner.db")

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = os.environ.get("PANTRY_DB_FILE", str(BASE_DIR / "pantry_planner.db"))

current_household_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_household", default="")

_initialized_households = set()


class LibsqlCursorAdapter:
    def __init__(self, client):
        self.client = client
        self.lastrowid = None
        self.rowcount = 0
        self._rows = []
        self._idx = 0
        self.description = None

    def execute(self, sql: str, params: Any = None):
        if params is None:
            p = []
        elif isinstance(params, (list, tuple)):
            p = list(params)
        elif isinstance(params, dict):
            p = params
        else:
            p = [params]
        res = self.client.execute(sql, p)
        self.lastrowid = getattr(res, "last_insert_rowid", None)
        self.rowcount = getattr(res, "rows_affected", 0)
        self._rows = list(res.rows)
        self._idx = 0
        if res.columns:
            self.description = [(col, None, None, None, None, None, None) for col in res.columns]
        else:
            self.description = None
        return self

    def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows

    def close(self):
        pass


class LibsqlClientAdapter:
    def __init__(self, client):
        self.client = client
        self.row_factory = None

    def cursor(self):
        return LibsqlCursorAdapter(self.client)

    def execute(self, sql: str, params: Any = None):
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self):
        pass

    def close(self):
        self.client.close()


def get_db_connection():
    """Connect to database: Turso/libSQL if DATABASE_URL is set, else local SQLite."""
    db_url = os.environ.get("DATABASE_URL", "").strip()
    auth_token = os.environ.get("DATABASE_AUTH_TOKEN", "").strip()

    if db_url:
        if db_url.startswith("libsql://") or db_url.startswith("https://") or db_url.startswith("http://") or db_url.startswith("wss://") or db_url.startswith("ws://"):
            try:
                import libsql_experimental as libsql
                conn = libsql.connect(database=db_url, auth_token=auth_token)
                return conn
            except ImportError:
                import libsql_client
                client = libsql_client.create_client_sync(url=db_url, auth_token=auth_token)
                return LibsqlClientAdapter(client)
        else:
            # File or local path
            clean_path = db_url[5:] if db_url.startswith("file:") else db_url
            conn = sqlite3.connect(clean_path)
            conn.row_factory = sqlite3.Row
            return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn


def is_multi_household() -> bool:
    return os.environ.get("MULTI_HOUSEHOLD", "").strip() in ("1", "true", "yes")


def get_table_names(household: Optional[str] = None) -> Tuple[str, str, str]:
    """Return (inventory_table, settings_table, saved_plans_table).
    If MULTI_HOUSEHOLD is unset: returns ('inventory', 'settings', 'saved_plans').
    If MULTI_HOUSEHOLD=1: returns tables scoped by household access code.
    """
    if not is_multi_household():
        return ("inventory", "settings", "saved_plans")

    code = (household or current_household_var.get() or "").strip()
    if not code:
        h_suffix = "default"
    else:
        h_suffix = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]

    return (f"inv_{h_suffix}", f"set_{h_suffix}", f"plan_{h_suffix}")


def init_db(household: Optional[str] = None, force: bool = False):
    """Ensure required tables and initial settings exist for the current or specified household."""
    inv_tbl, set_tbl, plan_tbl = get_table_names(household)
    key = (inv_tbl, set_tbl, plan_tbl)
    if key in _initialized_households and not force:
        return

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS {inv_tbl} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT,
            item_name TEXT,
            category TEXT,
            quantity REAL,
            unit TEXT,
            updated_at TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS {set_tbl} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            base_headcount INTEGER,
            dietary_tags TEXT,
            custom_diet_notes TEXT,
            essential_staples TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS {plan_tbl} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            plan_json TEXT
        )
    """)

    c.execute(f"SELECT COUNT(*) FROM {set_tbl}")
    row = c.fetchone()
    count = row[0] if row else 0
    if count == 0:
        default_tags = json.dumps(["Balanced & Whole Foods"])
        default_staples = "Olive oil, garlic powder, onion powder, black pepper, broth, fresh veggies, water"
        c.execute(f"""
            INSERT INTO {set_tbl} (id, base_headcount, dietary_tags, custom_diet_notes, essential_staples)
            VALUES (1, 2, ?, 'Provide wholesome meals tailored to any household restrictions entered below.', ?)
        """, (default_tags, default_staples))

    conn.commit()
    conn.close()
    _initialized_households.add(key)

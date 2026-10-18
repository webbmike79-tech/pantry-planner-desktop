"""
Pantry-to-Menu Planner — DESKTOP edition (FastAPI backend + desktop web UI).

This is the desktop sibling of the mobile API/PWA version in
~/workspace/projects/pantry-planner/api/. The database layer, Pydantic
schemas, Gemini prompts, business logic, and REST endpoints are identical
to the mobile backend (itself a faithful port of the original Streamlit
app.py, including the 2026-10-14 fixes: inventory IDs are preserved on bulk
save, and all Gemini calls go through _generate_structured, which raises
friendly RuntimeErrors instead of raw tracebacks).

This desktop app is the HUB of the connected system: its SQLite database
(pantry_planner.db in THIS project's root) is the one shared copy. The phone app
connects to this server over the home Wi-Fi - but only while "phone access" is
turned on (see phone_access.py; off by default, optional access code). Set the
PANTRY_DB_FILE environment variable to override the DB location (useful for
testing) and PANTRY_CONFIG_FILE to override the phone-access config location.

Run it (from this project's root):
    python run_desktop.py            # native window (pywebview) or browser
    uvicorn main:app --port 8001     # server only → http://127.0.0.1:8001
"""

import io
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from PIL import Image
from pydantic import BaseModel, Field

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile

from google import genai
from google.genai import types

from ai_fallback import (
    ProviderKeys,
    extract_provider_keys,
    generate_structured_with_fallback,
    validate_provider_key,
)

import phone_access

# ==========================================
# PATHS
# ==========================================

BASE_DIR = Path(__file__).resolve().parent  # this project's root (main.py lives here)
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_FILE = os.environ.get("PANTRY_DB_FILE", str(BASE_DIR / "pantry_planner.db"))

# Theme names (from app.py's THEMES). The PWA ships with the current season's
# palette baked into CSS; the backend only needs the name for the AI prompt.
THEME_NAMES = [
    "🎃 Halloween / October",
    "🦃 Thanksgiving / November",
    "🎄 Christmas / December",
    "❄️ Winter Frost (Jan–Feb)",
    "🌱 Spring Garden (Mar–May)",
    "☀️ Summer BBQ (Jun–Sep)",
]

LOCATIONS = ["Fridge", "Freezer", "Cupboard / Pantry"]


def get_auto_theme_name() -> str:
    month = datetime.now().month
    if month == 10:
        return "🎃 Halloween / October"
    elif month == 11:
        return "🦃 Thanksgiving / November"
    elif month == 12:
        return "🎄 Christmas / December"
    elif month in (1, 2):
        return "❄️ Winter Frost (Jan–Feb)"
    elif month in (3, 4, 5):
        return "🌱 Spring Garden (Mar–May)"
    else:
        return "☀️ Summer BBQ (Jun–Sep)"


# ==========================================
# PYDANTIC SCHEMAS (ported from app.py)
# app.py names: DetectedItem / VisionInventoryResult / DayMealPlan /
# ReceiptLineItem correspond to the scan / day-plan / receipt schemas.
# ==========================================


class DetectedItem(BaseModel):
    item_name: str = Field(description="Clear name of the food, drink, or seasoning")
    category: str = Field(
        description="One of: Produce/Veggies, Protein, Dairy, Seasoning/Spice, Drink/Beverage, Pantry/Dry, Frozen"
    )
    quantity: float = Field(description="Numeric estimate of the amount visible")
    unit: str = Field(description="Unit of measure, e.g., oz, lbs, count, bottle, bag, can")


class VisionInventoryResult(BaseModel):
    items: List[DetectedItem]


class UsedIngredient(BaseModel):
    inventory_id: int = Field(
        description="The exact 'id' from CURRENT KITCHEN INVENTORY (use -1 if not in inventory)"
    )
    item_name: str = Field(description="Name of the inventory item used")
    amount_used: float = Field(
        description="Numeric quantity used for this day's meals, in the same unit as the inventory item"
    )
    unit: str = Field(description="Unit of measure matching the inventory item")


class MealDetail(BaseModel):
    dish_name: str = Field(description="Name of the recipe")
    prep_steps: List[str] = Field(
        description="Step-by-step cooking instructions (include air fryer, oven, or stovetop guidance)"
    )
    cook_time_minutes: int = Field(description="Estimated cooking time in minutes")
    changed_from: Optional[str] = Field(
        default=None, description="Name of the previous meal if this meal slot was swapped"
    )


class DayMealPlan(BaseModel):
    day: str = Field(description="Day of the week (e.g., Monday)")
    headcount: int = Field(description="Number of people being fed this day")
    breakfast: MealDetail
    lunch: MealDetail
    dinner: MealDetail
    ingredients_to_deduct: List[UsedIngredient] = Field(
        description="Exact inventory items and numeric amounts used on this day"
    )
    cooked: bool = Field(
        default=False, description="Always initialize as False when generating a plan"
    )


class ShoppingListItem(BaseModel):
    item_name: str = Field(description="Clean name of the grocery item to buy")
    category: str = Field(
        description="One of: Veggies & Produce, Proteins, Seasonings & Spices, Drinks & Beverages, Pantry & Dairy"
    )
    amount_needed: str = Field(description="Human-readable amount to buy, e.g., '2 lbs' or '1 bottle'")
    numeric_quantity: float = Field(
        description="Numeric amount to add to inventory when purchased, e.g., 2.0"
    )
    unit: str = Field(description="Unit of measure, e.g., lbs, oz, count, bottle, bag, can")
    default_location: str = Field(
        description="Where this item belongs when bought: 'Fridge', 'Freezer', or 'Cupboard / Pantry'"
    )
    inventory_category: str = Field(
        description="One of: Produce/Veggies, Protein, Dairy, Seasoning/Spice, Drink/Beverage, Pantry/Dry, Frozen"
    )
    reason: str = Field(description="Which meal(s) or essential restock this covers")
    purchased: bool = Field(default=False, description="Always initialize as False")


class WeeklyPlanOutput(BaseModel):
    menu: List[DayMealPlan]
    shopping_list: List[ShoppingListItem]
    nutrition_notes: str = Field(
        description="Brief summary of how this menu strictly complies with the household's custom dietary restrictions"
    )


class MealSwapOutput(BaseModel):
    found: bool = Field(
        description="True if an on-hand recipe satisfying all rules was found, False otherwise"
    )
    dish_name: str = Field(
        default="", description="Name of the replacement meal. Empty if found is False"
    )
    prep_steps: List[str] = Field(
        default_factory=list, description="Step-by-step preparation steps"
    )
    cook_time_minutes: int = Field(
        default=0, description="Estimated cooking time in minutes"
    )
    ingredients_used: List[UsedIngredient] = Field(
        default_factory=list,
        description="Ingredients deducted from remaining on-hand inventory for this replacement meal",
    )
    reason_if_not_found: str = Field(
        default="",
        description="Brief plain explanation if no on-hand recipe satisfies the constraints, e.g. 'Nothing else on hand fits this slot'",
    )


class ReceiptLineItem(BaseModel):
    item_name: str = Field(
        description="Decoded, human-readable food/drink/seasoning name from the receipt"
    )
    quantity: float = Field(description="Numeric quantity purchased (e.g., 1.0, 2.5, 12.0)")
    unit: str = Field(description="Unit of measure, e.g., lbs, oz, count, bag, bottle, can")
    default_location: str = Field(
        description="Best storage spot: 'Fridge', 'Freezer', or 'Cupboard / Pantry'"
    )
    inventory_category: str = Field(
        description="One of: Produce/Veggies, Protein, Dairy, Seasoning/Spice, Drink/Beverage, Pantry/Dry, Frozen"
    )
    matched_shopping_list_index: int = Field(
        description="Exact 'index' from the provided shopping list if this item fulfills it; otherwise -1"
    )


class ReceiptScanResult(BaseModel):
    store_name: str = Field(description="Store name if visible on receipt, otherwise 'Grocery Store'")
    items: List[ReceiptLineItem]


# ==========================================
# SQLITE DATABASE LAYER (ported from app.py)
# ==========================================


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT,
            item_name TEXT,
            category TEXT,
            quantity REAL,
            unit TEXT,
            updated_at TEXT
        )
    """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            base_headcount INTEGER,
            dietary_tags TEXT,
            custom_diet_notes TEXT,
            essential_staples TEXT
        )
    """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            plan_json TEXT
        )
    """
    )
    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_tags = json.dumps(["Balanced & Whole Foods"])
        default_staples = (
            "Olive oil, garlic powder, onion powder, black pepper, broth, fresh veggies, water"
        )
        c.execute(
            """
            INSERT INTO settings (id, base_headcount, dietary_tags, custom_diet_notes, essential_staples)
            VALUES (1, 2, ?, 'Provide wholesome meals tailored to any household restrictions entered below.', ?)
        """,
            (default_tags, default_staples),
        )
    conn.commit()
    conn.close()


def add_inventory_items(location: str, items: List[DetectedItem]):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for item in items:
        c.execute(
            """
            INSERT INTO inventory (location, item_name, category, quantity, unit, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (location, item.item_name, item.category, item.quantity, item.unit, now),
        )
    conn.commit()
    conn.close()


def get_inventory_df() -> pd.DataFrame:
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM inventory ORDER BY location, category, item_name", conn)
    conn.close()
    return df


def get_inventory() -> List[dict]:
    """Inventory as a list of plain dicts (JSON-friendly types)."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM inventory ORDER BY location, category, item_name"
    ).fetchall()
    conn.close()
    return [
        {
            "id": int(r["id"]),
            "location": r["location"],
            "item_name": r["item_name"],
            "category": r["category"],
            "quantity": float(r["quantity"]) if r["quantity"] is not None else 0.0,
            "unit": r["unit"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def clear_inventory():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM inventory")
    conn.commit()
    conn.close()


def replace_inventory_from_df(df: pd.DataFrame):
    # FIX (2026-10-14, from app.py): preserve existing row IDs on save. The
    # original version deleted all rows and re-inserted without IDs, so every
    # save renumbered the inventory and broke the inventory_id references
    # stored in saved meal plans. Rows carrying a valid existing id keep it;
    # brand-new rows (id empty/NaN) get a fresh id.
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM inventory")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    seen_ids = set()
    for _, row in df.iterrows():
        raw_id = row.get("id")
        keep_id = None
        if raw_id is not None and not (isinstance(raw_id, float) and pd.isna(raw_id)):
            try:
                candidate = int(raw_id)
                if candidate > 0 and candidate not in seen_ids:
                    keep_id = candidate
                    seen_ids.add(candidate)
            except (TypeError, ValueError):
                keep_id = None
        values = (
            row.get("location", "Cupboard / Pantry"),
            row.get("item_name", ""),
            row.get("category", "Pantry/Dry"),
            float(row.get("quantity", 0.0)),
            row.get("unit", "count"),
            row.get("updated_at") or now,
        )
        if keep_id is not None:
            c.execute(
                """
                INSERT INTO inventory (id, location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (keep_id,) + values,
            )
        else:
            c.execute(
                """
                INSERT INTO inventory (location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                values,
            )
    conn.commit()
    conn.close()



def deduct_day_from_inventory(plan_id: int, day_idx: int) -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT plan_json FROM saved_plans WHERE id = ?", (plan_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return []

    plan_data = json.loads(row[0])
    day_obj = plan_data["menu"][day_idx]

    if day_obj.get("cooked", False):
        conn.close()
        return []

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    deducted_summary = []

    for ing in day_obj.get("ingredients_to_deduct", []):
        inv_id = ing.get("inventory_id", -1)
        amt = float(ing.get("amount_used", 0.0))
        name = ing.get("item_name", "")
        unit = ing.get("unit", "")

        if amt <= 0:
            continue

        c.execute("SELECT id, quantity FROM inventory WHERE id = ?", (inv_id,))
        target = c.fetchone()
        if not target:
            c.execute(
                "SELECT id, quantity FROM inventory WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
                (name,),
            )
            target = c.fetchone()

        if target:
            target_id, current_qty = target
            new_qty = max(0.0, round(float(current_qty) - amt, 2))
            c.execute(
                "UPDATE inventory SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, target_id),
            )
            deducted_summary.append(f"{name}: -{amt} {unit} (now {new_qty} {unit})")

    plan_data["menu"][day_idx]["cooked"] = True
    c.execute(
        "UPDATE saved_plans SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_data), plan_id),
    )
    conn.commit()
    conn.close()
    return deducted_summary


def restock_checked_shopping_items(plan_id: int, item_indices: List[int]) -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT plan_json FROM saved_plans WHERE id = ?", (plan_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return []

    plan_data = json.loads(row[0])
    shopping_list = plan_data.get("shopping_list", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    restocked_log = []

    for idx in item_indices:
        if idx < 0 or idx >= len(shopping_list):
            continue
        item = shopping_list[idx]
        if item.get("purchased", False):
            continue

        name = item.get("item_name", "Unknown Item")
        qty = item.get("numeric_quantity")
        if qty is None:
            match = re.search(r"(\d+(\.\d+)?)", str(item.get("amount_needed", "1")))
            qty = float(match.group(1)) if match else 1.0
        else:
            qty = float(qty)

        unit = item.get("unit") or "count"
        loc = item.get("default_location") or "Cupboard / Pantry"
        inv_cat = item.get("inventory_category") or "Pantry/Dry"

        c.execute(
            "SELECT id, quantity FROM inventory WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
            (name,),
        )
        existing = c.fetchone()

        if existing:
            ex_id, ex_qty = existing
            new_qty = round(float(ex_qty) + qty, 2)
            c.execute(
                "UPDATE inventory SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, ex_id),
            )
            restocked_log.append(f"Updated {name}: +{qty} {unit} (Total: {new_qty} {unit})")
        else:
            c.execute(
                """
                INSERT INTO inventory (location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (loc, name, inv_cat, qty, unit, now),
            )
            restocked_log.append(f"Added {name} ({qty} {unit}) to {loc}")

        shopping_list[idx]["purchased"] = True

    plan_data["shopping_list"] = shopping_list
    c.execute(
        "UPDATE saved_plans SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_data), plan_id),
    )
    conn.commit()
    conn.close()
    return restocked_log


def clear_purchased_shopping_items(plan_id: int) -> int:
    """Remove all items already marked purchased from the plan's shopping list.

    Returns the number of items removed. Inventory is not touched.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT plan_json FROM saved_plans WHERE id = ?", (plan_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return 0

    plan_data = json.loads(row[0])
    shopping_list = plan_data.get("shopping_list", [])
    remaining = [i for i in shopping_list if not i.get("purchased", False)]
    removed = len(shopping_list) - len(remaining)

    if removed:
        plan_data["shopping_list"] = remaining
        c.execute(
            "UPDATE saved_plans SET plan_json = ? WHERE id = ?",
            (json.dumps(plan_data), plan_id),
        )
        conn.commit()
    conn.close()
    return removed


def process_scanned_receipt_items(
    plan_id: Optional[int], receipt_items: List[ReceiptLineItem]
) -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    plan_data = None
    shopping_list = []
    if plan_id is not None:
        c.execute("SELECT plan_json FROM saved_plans WHERE id = ?", (plan_id,))
        row = c.fetchone()
        if row:
            plan_data = json.loads(row[0])
            shopping_list = plan_data.get("shopping_list", [])

    restocked_log = []

    for r_item in receipt_items:
        name = r_item.item_name.strip()
        qty = max(0.1, round(float(r_item.quantity), 2))
        unit = r_item.unit.strip() or "count"
        loc = r_item.default_location
        if loc not in ["Fridge", "Freezer", "Cupboard / Pantry"]:
            loc = "Cupboard / Pantry"
        inv_cat = r_item.inventory_category

        c.execute(
            "SELECT id, quantity FROM inventory WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
            (name,),
        )
        existing = c.fetchone()
        if existing:
            ex_id, ex_qty = existing
            new_qty = round(float(ex_qty) + qty, 2)
            c.execute(
                "UPDATE inventory SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, ex_id),
            )
            log_entry = f"🔄 **{name}**: +{qty} {unit} (Now {new_qty} {unit} in {loc})"
        else:
            c.execute(
                """
                INSERT INTO inventory (location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (loc, name, inv_cat, qty, unit, now),
            )
            log_entry = f"➕ **{name}**: Added {qty} {unit} to {loc}"

        matched_idx = r_item.matched_shopping_list_index
        if shopping_list:
            if 0 <= matched_idx < len(shopping_list):
                shopping_list[matched_idx]["purchased"] = True
                log_entry += f" *(✅ Checked off Shopping List)*"
            else:
                for s_idx, s_item in enumerate(shopping_list):
                    s_name = s_item.get("item_name", "").lower()
                    if s_name and (s_name in name.lower() or name.lower() in s_name):
                        shopping_list[s_idx]["purchased"] = True
                        log_entry += f" *(✅ Checked off Shopping List)*"
                        break

        restocked_log.append(log_entry)

    if plan_data is not None and plan_id is not None:
        plan_data["shopping_list"] = shopping_list
        c.execute(
            "UPDATE saved_plans SET plan_json = ? WHERE id = ?",
            (json.dumps(plan_data), plan_id),
        )

    conn.commit()
    conn.close()
    return restocked_log


def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT base_headcount, dietary_tags, custom_diet_notes, essential_staples FROM settings WHERE id = 1"
    )
    row = c.fetchone()
    conn.close()
    return {
        "base_headcount": row[0],
        "dietary_tags": json.loads(row[1]),
        "custom_diet_notes": row[2],
        "essential_staples": row[3],
    }


def save_settings(base_headcount: int, dietary_tags: list, custom_notes: str, staples: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        UPDATE settings
        SET base_headcount = ?, dietary_tags = ?, custom_diet_notes = ?, essential_staples = ?
        WHERE id = 1
    """,
        (base_headcount, json.dumps(dietary_tags), custom_notes, staples),
    )
    conn.commit()
    conn.close()


def save_plan(plan_dict: dict):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO saved_plans (created_at, plan_json) VALUES (?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), json.dumps(plan_dict)),
    )
    conn.commit()
    conn.close()


def get_latest_plan():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, created_at, plan_json FROM saved_plans ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1], json.loads(row[2])
    return None, None, None


def update_saved_plan(plan_id: int, plan_dict: dict):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE saved_plans SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_dict), plan_id),
    )
    conn.commit()
    conn.close()



# ==========================================
# GEMINI AI FUNCTIONS (ported from app.py)
# All Gemini calls raise a friendly RuntimeError on any API/parse failure;
# the API layer converts those to HTTP 502 with the friendly message.
# ==========================================


def get_client(api_key: str):
    return genai.Client(api_key=api_key)


def _generate_structured(keys_or_key, contents, schema, temperature: float, what: str):
    """Shared structured-output call routed through the AI provider fallback chain."""
    if isinstance(keys_or_key, ProviderKeys):
        keys = keys_or_key
    elif isinstance(keys_or_key, str):
        keys = ProviderKeys(gemini_key=keys_or_key)
    else:
        keys = ProviderKeys()
    return generate_structured_with_fallback(keys, contents, schema, temperature, what)


def analyze_storage_photo(keys, image: Image.Image, location: str) -> Tuple[List[DetectedItem], str]:
    prompt = f"""
    You are scanning a user's {location}.
    Identify all visible food items, ingredients, veggies, drinks, and seasonings.
    Estimate the remaining quantity and unit for each item as accurately as possible
    (e.g., 0.5 gallon, 2 cans, 12 oz, 1 bottle).
    """
    parsed, served_by = _generate_structured(
        keys, [image, prompt], VisionInventoryResult, 0.2, f"scan your {location} photo"
    )
    return parsed.items, served_by


def _receipt_prompt(shopping_list: list, source_description: str) -> str:
    """Shared receipt-extraction prompt for images, PDFs, and pasted text."""
    indexed_shopping = [
        {
            "index": idx,
            "item_name": item.get("item_name"),
            "amount_needed": item.get("amount_needed"),
            "purchased": item.get("purchased", False),
        }
        for idx, item in enumerate(shopping_list)
    ]

    return f"""
    You are scanning {source_description}.
    1. Extract every food, drink, produce, meat, dairy, or seasoning item purchased.
    2. Decode abbreviated store receipt text into clean, standard grocery names.
    3. Ignore non-food items (paper towels, tax, etc.).
    4. Estimate numeric quantity, unit, default storage location, and category.
    5. Match against the ACTIVE SHOPPING LIST below where applicable.

    ACTIVE SHOPPING LIST:
    {json.dumps(indexed_shopping, indent=2)}
    """


def analyze_grocery_receipt(
    keys, image: Image.Image, shopping_list: list
) -> Tuple[ReceiptScanResult, str]:
    prompt = _receipt_prompt(shopping_list, "a grocery store receipt image")
    return _generate_structured(
        keys, [image, prompt], ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def analyze_receipt_pdf(
    keys, pdf_bytes: bytes, shopping_list: list
) -> Tuple[ReceiptScanResult, str]:
    prompt = _receipt_prompt(
        shopping_list, "a grocery store receipt or online grocery order document (PDF)"
    )
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    return _generate_structured(
        keys, [pdf_part, prompt], ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def analyze_receipt_text(
    keys, text: str, shopping_list: list
) -> Tuple[ReceiptScanResult, str]:
    prompt = _receipt_prompt(
        shopping_list,
        "pasted text from a grocery store receipt or online grocery order confirmation",
    )
    contents = f"{prompt}\nRECEIPT / ORDER TEXT:\n{text}"
    return _generate_structured(
        keys, contents, ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def generate_weekly_menu_and_list(
    keys,
    inventory_df: pd.DataFrame,
    settings: dict,
    daily_headcounts: dict,
    active_theme: str,
) -> Tuple[WeeklyPlanOutput, str]:
    if inventory_df.empty:
        inv_records = []
    else:
        inv_records = inventory_df[
            ["id", "location", "item_name", "category", "quantity", "unit"]
        ].to_dict(orient="records")

    prompt = f"""
    Build a practical, delicious 7-day meal plan and a consolidated shopping list.
    Current Season/Holiday Context: {active_theme}

    CURRENT KITCHEN INVENTORY (Cupboard, Fridge, Freezer):
    {json.dumps(inv_records, indent=2)}

    DAILY HEADCOUNT SCHEDULE (Scale portion sizes and ingredient math to match each day):
    {json.dumps(daily_headcounts, indent=2)}

    HOUSEHOLD DIETARY RESTRICTIONS & MEDICAL NEEDS (STRICT ENFORCEMENT):
    - Selected Tags/Diets: {", ".join(settings["dietary_tags"])}
    - Specific Household Rules & Allergies: {settings["custom_diet_notes"]}

    DIETARY ENFORCEMENT RULES FOR THE AI:
    1. STRICTLY OBEY all dietary tags, medical restrictions, and food allergies specified by the household above. Avoid all allergens and restricted ingredients completely across every meal.
    2. Provide clear step-by-step preparation instructions for breakfast, lunch, and dinner, including timings and recommendations for countertop appliances (like an air fryer or stovetop/oven).

    INSTRUCTIONS:
    1. Prioritize using existing fridge, freezer, and cupboard items before they spoil.
    2. Scale recipes accurately for the exact headcount on each day.
    3. For each day's `ingredients_to_deduct`, reference the exact `id` (`inventory_id`), `item_name`, `amount_used` (numeric), and matching `unit` from CURRENT KITCHEN INVENTORY. Set `cooked` to False.
    4. Compare total ingredients needed for the 7 days against CURRENT KITCHEN INVENTORY and generate a categorized Shopping List for missing items. Populate `numeric_quantity`, `unit`, `default_location`, and `inventory_category`. Set `purchased` to False.
    """

    return _generate_structured(
        keys, prompt, WeeklyPlanOutput, 0.3, "build your weekly menu"
    )


def swap_single_meal(
    keys,
    available_inventory: list,
    target_slot: str,
    current_dish_name: str,
    other_meals: list,
    headcount: int,
    settings: dict,
) -> Tuple[MealSwapOutput, str]:
    """Re-picks a single meal slot using ONLY on-hand ingredients.
    
    Routes through the Milestone 5 provider fallback chain.
    """
    prompt = f"""
    You are swapping out a single meal for {target_slot.upper()}.
    
    RULES FOR THE REPLACEMENT MEAL:
    1. It MUST be buildable entirely from ingredients currently on hand in AVAILABLE INVENTORY.
       Every ingredient used must exist in AVAILABLE INVENTORY with sufficient remaining quantity.
       DO NOT propose any ingredient not in AVAILABLE INVENTORY or exceeding available quantity.
       DO NOT create any new shopping list items.
    2. It MUST NOT be "{current_dish_name}" (the meal being swapped out).
    3. It SHOULD NOT duplicate any other meal already planned this week: {json.dumps(other_meals)}
    4. Household Dietary Rules (STRICT ENFORCEMENT):
       - Selected Tags/Diets: {", ".join(settings["dietary_tags"])}
       - Specific Household Rules & Allergies: {settings["custom_diet_notes"]}
       The replacement must strictly comply with all dietary tags and allergies.
    5. Scale portion sizes accurately for a headcount of {headcount} people.
    6. For each ingredient in `ingredients_used`, reference the exact `inventory_id` (matching the id in AVAILABLE INVENTORY), `item_name`, numeric `amount_used`, and `unit`.
    7. IF NO ON-HAND REPLACEMENT SATISFIES ALL THE RULES: set `found` to False, leave `dish_name` empty, and set `reason_if_not_found` to "Nothing else on hand fits this slot". NEVER invent ingredients or ignore dietary rules.

    AVAILABLE INVENTORY (Items on hand after committed usage):
    {json.dumps(available_inventory, indent=2)}
    """

    return _generate_structured(
        keys, prompt, MealSwapOutput, 0.2, "swap meal"
    )


# ==========================================
# SHOPPING LIST MARKDOWN EXPORT
# (same format as the Streamlit Tab 4 export)
# ==========================================


def build_shopping_markdown(plan: dict, created_at: Optional[str], include_purchased: bool) -> str:
    shopping_items = plan.get("shopping_list", [])
    export_lines = [
        "# 🛒 Shopping List",
        f"_Plan generated: {created_at}_",
        "",
        "## This Week's Menu",
    ]
    for d in plan.get("menu", []):
        export_lines.append(
            f"- **{d['day']}** — Breakfast: {d.get('breakfast', {}).get('dish_name', '')} | "
            f"Lunch: {d.get('lunch', {}).get('dish_name', '')} | "
            f"Dinner: {d.get('dinner', {}).get('dish_name', '')}"
        )
    export_lines += ["", "## To Buy"]
    export_cats = sorted(list({i.get("category", "General") for i in shopping_items}))
    any_to_buy = False
    for cat in export_cats:
        cat_items = [
            i
            for i in shopping_items
            if i.get("category", "General") == cat
            and (include_purchased or not i.get("purchased", False))
        ]
        if not cat_items:
            continue
        any_to_buy = True
        export_lines.append(f"\n### {cat}")
        for i in cat_items:
            mark = "x" if i.get("purchased", False) else " "
            export_lines.append(
                f"- [{mark}] {i['item_name']} — {i['amount_needed']} "
                f"(store to: {i.get('default_location', 'Cupboard / Pantry')})"
            )
    if not any_to_buy:
        export_lines.append("\n🎉 Nothing to buy — inventory covers the week!")
    return "\n".join(export_lines)



# ==========================================
# FASTAPI APP & REST ENDPOINTS
# ==========================================

app = FastAPI(title="Pantry-to-Menu Planner API", version="1.0.0")

init_db()

# Middleware order matters: the LAST one added is the OUTERMOST. CORS must wrap the
# phone-access gate so that its 401/403 replies still carry CORS headers - otherwise
# a phone app would see a bare network error instead of a readable message.
# Same-origin use (the desktop window, the browser UI) is unaffected by CORS.
app.add_middleware(phone_access.PhoneAccessMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "https://localhost", "capacitor://localhost"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-Gemini-Key",
        "X-Groq-Key",
        "X-OpenRouter-Key",
        "X-Pantry-Token",
    ],
)
# Loopback-only /api/desktop/* routes. Must be registered before the catch-all
# static mount at the bottom of this file.
app.include_router(phone_access.router)


# ---------- Request body models ----------


class InventoryItemIn(BaseModel):
    id: Optional[int] = None
    location: str = "Cupboard / Pantry"
    item_name: str
    category: str = "Pantry/Dry"
    quantity: float = 0.0
    unit: str = "count"
    updated_at: Optional[str] = None


class AddInventoryRequest(BaseModel):
    location: str
    items: List[DetectedItem]


class SettingsIn(BaseModel):
    base_headcount: int
    dietary_tags: List[str] = []
    custom_diet_notes: str = ""
    essential_staples: str = ""


class GeneratePlanRequest(BaseModel):
    daily_headcounts: Dict[str, int] = {}
    theme_name: Optional[str] = None


class CookRequest(BaseModel):
    day_index: int


class SwapMealRequest(BaseModel):
    day_index: int
    meal_slot: str  # 'breakfast', 'lunch', or 'dinner'


class RestockRequest(BaseModel):
    indices: List[int] = []


class ReceiptPasteRequest(BaseModel):
    text: str = ""


# ---------- Helpers ----------


def require_ai_keys(request: Request) -> ProviderKeys:
    """Extract AI keys: Gemini, Groq, OpenRouter from headers or env vars."""
    keys = extract_provider_keys(request.headers)
    if not (keys.gemini_key or keys.groq_key or keys.openrouter_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "No AI API key available. Enter your Gemini API key (or Groq/OpenRouter key) in the app "
                "(Diet tab) or set the GEMINI_API_KEY environment variable."
            ),
        )
    return keys


def ai_call(fn, *args):
    """Run an AI function, converting friendly RuntimeErrors to HTTP 502."""
    try:
        return fn(*args)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


# Legacy aliases for backward compatibility if tests call them
require_gemini_key = require_ai_keys
gemini_call = ai_call


async def load_image(upload: UploadFile) -> Image.Image:
    data = await upload.read()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image.convert("RGB")
    except Exception:
        raise HTTPException(
            status_code=400, detail="That file could not be read as an image."
        )


def latest_plan_payload():
    plan_id, created_at, plan = get_latest_plan()
    return {"plan_id": plan_id, "created_at": created_at, "plan": plan}


# ---------- Health ----------


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "pantry-planner",
        "auth_required": bool(phone_access.load_config().get("access_code")),
    }


# ---------- Inventory ----------


@app.get("/api/inventory")
def api_get_inventory():
    return get_inventory()


@app.post("/api/inventory")
def api_add_inventory(body: AddInventoryRequest):
    if body.location not in LOCATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown location: {body.location}")
    add_inventory_items(body.location, body.items)
    return {"added": len(body.items), "inventory": get_inventory()}


@app.put("/api/inventory")
def api_replace_inventory(items: List[InventoryItemIn]):
    df = pd.DataFrame(
        [
            {
                "id": it.id,
                "location": it.location,
                "item_name": it.item_name,
                "category": it.category,
                "quantity": it.quantity,
                "unit": it.unit,
                "updated_at": it.updated_at,
            }
            for it in items
        ],
        columns=["id", "location", "item_name", "category", "quantity", "unit", "updated_at"],
    )
    replace_inventory_from_df(df)
    return {"inventory": get_inventory()}


@app.delete("/api/inventory")
def api_clear_inventory():
    clear_inventory()
    return {"inventory": []}


# ---------- Settings ----------


@app.get("/api/settings")
def api_get_settings():
    return get_settings()


@app.put("/api/settings")
def api_save_settings(body: SettingsIn):
    save_settings(body.base_headcount, body.dietary_tags, body.custom_diet_notes, body.essential_staples)
    return get_settings()


# ---------- Photo scan ----------


@app.post("/api/scan-photo")
async def api_scan_photo(
    request: Request,
    file: UploadFile = File(...),
    location: str = Form(...),
):
    if location not in LOCATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown location: {location}")
    keys = require_ai_keys(request)
    image = await load_image(file)
    detected, served_by = ai_call(analyze_storage_photo, keys, image, location)
    add_inventory_items(location, detected)
    return {
        "added": len(detected),
        "items": [d.model_dump() for d in detected],
        "inventory": get_inventory(),
        "served_by": served_by,
    }


# ---------- Weekly plan ----------


@app.post("/api/plan/generate")
def api_generate_plan(body: GeneratePlanRequest, request: Request):
    keys = require_ai_keys(request)
    settings = get_settings()
    theme = body.theme_name or get_auto_theme_name()
    plan_output, served_by = ai_call(
        generate_weekly_menu_and_list,
        keys,
        get_inventory_df(),
        settings,
        body.daily_headcounts,
        theme,
    )
    save_plan(plan_output.model_dump())
    payload = latest_plan_payload()
    payload["served_by"] = served_by
    return payload


@app.get("/api/plan/latest")
def api_latest_plan():
    return latest_plan_payload()


@app.post("/api/plan/cook")
def api_cook_day(body: CookRequest):
    plan_id, _created_at, plan = get_latest_plan()
    if plan_id is None or plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")
    menu = plan.get("menu", [])
    if body.day_index < 0 or body.day_index >= len(menu):
        raise HTTPException(status_code=400, detail=f"day_index out of range: {body.day_index}")
    deducted = deduct_day_from_inventory(plan_id, body.day_index)
    payload = latest_plan_payload()
    payload["deducted"] = deducted
    payload["inventory"] = get_inventory()
    return payload


@app.post("/api/plan/swap-meal")
def api_swap_meal(body: SwapMealRequest, request: Request):
    """Swap out a single meal slot using ONLY available on-hand ingredients.

    Calculates remaining on-hand inventory (total inventory minus committed
    deductions for other meals in the week), and requests a replacement meal
    from the Milestone 5 AI provider chain. If no compliant on-hand meal is
    found, leaves original meal untouched and returns friendly message.
    """
    keys = require_ai_keys(request)
    plan_id, _created_at, plan = get_latest_plan()
    if plan_id is None or plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")

    menu = plan.get("menu", [])
    if body.day_index < 0 or body.day_index >= len(menu):
        raise HTTPException(status_code=400, detail=f"day_index out of range: {body.day_index}")

    slot = body.meal_slot.lower().strip()
    if slot not in ("breakfast", "lunch", "dinner"):
        raise HTTPException(status_code=400, detail=f"Invalid meal slot: {body.meal_slot}")

    day_obj = menu[body.day_index]
    current_meal = day_obj.get(slot)
    if not current_meal or not isinstance(current_meal, dict):
        raise HTTPException(status_code=400, detail=f"No meal found in {day_obj.get('day')} {slot}")

    current_dish_name = current_meal.get("dish_name", "")
    headcount = day_obj.get("headcount", 2)
    settings = get_settings()

    # 1. Collect all other meals planned across the week to avoid duplicates
    other_meals = []
    for d_idx, d in enumerate(menu):
        for s in ("breakfast", "lunch", "dinner"):
            if d_idx == body.day_index and s == slot:
                continue
            m = d.get(s)
            if m and isinstance(m, dict) and m.get("dish_name"):
                other_meals.append(m["dish_name"])

    # 2. Compute committed inventory usage by all other meals/days
    committed: Dict[int, float] = {}
    for d_idx, d in enumerate(menu):
        if d_idx != body.day_index:
            for ing in d.get("ingredients_to_deduct", []):
                inv_id = ing.get("inventory_id")
                amt = float(ing.get("amount_used", 0.0))
                if inv_id is not None and amt > 0:
                    committed[inv_id] = committed.get(inv_id, 0.0) + amt

    # 3. Calculate remaining on-hand inventory
    all_inv = get_inventory()
    available_inv = []
    for item in all_inv:
        item_id = item["id"]
        total_qty = float(item.get("quantity") or 0.0)
        rem_qty = round(total_qty - committed.get(item_id, 0.0), 2)
        if rem_qty > 0.01:
            available_inv.append({
                "id": item_id,
                "location": item.get("location"),
                "item_name": item.get("item_name"),
                "category": item.get("category"),
                "quantity": rem_qty,
                "unit": item.get("unit"),
            })

    if not available_inv:
        return {
            "success": False,
            "message": "Nothing else on hand fits this slot",
            "plan_id": plan_id,
            "plan": plan,
            "served_by": "",
        }

    # 4. Route AI call through Milestone 5 fallback chain
    swap_res, served_by = ai_call(
        swap_single_meal,
        keys,
        available_inv,
        slot,
        current_dish_name,
        other_meals,
        headcount,
        settings,
    )

    if not swap_res.found or not swap_res.dish_name:
        msg = swap_res.reason_if_not_found or "Nothing else on hand fits this slot"
        return {
            "success": False,
            "message": msg,
            "plan_id": plan_id,
            "plan": plan,
            "served_by": served_by,
        }

    # 5. Apply the replacement to target slot
    new_meal = {
        "dish_name": swap_res.dish_name,
        "prep_steps": swap_res.prep_steps,
        "cook_time_minutes": swap_res.cook_time_minutes,
        "changed_from": current_dish_name,
    }
    day_obj[slot] = new_meal

    # Append new ingredient deductions to the day
    new_deductions = [i.model_dump() for i in swap_res.ingredients_used]
    current_deductions = day_obj.get("ingredients_to_deduct", [])
    day_obj["ingredients_to_deduct"] = current_deductions + new_deductions

    # 6. Save updated plan to database
    update_saved_plan(plan_id, plan)

    payload = latest_plan_payload()
    payload["success"] = True
    payload["message"] = f"Changed {slot.capitalize()} to {swap_res.dish_name} (from: {current_dish_name})"
    payload["served_by"] = served_by
    return payload


# ---------- Shopping ----------


@app.post("/api/shopping/restock")
def api_restock(body: RestockRequest):
    plan_id, _created_at, plan = get_latest_plan()
    if plan_id is None or plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")
    restocked = restock_checked_shopping_items(plan_id, body.indices)
    payload = latest_plan_payload()
    payload["restocked"] = restocked
    payload["inventory"] = get_inventory()
    return payload


@app.post("/api/shopping/clear-purchased")
def api_clear_purchased():
    plan_id, _created_at, plan = get_latest_plan()
    if plan_id is None or plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")
    cleared = clear_purchased_shopping_items(plan_id)
    payload = latest_plan_payload()
    payload["cleared"] = cleared
    return payload


@app.get("/api/shopping-list/export")
def api_export_shopping_list(include_purchased: bool = Query(False)):
    _plan_id, created_at, plan = get_latest_plan()
    if plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")
    md = build_shopping_markdown(plan, created_at, include_purchased)
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


# ---------- Receipt scan / online order import ----------


def _active_shopping_list() -> list:
    _plan_id, _created_at, plan = get_latest_plan()
    return plan.get("shopping_list", []) if plan else []


def _receipt_response(store_name: str, items: List[ReceiptLineItem], served_by: str = ""):
    """Run the single inventory-update pass and build the shared response."""
    plan_id, _created_at, _plan = get_latest_plan()
    logs = process_scanned_receipt_items(plan_id, items)
    payload = latest_plan_payload()
    return {
        "store_name": store_name,
        "items": [i.model_dump() for i in items],
        "logs": logs,
        "plan_id": payload["plan_id"],
        "plan": payload["plan"],
        "inventory": get_inventory(),
        "served_by": served_by,
    }


@app.post("/api/receipt/scan")
async def api_scan_receipt(request: Request):
    """Scan one or more receipt photos/screenshots (online-order imports).

    Accepts repeated `files` fields (and the legacy single `file` field).
    Each image is analyzed separately; the items are then merged and deduped
    by normalized (item_name, quantity, unit) so overlapping screenshots of
    the same order don't double-count, before one inventory update pass.
    """
    keys = require_ai_keys(request)
    form = await request.form()
    uploads: List[UploadFile] = []
    for field_name, value in form.multi_items():
        if (
            field_name in ("files", "file")
            and isinstance(value, StarletteUploadFile)
            and value.filename
        ):
            uploads.append(value)
    if not uploads:
        raise HTTPException(status_code=400, detail="No receipt photo was uploaded.")

    active_shop_list = _active_shopping_list()
    merged: List[ReceiptLineItem] = []
    seen = set()
    store_name = "Grocery Store"
    last_served_by = ""
    for upload in uploads:
        image = await load_image(upload)
        scan_res, served_by = ai_call(analyze_grocery_receipt, keys, image, active_shop_list)
        last_served_by = served_by
        if store_name == "Grocery Store" and scan_res.store_name:
            store_name = scan_res.store_name
        for item in scan_res.items:
            dedupe_key = (
                item.item_name.strip().lower(),
                round(float(item.quantity), 2),
                item.unit.strip().lower(),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(item)
    return _receipt_response(store_name, merged, last_served_by)


@app.post("/api/receipt/scan-pdf")
async def api_scan_receipt_pdf(request: Request, file: UploadFile = File(...)):
    """Import an online grocery order from a PDF (order confirmation/receipt)."""
    keys = require_ai_keys(request)
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="That PDF file was empty.")
    scan_res, served_by = ai_call(
        analyze_receipt_pdf, keys, pdf_bytes, _active_shopping_list()
    )
    return _receipt_response(scan_res.store_name, scan_res.items, served_by)


@app.post("/api/receipt/paste")
def api_paste_receipt(body: ReceiptPasteRequest, request: Request):
    """Import an online grocery order from pasted confirmation text/email."""
    keys = require_ai_keys(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400, detail="Paste your order or receipt text first."
        )
    scan_res, served_by = ai_call(
        analyze_receipt_text, keys, text, _active_shopping_list()
    )
    return _receipt_response(scan_res.store_name, scan_res.items, served_by)


# ---------- Provider key validation ----------


class ValidateProviderRequest(BaseModel):
    provider: str = "gemini"
    api_key: str = ""


@app.post("/api/validate-provider")
def api_validate_provider(body: ValidateProviderRequest, request: Request):
    """Validate any supported AI provider key (Gemini, Groq, OpenRouter)."""
    provider = body.provider.lower().strip()
    key = body.api_key.strip()
    if not key:
        header_key = (
            request.headers.get(f"x-{provider}-key")
            or request.headers.get(f"X-{provider.capitalize()}-Key")
            or ""
        )
        key = header_key.strip()
    if not key:
        raise HTTPException(
            status_code=400, detail=f"No key provided. Paste your {provider.capitalize()} API key first."
        )
    ok, msg = validate_provider_key(provider, key)
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True, "message": msg}


@app.post("/api/validate-key")
def api_validate_key(request: Request):
    """Validate a user-supplied Gemini key (backward compatible endpoint)."""
    api_key = request.headers.get("x-gemini-key") or ""
    if not api_key:
        raise HTTPException(
            status_code=400, detail="No key provided. Paste your Gemini API key first."
        )
    ok, msg = validate_provider_key("gemini", api_key)
    if not ok:
        return {"ok": False, "error": f"That key didn't work — {msg}"}
    return {"ok": True, "message": "Key works! You're connected."}


# ---------- Static PWA (mounted last so /api routes win) ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

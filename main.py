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
import logging
import os
import re
import sqlite3

logger = logging.getLogger("pantry_planner.main")
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
    extract_provider_models,
    generate_structured_with_fallback,
    validate_provider_key,
    save_provider_model_with_validation,
    probe_all_providers,
    get_providers_status,
)

import phone_access
import db
import religious_diet
from religious_diet import (
    check_meal_violation,
    build_religious_diet_prompt_section,
)

# ==========================================
# PATHS
# ==========================================

BASE_DIR = Path(__file__).resolve().parent  # this project's root (main.py lives here)
STATIC_DIR = Path(__file__).resolve().parent / "static"

def get_db_file() -> str:
    return db.get_db_file()

DB_FILE = get_db_file()

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
    servings: Optional[int] = Field(
        default=None, description="Servings for this day (defaults to headcount / household size)"
    )
    is_overridden: bool = Field(
        default=False, description="Whether this day's servings was manually overridden"
    )
    servings_override: bool = Field(
        default=False, description="Alias for is_overridden"
    )
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


def init_db(household: Optional[str] = None):
    db.init_db(household)


def add_inventory_items(location: str, items: List[DetectedItem], household: Optional[str] = None):
    inv_tbl, _, _ = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for item in items:
        c.execute(
            f"""
            INSERT INTO {inv_tbl} (location, item_name, category, quantity, unit, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (location, item.item_name, item.category, item.quantity, item.unit, now),
        )
    conn.commit()
    conn.close()


def get_inventory_df(household: Optional[str] = None) -> pd.DataFrame:
    inv_tbl, _, _ = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT * FROM {inv_tbl} ORDER BY location, category, item_name")
    rows = c.fetchall()
    cols = [d[0] for d in c.description] if c.description else ["id", "location", "item_name", "category", "quantity", "unit", "updated_at"]
    data = [list(r) for r in rows]
    conn.close()
    return pd.DataFrame(data, columns=cols)


def get_inventory(household: Optional[str] = None) -> List[dict]:
    """Inventory as a list of plain dicts (JSON-friendly types)."""
    inv_tbl, _, _ = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT * FROM {inv_tbl} ORDER BY location, category, item_name")
    rows = c.fetchall()
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


def clear_inventory(household: Optional[str] = None):
    inv_tbl, _, _ = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"DELETE FROM {inv_tbl}")
    conn.commit()
    conn.close()


def replace_inventory_from_df(df: pd.DataFrame, household: Optional[str] = None):
    # FIX (2026-10-14, from app.py): preserve existing row IDs on save. The
    # original version deleted all rows and re-inserted without IDs, so every
    # save renumbered the inventory and broke the inventory_id references
    # stored in saved meal plans. Rows carrying a valid existing id keep it;
    # brand-new rows (id empty/NaN) get a fresh id.
    inv_tbl, _, _ = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"DELETE FROM {inv_tbl}")
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
                f"""
                INSERT INTO {inv_tbl} (id, location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (keep_id,) + values,
            )
        else:
            c.execute(
                f"""
                INSERT INTO {inv_tbl} (location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                values,
            )
    conn.commit()
    conn.close()


# ==========================================
# PER-DAY SERVINGS RESCALING LOGIC (Part 2 Rule 5)
# ==========================================

import math

COUNTABLE_UNITS = {
    "count", "item", "items", "can", "cans", "clove", "cloves", "egg", "eggs",
    "bottle", "bottles", "box", "boxes", "bag", "bags", "jar", "jars",
    "package", "packages", "pack", "packs", "piece", "pieces", "head", "heads",
    "bunch", "bunches", "stalk", "stalks", "slice", "slices", "loaf", "loaves",
    "ear", "ears", "pinch", "dash", "packet", "packets", ""
}


def is_countable_unit(unit: Optional[str]) -> bool:
    if not unit:
        return True
    return unit.lower().strip() in COUNTABLE_UNITS


def round_scaled_quantity(val: float, unit: Optional[str]) -> float:
    """Rounding rule (Part 2 Rule 5):
    - fractional measures round to nearest 0.25;
    - countable whole items (eggs, cans, cloves — units that are whole things) round UP to the next whole number;
    - a quantity never drops below 1 whole countable item.
    """
    if is_countable_unit(unit):
        return float(max(1, math.ceil(val)))
    else:
        rounded = round(val * 4.0) / 4.0
        if val > 0 and rounded == 0.0:
            rounded = 0.25
        return rounded


def scale_amount_needed_str(amount_needed: str, ratio: float, unit: Optional[str]) -> str:
    """Scales leading number in amount_needed string if parseable.
    If any ingredient quantity is free text with no parseable number, leave its text unchanged.
    """
    if not amount_needed:
        return amount_needed
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(.*)$", amount_needed.strip())
    if not m:
        return amount_needed
    old_num = float(m.group(1))
    rest = m.group(2)
    new_num = round_scaled_quantity(old_num * ratio, unit)
    num_str = str(int(new_num)) if new_num.is_integer() else str(new_num)
    return f"{num_str} {rest}".strip() if rest else num_str


def scale_day_ingredients_and_shopping(plan: dict, day_name: str, old_servings: int, new_servings: int):
    """Rescales a single day's ingredient deductions and matching shopping list items by new ÷ old."""
    if old_servings <= 0 or new_servings <= 0 or old_servings == new_servings:
        return
    ratio = float(new_servings) / float(old_servings)
    d_clean = (day_name or "").lower().strip()

    # 1. Rescale ingredients_to_deduct on that day
    for d in plan.get("menu", []):
        if (d.get("day") or "").lower().strip() == d_clean:
            for ing in d.get("ingredients_to_deduct", []):
                amt = float(ing.get("amount_used", 0.0))
                u = ing.get("unit", "")
                ing["amount_used"] = round_scaled_quantity(amt * ratio, u)
            break

    # 2. Rescale shopping list items that belong to that day
    all_days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for item in plan.get("shopping_list", []):
        reason = (item.get("reason") or "").lower()
        item_day = (item.get("day") or "").lower().strip()
        belongs = False
        if item_day == d_clean:
            belongs = True
        elif d_clean in reason:
            other_days = [o for o in all_days if o != d_clean and o in reason]
            if not other_days:
                belongs = True

        if belongs:
            num_qty = float(item.get("numeric_quantity", 0.0))
            u = item.get("unit", "")
            item["numeric_quantity"] = round_scaled_quantity(num_qty * ratio, u)
            item["amount_needed"] = scale_amount_needed_str(item.get("amount_needed", ""), ratio, u)


def deduct_day_from_inventory(plan_id: int, day_idx: int, household: Optional[str] = None) -> List[str]:
    inv_tbl, _, plan_tbl = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT plan_json FROM {plan_tbl} WHERE id = ?", (plan_id,))
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

        c.execute(f"SELECT id, quantity FROM {inv_tbl} WHERE id = ?", (inv_id,))
        target = c.fetchone()
        if not target:
            c.execute(
                f"SELECT id, quantity FROM {inv_tbl} WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
                (name,),
            )
            target = c.fetchone()

        if target:
            target_id, current_qty = target[0], target[1]
            new_qty = max(0.0, round(float(current_qty) - amt, 2))
            c.execute(
                f"UPDATE {inv_tbl} SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, target_id),
            )
            deducted_summary.append(f"{name}: -{amt} {unit} (now {new_qty} {unit})")

    plan_data["menu"][day_idx]["cooked"] = True
    c.execute(
        f"UPDATE {plan_tbl} SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_data), plan_id),
    )
    conn.commit()
    conn.close()
    return deducted_summary


def restock_checked_shopping_items(plan_id: int, item_indices: List[int], household: Optional[str] = None) -> List[str]:
    inv_tbl, _, plan_tbl = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT plan_json FROM {plan_tbl} WHERE id = ?", (plan_id,))
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
            f"SELECT id, quantity FROM {inv_tbl} WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
            (name,),
        )
        existing = c.fetchone()

        if existing:
            ex_id, ex_qty = existing[0], existing[1]
            new_qty = round(float(ex_qty) + qty, 2)
            c.execute(
                f"UPDATE {inv_tbl} SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, ex_id),
            )
            restocked_log.append(f"Updated {name}: +{qty} {unit} (Total: {new_qty} {unit})")
        else:
            c.execute(
                f"""
                INSERT INTO {inv_tbl} (location, item_name, category, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (loc, name, inv_cat, qty, unit, now),
            )
            restocked_log.append(f"Added {name} ({qty} {unit}) to {loc}")

        shopping_list[idx]["purchased"] = True

    plan_data["shopping_list"] = shopping_list
    c.execute(
        f"UPDATE {plan_tbl} SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_data), plan_id),
    )
    conn.commit()
    conn.close()
    return restocked_log


def clear_purchased_shopping_items(plan_id: int, household: Optional[str] = None) -> int:
    """Remove all items already marked purchased from the plan's shopping list.

    Returns the number of items removed. Inventory is not touched.
    """
    _, _, plan_tbl = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT plan_json FROM {plan_tbl} WHERE id = ?", (plan_id,))
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
            f"UPDATE {plan_tbl} SET plan_json = ? WHERE id = ?",
            (json.dumps(plan_data), plan_id),
        )
        conn.commit()
    conn.close()
    return removed


def process_scanned_receipt_items(
    plan_id: Optional[int], receipt_items: List[ReceiptLineItem], household: Optional[str] = None
) -> List[str]:
    inv_tbl, _, plan_tbl = db.get_table_names(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    plan_data = None
    shopping_list = []
    if plan_id is not None:
        c.execute(f"SELECT plan_json FROM {plan_tbl} WHERE id = ?", (plan_id,))
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
            f"SELECT id, quantity FROM {inv_tbl} WHERE LOWER(item_name) = LOWER(?) LIMIT 1",
            (name,),
        )
        existing = c.fetchone()
        if existing:
            ex_id, ex_qty = existing[0], existing[1]
            new_qty = round(float(ex_qty) + qty, 2)
            c.execute(
                f"UPDATE {inv_tbl} SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, ex_id),
            )
            log_entry = f"🔄 **{name}**: +{qty} {unit} (Now {new_qty} {unit} in {loc})"
        else:
            c.execute(
                f"""
                INSERT INTO {inv_tbl} (location, item_name, category, quantity, unit, updated_at)
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
            f"UPDATE {plan_tbl} SET plan_json = ? WHERE id = ?",
            (json.dumps(plan_data), plan_id),
        )

    conn.commit()
    conn.close()
    return restocked_log


def get_settings(household: Optional[str] = None) -> dict:
    _, set_tbl, _ = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    try:
        c.execute(
            f"SELECT base_headcount, dietary_tags, custom_diet_notes, essential_staples, kitchen_name, religious_presets FROM {set_tbl} WHERE id = 1"
        )
        row = c.fetchone()
    except Exception:
        try:
            c.execute(
                f"SELECT base_headcount, dietary_tags, custom_diet_notes, essential_staples, kitchen_name FROM {set_tbl} WHERE id = 1"
            )
            row = c.fetchone()
        except Exception:
            c.execute(
                f"SELECT base_headcount, dietary_tags, custom_diet_notes, essential_staples FROM {set_tbl} WHERE id = 1"
            )
            row = c.fetchone()
    conn.close()
    if not row:
        return {
            "base_headcount": 2,
            "dietary_tags": ["Balanced & Whole Foods"],
            "custom_diet_notes": "",
            "essential_staples": "",
            "kitchen_name": "",
            "religious_presets": [],
        }
    k_name = row[4] if (len(row) > 4 and row[4]) else ""
    r_presets = []
    if len(row) > 5 and row[5]:
        try:
            r_presets = json.loads(row[5]) if isinstance(row[5], str) else row[5]
        except Exception:
            r_presets = []
    return {
        "base_headcount": row[0],
        "dietary_tags": json.loads(row[1]) if isinstance(row[1], str) else row[1],
        "custom_diet_notes": row[2],
        "essential_staples": row[3],
        "kitchen_name": k_name,
        "religious_presets": r_presets,
    }


def save_settings(
    base_headcount: int,
    dietary_tags: list,
    custom_notes: str,
    staples: str,
    household: Optional[str] = None,
    kitchen_name: Optional[str] = None,
    religious_presets: Optional[list] = None,
):
    _, set_tbl, _ = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    presets_json = json.dumps(religious_presets if religious_presets is not None else [])
    try:
        c.execute(
            f"""
            UPDATE {set_tbl}
            SET base_headcount = ?, dietary_tags = ?, custom_diet_notes = ?, essential_staples = ?, religious_presets = ?
            WHERE id = 1
        """,
            (base_headcount, json.dumps(dietary_tags), custom_notes, staples, presets_json),
        )
    except Exception:
        c.execute(
            f"""
            UPDATE {set_tbl}
            SET base_headcount = ?, dietary_tags = ?, custom_diet_notes = ?, essential_staples = ?
            WHERE id = 1
        """,
            (base_headcount, json.dumps(dietary_tags), custom_notes, staples),
        )
    if kitchen_name is not None and kitchen_name.strip():
        try:
            c.execute(f"SELECT kitchen_name FROM {set_tbl} WHERE id = 1")
            r = c.fetchone()
            if not (r and r[0] and str(r[0]).strip()):
                formatted = kitchen_name.strip()
                if not formatted.endswith("'s Kitchen"):
                    formatted = f"{formatted}'s Kitchen"
                c.execute(f"UPDATE {set_tbl} SET kitchen_name = ? WHERE id = 1", (formatted,))
        except Exception:
            pass
    conn.commit()
    conn.close()

    # Rule 3: Mid-week household change updates all non-overridden days of current saved plan
    plan_id, _created_at, plan = get_latest_plan(household)
    if plan and isinstance(plan, dict) and "menu" in plan:
        plan_modified = False
        for d in plan.get("menu", []):
            if not d.get("is_overridden", False):
                old_s = d.get("servings") or d.get("headcount")
                if old_s and old_s != base_headcount:
                    scale_day_ingredients_and_shopping(plan, d.get("day", ""), old_s, base_headcount)
                    d["servings"] = base_headcount
                    d["headcount"] = base_headcount
                    plan_modified = True
        if plan_modified:
            update_saved_plan(plan_id, plan, household)


def save_plan(plan_dict: dict, household: Optional[str] = None) -> Optional[int]:
    _, _, plan_tbl = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    if isinstance(plan_dict, dict) and "menu" in plan_dict and isinstance(plan_dict["menu"], list):
        for day in plan_dict["menu"]:
            if isinstance(day, dict):
                if day.get("servings") is None:
                    day["servings"] = day.get("headcount", 2)
                day["headcount"] = day["servings"]
                if "is_overridden" not in day:
                    day["is_overridden"] = False
                day["servings_override"] = day["is_overridden"]
    c.execute(
        f"INSERT INTO {plan_tbl} (created_at, plan_json) VALUES (?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), json.dumps(plan_dict)),
    )
    new_id = getattr(c, "lastrowid", None)
    conn.commit()
    conn.close()
    return new_id


def get_latest_plan(household: Optional[str] = None):
    _, _, plan_tbl = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT id, created_at, plan_json FROM {plan_tbl} ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        plan_id = row[0]
        created_at = row[1]
        plan = json.loads(row[2])
        if isinstance(plan, dict) and "menu" in plan and isinstance(plan["menu"], list):
            settings_size = None
            for day in plan["menu"]:
                if isinstance(day, dict):
                    if "servings" not in day or day.get("servings") is None:
                        if settings_size is None:
                            try:
                                s = get_settings(household)
                                settings_size = s.get("base_headcount", 2)
                            except Exception:
                                settings_size = 2
                        day["servings"] = day.get("headcount") or settings_size
                    if "is_overridden" not in day:
                        day["is_overridden"] = False
                    day["headcount"] = day["servings"]
                    day["servings_override"] = day["is_overridden"]
        return plan_id, created_at, plan
    return None, None, None


def update_saved_plan(plan_id: int, plan_dict: dict, household: Optional[str] = None):
    _, _, plan_tbl = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(
        f"UPDATE {plan_tbl} SET plan_json = ? WHERE id = ?",
        (json.dumps(plan_dict), plan_id),
    )
    conn.commit()
    conn.close()


def clear_saved_plans(household: Optional[str] = None):
    """Delete all rows from saved_plans, completely clearing the current menu and shopping list."""
    _, _, plan_tbl = db.get_table_names(household)
    db.init_db(household)
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(f"DELETE FROM {plan_tbl}")
    conn.commit()
    conn.close()



# ==========================================
# GEMINI AI FUNCTIONS (ported from app.py)
# All Gemini calls raise a friendly RuntimeError on any API/parse failure;
# the API layer converts those to HTTP 502 with the friendly message.
# ==========================================


def get_client(api_key: str):
    return genai.Client(api_key=api_key)


def _generate_structured(
    keys_or_key,
    contents,
    schema,
    temperature: float,
    what: str,
    item_count: Optional[int] = None,
    action: Optional[str] = None,
):
    """Shared structured-output call routed through the AI provider fallback chain."""
    if isinstance(keys_or_key, ProviderKeys):
        keys = keys_or_key
    elif isinstance(keys_or_key, str):
        keys = ProviderKeys(gemini_key=keys_or_key)
    else:
        keys = ProviderKeys()
    return generate_structured_with_fallback(
        keys,
        contents,
        schema,
        temperature,
        what,
        item_count=item_count,
        action=action,
    )


BARE_GENERIC_NAMES = {
    "frozen food",
    "food",
    "meat",
    "vegetable",
    "vegetables",
    "fruit",
    "dairy",
    "snack",
    "snacks",
    "drink",
    "beverage",
    "pantry item",
    "grocery",
    "groceries",
    "item",
}


def normalize_scan_location_tag(location: str) -> str:
    loc_clean = (location or "").strip()
    if "pantry" in loc_clean.lower() or "cupboard" in loc_clean.lower():
        return "Pantry"
    if "freezer" in loc_clean.lower():
        return "Freezer"
    if "fridge" in loc_clean.lower():
        return "Fridge"
    return loc_clean or "Pantry"


def sanitize_scan_items(items: List[DetectedItem], location: str) -> List[DetectedItem]:
    loc_tag = normalize_scan_location_tag(location)
    generic_idx = 0
    sanitized: List[DetectedItem] = []
    for it in items:
        clean_name = (it.item_name or "").strip()
        if clean_name.lower() in BARE_GENERIC_NAMES:
            generic_idx += 1
            old_name = it.item_name
            new_name = f"Unidentified {loc_tag} Item {generic_idx}"
            logger.info("Generic scan item renamed: '%s' -> '%s' (location=%s)", old_name, new_name, location)
            it.item_name = new_name
        sanitized.append(it)
    return sanitized


def analyze_storage_photo(keys, image: Image.Image, location: str) -> Tuple[List[DetectedItem], str]:
    prompt = f"""
    You are scanning a user's {location}.
    Identify all visible food items, ingredients, veggies, drinks, and seasonings.
    Estimate the remaining quantity and unit for each item as accurately as possible
    (e.g., 0.5 gallon, 2 cans, 12 oz, 1 bottle).

    NAMING RULES (STRICT ENFORCEMENT):
    NEVER use a bare category or storage word as an item name (examples banned: "Frozen Food", "Food", "Meat", "Vegetables", "Pantry Item"). Name items by, in order:
    1. Printed label/brand text visible on the packaging (e.g. "Campbell's Tomato Soup", "Eggo Waffles").
    2. The specific food you can see (e.g. "Frozen Mixed Vegetables", "Frozen Chicken Breasts", "Frozen Pizza").
    3. Only if genuinely indeterminate, a descriptive visible-form name (e.g. "Unidentified frozen item — green bag") so a human can rename it in review.

    ANTI-CONFABULATION & LEGIBILITY RULES (MANDATORY):
    - NEVER output a brand or product name unless its text is actually legible in the image. If a label is visible but unreadable, name the item by its visible kind only ("BBQ Sauce", not an invented brand).
    - NEVER claim contents you cannot see. Opaque, frosted, or sealed items whose contents aren't identifiable from visible text or clear appearance get a descriptive visible-form name (rule 3 of the hierarchy above) — never a guessed food, species, or cut. (A frosty bag is "Clear Bag with Frozen Meat", never "Frozen Pheasant".)
    - When torn between two specific readings, choose the LESS specific honest one.
    """
    parsed, served_by = _generate_structured(
        keys, [image, prompt], VisionInventoryResult, 0.2, f"scan your {location} photo"
    )
    sanitized_items = sanitize_scan_items(parsed.items, location)
    return sanitized_items, served_by


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
        inv_records = [
            {
                "n": r.get("item_name") or r.get("n", ""),
                "q": r.get("quantity") if "quantity" in r else r.get("q", 0.0),
                "u": r.get("unit") or r.get("u", ""),
                "l": r.get("location") or r.get("l", ""),
                "c": r.get("category") or r.get("c", ""),
            }
            for _, r in inventory_df.iterrows()
        ]

    rel_presets = settings.get("religious_presets", [])
    rel_section = build_religious_diet_prompt_section(rel_presets)

    prompt = f"""
    Build a practical, delicious 7-day meal plan and a consolidated shopping list.
    Current Season/Holiday Context: {active_theme}

    CURRENT KITCHEN INVENTORY (keys: n=item_name, q=quantity, u=unit, l=location, c=category):
    {json.dumps(inv_records, separators=(',', ':'))}

    DAILY HEADCOUNT SCHEDULE (Scale portion sizes and ingredient math to match each day):
    {json.dumps(daily_headcounts, indent=2)}

    HOUSEHOLD DIETARY RESTRICTIONS & MEDICAL NEEDS (STRICT ENFORCEMENT):
    - Selected Tags/Diets: {", ".join(settings["dietary_tags"])}
    - Specific Household Rules & Allergies: {settings["custom_diet_notes"]}

    {rel_section}

    DIETARY ENFORCEMENT RULES FOR THE AI:
    1. STRICTLY OBEY all dietary tags, medical restrictions, religious presets, and food allergies specified by the household above. Avoid all allergens and restricted ingredients completely across every meal.
    2. Provide clear step-by-step preparation instructions for breakfast, lunch, and dinner, including timings and recommendations for countertop appliances (like an air fryer or stovetop/oven).

    INSTRUCTIONS:
    1. Prioritize using existing fridge, freezer, and cupboard items before they spoil.
    2. Scale recipes accurately for the exact headcount on each day.
    3. For each day's `ingredients_to_deduct`, reference the exact `id` (`inventory_id`), `item_name`, `amount_used` (numeric), and matching `unit` from CURRENT KITCHEN INVENTORY. Set `cooked` to False.
    4. Compare total ingredients needed for the 7 days against CURRENT KITCHEN INVENTORY and generate a categorized Shopping List for missing items. Populate `numeric_quantity`, `unit`, `default_location`, and `inventory_category`. Set `purchased` to False.
    """

    return _generate_structured(
        keys,
        prompt,
        WeeklyPlanOutput,
        0.3,
        "build your weekly menu",
        item_count=len(inv_records),
        action="generate_plan",
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
    rel_presets = settings.get("religious_presets", [])
    rel_section = build_religious_diet_prompt_section(rel_presets)

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
       {rel_section}
       The replacement must strictly comply with all dietary tags, religious presets, and allergies.
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
    kitchen_name: Optional[str] = None
    religious_presets: List[str] = []


class HouseholdNameIn(BaseModel):
    name: Optional[str] = None
    kitchen_name: Optional[str] = None


class GeneratePlanRequest(BaseModel):
    daily_headcounts: Dict[str, int] = {}
    theme_name: Optional[str] = None


class CookRequest(BaseModel):
    day_index: int


class SwapMealRequest(BaseModel):
    day_index: int
    meal_slot: str  # 'breakfast', 'lunch', or 'dinner'


class DayServingsRequest(BaseModel):
    day_index: Optional[int] = None
    day: Optional[str] = None
    servings: Optional[int] = None
    reset: bool = False


class RestockRequest(BaseModel):
    indices: List[int] = []


class ReceiptPasteRequest(BaseModel):
    text: str = ""


# ---------- Helpers ----------


def require_ai_keys(request: Request) -> ProviderKeys:
    """Extract AI keys: Gemini, Groq, OpenRouter from headers or config."""
    keys = extract_provider_keys(request.headers)
    if not (keys.gemini_key or keys.groq_key or keys.openrouter_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "No AI API key configured on the server. Please enter your Gemini API key "
                "(or Groq/OpenRouter fallback key) in Settings."
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
        "ok": True,
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
def api_replace_inventory(
    items: List[InventoryItemIn],
    confirm_clear: bool = Query(False, alias="confirm_clear"),
):
    if len(items) == 0 and not confirm_clear:
        raise HTTPException(
            status_code=400,
            detail="Clearing inventory to empty requires explicit confirm_clear confirmation.",
        )
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
def api_clear_inventory(
    confirm_clear: bool = Query(False, alias="confirm_clear"),
):
    if not confirm_clear:
        raise HTTPException(
            status_code=400,
            detail="Clearing inventory requires explicit confirm_clear confirmation.",
        )
    clear_inventory()
    return {"inventory": []}


# ---------- Settings ----------


@app.get("/api/settings")
def api_get_settings():
    return get_settings()


@app.put("/api/settings")
def api_save_settings(body: SettingsIn):
    save_settings(
        body.base_headcount,
        body.dietary_tags,
        body.custom_diet_notes,
        body.essential_staples,
        kitchen_name=body.kitchen_name,
        religious_presets=body.religious_presets,
    )
    return get_settings()


@app.get("/api/household/name")
def api_get_household_name():
    name = db.get_kitchen_name()
    return {"kitchen_name": name or None}


@app.post("/api/household/name")
def api_set_household_name(body: HouseholdNameIn):
    raw = (body.kitchen_name or body.name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Name cannot be empty.")
    display_name = raw if raw.endswith("'s Kitchen") else f"{raw}'s Kitchen"
    saved_name, created = db.set_kitchen_name(display_name)
    return {"kitchen_name": saved_name, "created": created}


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
    try:
        image = await load_image(file)
        try:
            detected, served_by = ai_call(analyze_storage_photo, keys, image, location)
            add_inventory_items(location, detected)
            return {
                "added": len(detected),
                "items": [d.model_dump() for d in detected],
                "inventory": get_inventory(),
                "served_by": served_by,
            }
        finally:
            if hasattr(image, "close"):
                try:
                    image.close()
                except Exception:
                    pass
    finally:
        try:
            await file.close()
        except Exception:
            pass


# ---------- Weekly plan ----------


@app.post("/api/plan/generate")
def api_generate_plan(body: GeneratePlanRequest, request: Request):
    keys = require_ai_keys(request)
    settings = get_settings()
    household_size = settings.get("base_headcount", 2)
    days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    # Part 2 Rule 1 & 4: Fresh weekly generation starts all 7 days at current household size with no overrides
    daily_headcounts = {d: household_size for d in days_list}
    theme = body.theme_name or get_auto_theme_name()
    plan_output, served_by = ai_call(
        generate_weekly_menu_and_list,
        keys,
        get_inventory_df(),
        settings,
        daily_headcounts,
        theme,
    )
    plan_dict = plan_output.model_dump()
    rel_presets = settings.get("religious_presets", [])

    # Deterministic verification of generated plan meals
    if rel_presets:
        for day in plan_dict.get("menu", []):
            day_name = day.get("day", "")
            for slot in ("breakfast", "lunch", "dinner"):
                meal = day.get(slot)
                if meal and isinstance(meal, dict):
                    ing_names = meal.get("prep_steps", [])
                    is_violation, reason = check_meal_violation(
                        dish_name=meal.get("dish_name", ""),
                        ingredients=ing_names,
                        active_presets=rel_presets,
                        day_name=day_name,
                    )
                    if is_violation:
                        raise HTTPException(
                            status_code=502,
                            detail=f"Generated plan violated dietary policy: {reason}. Please re-generate."
                        )

    for d in plan_dict.get("menu", []):
        d["servings"] = household_size
        d["headcount"] = household_size
        d["is_overridden"] = False
        d["servings_override"] = False
    save_plan(plan_dict)
    payload = latest_plan_payload()
    payload["served_by"] = served_by
    return payload


@app.get("/api/plan/latest")
def api_latest_plan():
    return latest_plan_payload()


@app.delete("/api/plan")
def api_clear_plan():
    """Clear the active menu plan and derived shopping list without touching inventory or settings."""
    clear_saved_plans()
    return latest_plan_payload()


@app.post("/api/plan/day-servings")
@app.post("/api/plan/servings")
def api_update_day_servings(body: DayServingsRequest):
    """Update or reset a single day's servings for the current week's plan (Part 2 Rule 2 & 5)."""
    plan_id, _created_at, plan = get_latest_plan()
    if plan_id is None or plan is None:
        raise HTTPException(status_code=404, detail="No plan generated yet.")
    menu = plan.get("menu", [])
    target_idx = None
    if body.day_index is not None and 0 <= body.day_index < len(menu):
        target_idx = body.day_index
    elif body.day:
        for idx, d in enumerate(menu):
            if d.get("day", "").lower().strip() == body.day.lower().strip():
                target_idx = idx
                break
    if target_idx is None:
        raise HTTPException(status_code=400, detail="Invalid day or day_index.")

    day_obj = menu[target_idx]
    settings = get_settings()
    household_size = settings.get("base_headcount", 2)
    old_servings = day_obj.get("servings") or day_obj.get("headcount") or household_size

    if body.reset:
        new_servings = household_size
        is_overridden = False
    else:
        if body.servings is None:
            raise HTTPException(status_code=400, detail="Servings must be specified unless reset is True.")
        if body.servings < 1 or body.servings > 12:
            raise HTTPException(status_code=400, detail="Servings must be between 1 and 12.")
        new_servings = body.servings
        is_overridden = True

    if new_servings != old_servings:
        scale_day_ingredients_and_shopping(plan, day_obj.get("day", ""), old_servings, new_servings)

    day_obj["servings"] = new_servings
    day_obj["headcount"] = new_servings
    day_obj["is_overridden"] = is_overridden
    day_obj["servings_override"] = is_overridden

    update_saved_plan(plan_id, plan)
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
    settings = get_settings()
    headcount = day_obj.get("servings") or day_obj.get("headcount") or settings.get("base_headcount", 2)

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

    # Deterministic religious diet preset verification
    rel_presets = settings.get("religious_presets", [])
    if rel_presets:
        used_ing_names = [i.item_name for i in swap_res.ingredients_used] + swap_res.prep_steps
        is_violation, reason = check_meal_violation(
            dish_name=swap_res.dish_name,
            ingredients=used_ing_names,
            active_presets=rel_presets,
            day_name=day_obj.get("day", ""),
        )
        if is_violation:
            return {
                "success": False,
                "message": f"Swap blocked by dietary policy: {reason}",
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
    try:
        for upload in uploads:
            image = await load_image(upload)
            try:
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
            finally:
                if hasattr(image, "close"):
                    try:
                        image.close()
                    except Exception:
                        pass
    finally:
        for upload in uploads:
            try:
                await upload.close()
            except Exception:
                pass
    return _receipt_response(store_name, merged, last_served_by)


@app.post("/api/receipt/scan-pdf")
async def api_scan_receipt_pdf(request: Request, file: UploadFile = File(...)):
    """Import an online grocery order from a PDF (order confirmation/receipt)."""
    keys = require_ai_keys(request)
    try:
        pdf_bytes = await file.read()
        if not pdf_bytes:
            raise HTTPException(status_code=400, detail="That PDF file was empty.")
        scan_res, served_by = ai_call(
            analyze_receipt_pdf, keys, pdf_bytes, _active_shopping_list()
        )
        return _receipt_response(scan_res.store_name, scan_res.items, served_by)
    finally:
        try:
            await file.close()
        except Exception:
            pass


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
    try:
        cfg = phone_access.load_config()
        if "provider_keys" not in cfg or not isinstance(cfg["provider_keys"], dict):
            cfg["provider_keys"] = {}
        cfg["provider_keys"][provider] = key
        phone_access.save_config(cfg)
    except Exception:
        pass
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
    try:
        cfg = phone_access.load_config()
        if "provider_keys" not in cfg or not isinstance(cfg["provider_keys"], dict):
            cfg["provider_keys"] = {}
        cfg["provider_keys"]["gemini"] = api_key
        phone_access.save_config(cfg)
    except Exception:
        pass
    return {"ok": True, "message": "Key works! You're connected."}


# ---------- Provider Resilience & Health Watchdog ----------

class SaveProviderModelRequest(BaseModel):
    provider: str
    model: str


@app.get("/api/providers/status")
def api_get_providers_status(request: Request):
    """Retrieve persisted status of AI providers. Connected phones receive sanitized health words."""
    client_is_phone = False
    if request.client and request.client.host not in phone_access.LOOPBACK_HOSTS:
        client_is_phone = True
    return get_providers_status(client_is_phone=client_is_phone)


@app.post("/api/providers/status/check")
def api_check_providers_status(request: Request):
    """Run an on-demand probe of all configured providers."""
    client_is_phone = False
    if request.client and request.client.host not in phone_access.LOOPBACK_HOSTS:
        client_is_phone = True
    probe_all_providers()
    return get_providers_status(client_is_phone=client_is_phone)


@app.post("/api/providers/model")
def api_save_provider_model(body: SaveProviderModelRequest, request: Request):
    """Update a provider's model ID with validation probe."""
    ok, msg, probe_data = save_provider_model_with_validation(
        provider=body.provider,
        candidate_model=body.model,
    )
    if not ok:
        return {"ok": False, "error": msg, "details": probe_data}
    return {"ok": True, "provider": body.provider, "model": body.model, "message": msg}


@app.on_event("startup")
async def startup_resilience_probe():
    """Run initial probe at startup and launch 24h background watchdog loop."""
    if os.environ.get("PANTRY_TEST_MODE"):
        return
    import asyncio
    asyncio.create_task(_background_probe_loop())


async def _background_probe_loop():
    import asyncio
    try:
        await asyncio.to_thread(probe_all_providers)
    except Exception:
        pass
    while True:
        try:
            await asyncio.sleep(86400)
            await asyncio.to_thread(probe_all_providers)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ---------- Static PWA (mounted last so /api routes win) ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

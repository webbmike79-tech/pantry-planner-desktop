"""
Pantry-to-Menu Planner — DESKTOP edition (FastAPI backend + desktop web UI).

This is the desktop sibling of the mobile API/PWA version in
~/workspace/projects/pantry-planner/api/. The database layer, Pydantic
schemas, Gemini prompts, business logic, and REST endpoints are identical
to the mobile backend (itself a faithful port of the original Streamlit
app.py, including the 2026-10-14 fixes: inventory IDs are preserved on bulk
save, and all Gemini calls go through _generate_structured, which raises
friendly RuntimeErrors instead of raw tracebacks).

This project is fully standalone: it uses its OWN SQLite database
(pantry_planner.db in THIS project's root), separate from the mobile
project's database. Set the PANTRY_DB_FILE environment variable to override
the DB location (useful for testing).

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
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile

from google import genai
from google.genai import types

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



# ==========================================
# GEMINI AI FUNCTIONS (ported from app.py)
# All Gemini calls raise a friendly RuntimeError on any API/parse failure;
# the API layer converts those to HTTP 502 with the friendly message.
# ==========================================


def get_client(api_key: str):
    return genai.Client(api_key=api_key)


def _generate_structured(api_key: str, contents, schema, temperature: float, what: str):
    """Shared Gemini structured-output call with friendly error wrapping."""
    try:
        client = get_client(api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
            ),
        )
    except Exception as e:
        raise RuntimeError(f"Gemini could not {what}. API error: {e}") from e
    if not getattr(response, "text", None):
        raise RuntimeError(
            f"Gemini returned an empty response while trying to {what}. Please try again."
        )
    try:
        return schema.model_validate_json(response.text)
    except Exception as e:
        raise RuntimeError(
            f"Gemini returned data in an unexpected format while trying to {what}. Please try again. ({e})"
        ) from e


def analyze_storage_photo(api_key: str, image: Image.Image, location: str) -> List[DetectedItem]:
    prompt = f"""
    You are scanning a user's {location}.
    Identify all visible food items, ingredients, veggies, drinks, and seasonings.
    Estimate the remaining quantity and unit for each item as accurately as possible
    (e.g., 0.5 gallon, 2 cans, 12 oz, 1 bottle).
    """
    parsed = _generate_structured(
        api_key, [image, prompt], VisionInventoryResult, 0.2, f"scan your {location} photo"
    )
    return parsed.items


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
    api_key: str, image: Image.Image, shopping_list: list
) -> ReceiptScanResult:
    prompt = _receipt_prompt(shopping_list, "a grocery store receipt image")
    return _generate_structured(
        api_key, [image, prompt], ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def analyze_receipt_pdf(
    api_key: str, pdf_bytes: bytes, shopping_list: list
) -> ReceiptScanResult:
    prompt = _receipt_prompt(
        shopping_list, "a grocery store receipt or online grocery order document (PDF)"
    )
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    return _generate_structured(
        api_key, [pdf_part, prompt], ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def analyze_receipt_text(
    api_key: str, text: str, shopping_list: list
) -> ReceiptScanResult:
    prompt = _receipt_prompt(
        shopping_list,
        "pasted text from a grocery store receipt or online grocery order confirmation",
    )
    contents = f"{prompt}\nRECEIPT / ORDER TEXT:\n{text}"
    return _generate_structured(
        api_key, contents, ReceiptScanResult, 0.1, "read your grocery receipt"
    )


def generate_weekly_menu_and_list(
    api_key: str,
    inventory_df: pd.DataFrame,
    settings: dict,
    daily_headcounts: dict,
    active_theme: str,
) -> WeeklyPlanOutput:
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
        api_key, prompt, WeeklyPlanOutput, 0.3, "build your weekly menu"
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


class RestockRequest(BaseModel):
    indices: List[int] = []


class ReceiptPasteRequest(BaseModel):
    text: str = ""


# ---------- Helpers ----------


def require_gemini_key(request: Request) -> str:
    """Gemini key: X-Gemini-Key header first, else GEMINI_API_KEY env var."""
    key = request.headers.get("x-gemini-key") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Gemini API key available. Enter your key in the app "
                "(Diet tab) or set the GEMINI_API_KEY environment variable."
            ),
        )
    return key


def gemini_call(fn, *args):
    """Run a Gemini function, converting friendly RuntimeErrors to HTTP 502."""
    try:
        return fn(*args)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


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
    return {"status": "ok"}


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
    api_key = require_gemini_key(request)
    image = await load_image(file)
    detected = gemini_call(analyze_storage_photo, api_key, image, location)
    add_inventory_items(location, detected)
    return {
        "added": len(detected),
        "items": [d.model_dump() for d in detected],
        "inventory": get_inventory(),
    }


# ---------- Weekly plan ----------


@app.post("/api/plan/generate")
def api_generate_plan(body: GeneratePlanRequest, request: Request):
    api_key = require_gemini_key(request)
    settings = get_settings()
    theme = body.theme_name or get_auto_theme_name()
    plan_output = gemini_call(
        generate_weekly_menu_and_list,
        api_key,
        get_inventory_df(),
        settings,
        body.daily_headcounts,
        theme,
    )
    save_plan(plan_output.model_dump())
    return latest_plan_payload()


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


def _receipt_response(store_name: str, items: List[ReceiptLineItem]):
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
    }


@app.post("/api/receipt/scan")
async def api_scan_receipt(request: Request):
    """Scan one or more receipt photos/screenshots (online-order imports).

    Accepts repeated `files` fields (and the legacy single `file` field).
    Each image is analyzed separately; the items are then merged and deduped
    by normalized (item_name, quantity, unit) so overlapping screenshots of
    the same order don't double-count, before one inventory update pass.
    """
    api_key = require_gemini_key(request)
    form = await request.form()
    uploads: List[UploadFile] = []
    for field_name, value in form.multi_items():
        # NOTE: form parsing yields starlette UploadFiles; FastAPI's UploadFile
        # is a subclass of it, so test against the starlette base class.
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
    for upload in uploads:
        image = await load_image(upload)
        scan_res = gemini_call(analyze_grocery_receipt, api_key, image, active_shop_list)
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
    return _receipt_response(store_name, merged)


@app.post("/api/receipt/scan-pdf")
async def api_scan_receipt_pdf(request: Request, file: UploadFile = File(...)):
    """Import an online grocery order from a PDF (order confirmation/receipt)."""
    api_key = require_gemini_key(request)
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="That PDF file was empty.")
    scan_res = gemini_call(
        analyze_receipt_pdf, api_key, pdf_bytes, _active_shopping_list()
    )
    return _receipt_response(scan_res.store_name, scan_res.items)


@app.post("/api/receipt/paste")
def api_paste_receipt(body: ReceiptPasteRequest, request: Request):
    """Import an online grocery order from pasted confirmation text/email."""
    api_key = require_gemini_key(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400, detail="Paste your order or receipt text first."
        )
    scan_res = gemini_call(
        analyze_receipt_text, api_key, text, _active_shopping_list()
    )
    return _receipt_response(scan_res.store_name, scan_res.items)


# ---------- Gemini key validation (per-user bring-your-own-key) ----------


@app.post("/api/validate-key")
def api_validate_key(request: Request):
    """Validate a user-supplied Gemini key with one tiny test call.

    The key arrives in the X-Gemini-Key header, is used for a single
    minimal generateContent call against Google, and is never stored
    or logged by the backend.
    """
    api_key = request.headers.get("x-gemini-key") or ""
    if not api_key:
        raise HTTPException(
            status_code=400, detail="No key provided. Paste your Gemini API key first."
        )
    try:
        client = get_client(api_key)
        client.models.generate_content(
            model="gemini-3.8-flash",
            contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=16, temperature=0),
        )
    except Exception as e:
        return {"ok": False, "error": f"That key didn't work — Google rejected it. ({e})"}
    return {"ok": True, "message": "Key works! You're connected."}


# ---------- Static PWA (mounted last so /api routes win) ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

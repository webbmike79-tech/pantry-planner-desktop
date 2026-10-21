"""
test_religious_diet.py - Tests for Religious Diet Presets ("Faith & tradition") - Desktop Repo.

Covers:
1. One rule-mapping test per preset (bundle contents & description)
2. Generation context includes active rules and excludes inactive ones
3. Kosher same-meal violation blocked in swap + allowed when dairy-only
4. Lent: Friday dinner with chicken blocked/swapped out in enforcement, Saturday chicken fine, Friday fish fine
5. Stacking: Halal + Diabetic tag both enforced in one generation context
6. Presets-off regression: with NO presets on, generation & swap behavior unchanged
7. Settings persistence round-trip (saving & reading religious_presets)
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import test_isolation

from religious_diet import (
    PRESETS_INFO,
    HONESTY_NOTE,
    check_meal_violation,
    build_religious_diet_prompt_section,
)
import db
from main import (
    app,
    get_settings,
    save_settings,
    WeeklyPlanOutput,
    MealSwapOutput,
    DayMealPlan,
    MealDetail,
    UsedIngredient,
    ShoppingListItem,
    generate_weekly_menu_and_list,
    swap_single_meal,
)
from starlette.testclient import TestClient
import pandas as pd


class TestReligiousDietPresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_rel_diet.db")
        os.environ["PANTRY_DB_FILE"] = self.db_path
        db._initialized_households.clear()
        db.init_db(force=True)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # 1. One rule-mapping test per preset (bundle contents)
    def test_preset_rule_mapping_halal(self):
        info = PRESETS_INFO["Halal"]
        self.assertIn("pork", info["exclusions_summary"].lower())
        self.assertIn("alcohol", info["exclusions_summary"].lower())

    def test_preset_rule_mapping_kosher(self):
        info = PRESETS_INFO["Kosher"]
        self.assertIn("pork", info["exclusions_summary"].lower())
        self.assertIn("shellfish", info["exclusions_summary"].lower())
        self.assertIn("same meal", info["exclusions_summary"].lower())

    def test_preset_rule_mapping_hindu(self):
        info = PRESETS_INFO["Hindu"]
        self.assertIn("beef", info["exclusions_summary"].lower())
        self.assertIn("veal", info["exclusions_summary"].lower())

    def test_preset_rule_mapping_jain(self):
        info = PRESETS_INFO["Jain"]
        self.assertIn("meat", info["exclusions_summary"].lower())
        self.assertIn("eggs", info["exclusions_summary"].lower())
        self.assertIn("root vegetables", info["exclusions_summary"].lower())
        self.assertIn("onion", info["exclusions_summary"].lower())
        self.assertIn("garlic", info["exclusions_summary"].lower())

    def test_preset_rule_mapping_latter_day_saint(self):
        info = PRESETS_INFO["Latter-day Saint"]
        self.assertIn("alcohol", info["exclusions_summary"].lower())
        self.assertIn("coffee", info["exclusions_summary"].lower())
        self.assertIn("tea from the tea plant", info["exclusions_summary"].lower())

    def test_preset_rule_mapping_lent_fridays(self):
        info = PRESETS_INFO["Lent (Fridays)"]
        self.assertIn("land meat", info["exclusions_summary"].lower())
        self.assertIn("friday", info["exclusions_summary"].lower())
        self.assertIn("seafood are allowed", info["exclusions_summary"].lower())

    # 2. Generation context includes active rules and excludes inactive ones
    def test_generation_context_includes_active_and_excludes_inactive(self):
        section = build_religious_diet_prompt_section(["Halal", "Hindu"])
        self.assertIn("Halal:", section)
        self.assertIn("Hindu:", section)
        self.assertNotIn("Kosher:", section)
        self.assertNotIn("Jain:", section)
        self.assertNotIn("Latter-day Saint:", section)
        self.assertNotIn("Lent (Fridays):", section)
        self.assertIn(HONESTY_NOTE, section)

        empty_section = build_religious_diet_prompt_section([])
        self.assertEqual(empty_section, "")

    # 3. Kosher same-meal violation blocked in swap + allowed when dairy-only
    def test_kosher_same_meal_enforcement(self):
        # Meat + Dairy together in same meal -> VIOLATION
        is_violation, reason = check_meal_violation(
            dish_name="Cheeseburger with Beef Patty",
            ingredients=["ground beef", "cheddar cheese", "bun"],
            active_presets=["Kosher"],
            day_name="Monday",
        )
        self.assertTrue(is_violation)
        self.assertIn("meat/poultry", reason)
        self.assertIn("dairy", reason)

        # Dairy only meal -> ALLOWED
        is_violation, _ = check_meal_violation(
            dish_name="Greek Yogurt Parfait with Honey",
            ingredients=["greek yogurt", "honey", "berries"],
            active_presets=["Kosher"],
            day_name="Monday",
        )
        self.assertFalse(is_violation)

        # Meat only meal (no dairy) -> ALLOWED
        is_violation, _ = check_meal_violation(
            dish_name="Roasted Chicken and Potatoes",
            ingredients=["chicken breast", "olive oil", "rosemary"],
            active_presets=["Kosher"],
            day_name="Monday",
        )
        self.assertFalse(is_violation)

        # Shellfish in Kosher -> VIOLATION
        is_violation, reason = check_meal_violation(
            dish_name="Shrimp Scampi",
            ingredients=["shrimp", "pasta", "garlic", "butter"],
            active_presets=["Kosher"],
            day_name="Tuesday",
        )
        self.assertTrue(is_violation)
        self.assertIn("shellfish", reason)

    # 4. Lent: Friday dinner with chicken blocked in enforcement, Saturday chicken fine, Friday fish fine
    def test_lent_friday_enforcement(self):
        # Friday chicken -> VIOLATION
        is_violation, reason = check_meal_violation(
            dish_name="Grilled Chicken Dinner",
            ingredients=["chicken breast", "broccoli"],
            active_presets=["Lent (Fridays)"],
            day_name="Friday",
        )
        self.assertTrue(is_violation)
        self.assertIn("land meat", reason)

        # Saturday chicken -> ALLOWED
        is_violation, _ = check_meal_violation(
            dish_name="Grilled Chicken Dinner",
            ingredients=["chicken breast", "broccoli"],
            active_presets=["Lent (Fridays)"],
            day_name="Saturday",
        )
        self.assertFalse(is_violation)

        # Friday fish -> ALLOWED
        is_violation, _ = check_meal_violation(
            dish_name="Baked Salmon with Asparagus",
            ingredients=["salmon", "asparagus", "lemon"],
            active_presets=["Lent (Fridays)"],
            day_name="Friday",
        )
        self.assertFalse(is_violation)

    # 5. Stacking (e.g. Halal + existing diabetic tag both enforced in one generation context)
    @patch("main._generate_structured")
    def test_stacking_halal_and_diabetic_in_generation(self, mock_gen):
        mock_output = WeeklyPlanOutput(
            menu=[
                DayMealPlan(
                    day="Monday",
                    headcount=2,
                    servings=2,
                    breakfast=MealDetail(dish_name="Oatmeal with Walnuts", prep_steps=["Boil oats"], cook_time_minutes=10),
                    lunch=MealDetail(dish_name="Chicken Salad", prep_steps=["Chop chicken"], cook_time_minutes=15),
                    dinner=MealDetail(dish_name="Baked Cod", prep_steps=["Bake cod"], cook_time_minutes=20),
                    ingredients_to_deduct=[],
                )
            ],
            shopping_list=[],
            nutrition_notes="Low glycemic, halal compliant",
        )
        mock_gen.return_value = (mock_output, "gemini")

        settings = {
            "base_headcount": 2,
            "dietary_tags": ["Diabetic / Low Glycemic"],
            "custom_diet_notes": "Strict sugar control",
            "religious_presets": ["Halal"],
        }
        res, served_by = generate_weekly_menu_and_list(
            keys=None,
            inventory_df=pd.DataFrame(),
            settings=settings,
            daily_headcounts={"Monday": 2},
            active_theme="Standard",
        )
        self.assertEqual(served_by, "gemini")
        call_args = mock_gen.call_args[0]
        prompt_text = call_args[1]
        self.assertIn("Diabetic / Low Glycemic", prompt_text)
        self.assertIn("Halal:", prompt_text)
        self.assertIn("No pork", prompt_text)

    # 6. Presets-off regression (existing behavior unchanged)
    @patch("main._generate_structured")
    def test_presets_off_regression(self, mock_gen):
        mock_output = WeeklyPlanOutput(
            menu=[],
            shopping_list=[],
            nutrition_notes="Standard nutrition notes",
        )
        mock_gen.return_value = (mock_output, "gemini")

        settings = {
            "base_headcount": 2,
            "dietary_tags": ["Balanced & Whole Foods"],
            "custom_diet_notes": "",
            "religious_presets": [],
        }
        res, served_by = generate_weekly_menu_and_list(
            keys=None,
            inventory_df=pd.DataFrame(),
            settings=settings,
            daily_headcounts={},
            active_theme="Standard",
        )
        call_args = mock_gen.call_args[0]
        prompt_text = call_args[1]
        self.assertNotIn("FAITH & TRADITION PRESETS", prompt_text)

    # 7. Settings persistence round-trip
    def test_settings_persistence_round_trip(self):
        save_settings(
            base_headcount=4,
            dietary_tags=["Gluten-Free"],
            custom_notes="No artificial dyes",
            staples="Salt, Pepper",
            religious_presets=["Kosher", "Lent (Fridays)"],
        )
        loaded = get_settings()
        self.assertEqual(loaded["base_headcount"], 4)
        self.assertEqual(loaded["dietary_tags"], ["Gluten-Free"])
        self.assertEqual(loaded["custom_diet_notes"], "No artificial dyes")
        self.assertEqual(loaded["essential_staples"], "Salt, Pepper")
        self.assertEqual(loaded["religious_presets"], ["Kosher", "Lent (Fridays)"])

        res = self.client.put(
            "/api/settings",
            json={
                "base_headcount": 3,
                "dietary_tags": ["Low Carb / Keto"],
                "custom_diet_notes": "",
                "essential_staples": "",
                "religious_presets": ["Halal"],
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["religious_presets"], ["Halal"])
        self.assertEqual(data["base_headcount"], 3)


if __name__ == "__main__":
    unittest.main()

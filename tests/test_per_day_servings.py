import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

os.environ["PANTRY_TEST_MODE"] = "1"

import main
import db
from main import (
    save_settings,
    get_settings,
    save_plan,
    get_latest_plan,
    clear_saved_plans,
    scale_day_ingredients_and_shopping,
    round_scaled_quantity,
    scale_amount_needed_str,
    DayMealPlan,
    MealDetail,
    UsedIngredient,
    ShoppingListItem,
    WeeklyPlanOutput,
    MealSwapOutput,
    DayServingsRequest,
    api_update_day_servings,
)


class TestPerDayServings(unittest.TestCase):
    def setUp(self):
        os.environ["MULTI_HOUSEHOLD"] = "1"
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_servings.db")
        os.environ["PANTRY_DB_FILE"] = self.db_path
        db._initialized_households.clear()
        self.hh_token = db.current_household_var.set("Test Servings Household")
        db.init_db("Test Servings Household", force=True)

    def tearDown(self):
        if "MULTI_HOUSEHOLD" in os.environ:
            del os.environ["MULTI_HOUSEHOLD"]
        try:
            db.current_household_var.reset(self.hh_token)
        except Exception:
            pass
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _sample_plan(self, default_headcount=3):
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        menu = []
        for d in days:
            menu.append({
                "day": d,
                "headcount": default_headcount,
                "servings": default_headcount,
                "is_overridden": False,
                "servings_override": False,
                "breakfast": {"dish_name": f"{d} Breakfast", "prep_steps": ["Cook"], "cook_time_minutes": 15},
                "lunch": {"dish_name": f"{d} Lunch", "prep_steps": ["Assemble"], "cook_time_minutes": 10},
                "dinner": {"dish_name": f"{d} Dinner", "prep_steps": ["Roast"], "cook_time_minutes": 45},
                "ingredients_to_deduct": [
                    {"inventory_id": 1, "item_name": "Chicken Broth", "amount_used": 1.0, "unit": "cup"},
                    {"inventory_id": 2, "item_name": "Eggs", "amount_used": 2.0, "unit": "egg"},
                ],
                "cooked": False,
            })
        shopping_list = [
            {
                "item_name": "Pumpkin Puree",
                "category": "Pantry & Dairy",
                "amount_needed": "1 can",
                "numeric_quantity": 1.0,
                "unit": "can",
                "default_location": "Cupboard / Pantry",
                "inventory_category": "Pantry/Dry",
                "reason": "For Thursday Dinner (Pumpkin Pie)",
                "purchased": False,
            },
            {
                "item_name": "Turkey Breast",
                "category": "Proteins",
                "amount_needed": "1.5 lbs",
                "numeric_quantity": 1.5,
                "unit": "lbs",
                "default_location": "Fridge",
                "inventory_category": "Protein",
                "reason": "For Thursday Dinner (Thanksgiving Feast)",
                "purchased": False,
            },
            {
                "item_name": "Rolled Oats",
                "category": "Pantry & Dairy",
                "amount_needed": "2 cups",
                "numeric_quantity": 2.0,
                "unit": "cups",
                "default_location": "Cupboard / Pantry",
                "inventory_category": "Pantry/Dry",
                "reason": "For Monday Breakfast (Oatmeal)",
                "purchased": False,
            },
            {
                "item_name": "Fresh Parsley",
                "category": "Veggies & Produce",
                "amount_needed": "a pinch",
                "numeric_quantity": 1.0,
                "unit": "pinch",
                "default_location": "Fridge",
                "inventory_category": "Produce/Veggies",
                "reason": "For Thursday Dinner garnish",
                "purchased": False,
            },
        ]
        return {
            "menu": menu,
            "shopping_list": shopping_list,
            "nutrition_notes": "Balanced nutritious meals",
        }

    # Test 1: Household 3 -> generated plan: all 7 days serve 3, no overrides.
    def test_household_3_generated_plan_all_7_days_serve_3_no_overrides(self):
        save_settings(3, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        s = get_settings()
        self.assertEqual(s["base_headcount"], 3)

        plan = self._sample_plan(default_headcount=3)
        plan_id = save_plan(plan)
        self.assertIsNotNone(plan_id)

        _, _, loaded = get_latest_plan()
        self.assertEqual(len(loaded["menu"]), 7)
        for day in loaded["menu"]:
            self.assertEqual(day["servings"], 3)
            self.assertEqual(day["headcount"], 3)
            self.assertFalse(day["is_overridden"])
            self.assertFalse(day.get("servings_override", False))

    # Test 2: Edit Thanksgiving-style day to 7 -> only that day changes; household still 3; quantities rescale 3->7; other days byte-identical; shopping list reflects that day's change only.
    def test_edit_thanksgiving_day_to_7_rescales_quantities_and_shopping_list(self):
        save_settings(3, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        plan = self._sample_plan(default_headcount=3)
        plan_id = save_plan(plan)

        # Snapshot Monday (other day) and Rolled Oats before edit
        _, _, pre_edit = get_latest_plan()
        monday_pre = [d for d in pre_edit["menu"] if d["day"] == "Monday"][0]
        oats_pre = [i for i in pre_edit["shopping_list"] if i["item_name"] == "Rolled Oats"][0]

        # Edit Thursday (day_index=3) to 7
        req = DayServingsRequest(day="Thursday", servings=7)
        updated_payload = api_update_day_servings(req)
        updated_plan = updated_payload["plan"]

        # Household setting still 3
        s = get_settings()
        self.assertEqual(s["base_headcount"], 3)

        # Thursday changed to 7 and marked overridden
        thursday = [d for d in updated_plan["menu"] if d["day"] == "Thursday"][0]
        self.assertEqual(thursday["servings"], 7)
        self.assertEqual(thursday["headcount"], 7)
        self.assertTrue(thursday["is_overridden"])

        # Check Thursday ingredients rescale 3 -> 7 (ratio = 7/3 = 2.3333333333333335)
        # Chicken Broth (fractional, cup): 1.0 * (7/3) = 2.3333 -> nearest 0.25 is 2.25
        broth = [i for i in thursday["ingredients_to_deduct"] if i["item_name"] == "Chicken Broth"][0]
        self.assertEqual(broth["amount_used"], 2.25)

        # Eggs (countable whole, egg): 2.0 * (7/3) = 4.6667 -> round UP to 5.0
        eggs = [i for i in thursday["ingredients_to_deduct"] if i["item_name"] == "Eggs"][0]
        self.assertEqual(eggs["amount_used"], 5.0)

        # Other days are byte-identical
        monday_post = [d for d in updated_plan["menu"] if d["day"] == "Monday"][0]
        self.assertEqual(monday_post, monday_pre)
        self.assertEqual(monday_post["servings"], 3)
        self.assertFalse(monday_post["is_overridden"])

        # Shopping list: Thursday items scaled, Monday items byte-identical
        pumpkin = [i for i in updated_plan["shopping_list"] if i["item_name"] == "Pumpkin Puree"][0]
        # Countable can: 1.0 * (7/3) = 2.3333 -> round UP to 3.0
        self.assertEqual(pumpkin["numeric_quantity"], 3.0)
        self.assertEqual(pumpkin["amount_needed"], "3 can")

        turkey = [i for i in updated_plan["shopping_list"] if i["item_name"] == "Turkey Breast"][0]
        # Fractional lbs: 1.5 * (7/3) = 3.5 -> nearest 0.25 is 3.5
        self.assertEqual(turkey["numeric_quantity"], 3.5)
        self.assertEqual(turkey["amount_needed"], "3.5 lbs")

        # Free text with no number left unchanged
        parsley = [i for i in updated_plan["shopping_list"] if i["item_name"] == "Fresh Parsley"][0]
        self.assertEqual(parsley["amount_needed"], "a pinch")

        # Monday item in shopping list is byte-identical
        oats_post = [i for i in updated_plan["shopping_list"] if i["item_name"] == "Rolled Oats"][0]
        self.assertEqual(oats_post, oats_pre)

    # Test 3: Change household 3->4 mid-week -> all non-overridden days become 4 and rescale; overridden day stays 7.
    def test_change_household_midweek_updates_non_overridden_days_preserves_overridden(self):
        save_settings(3, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        plan = self._sample_plan(default_headcount=3)
        save_plan(plan)

        # Override Thursday to 7
        api_update_day_servings(DayServingsRequest(day="Thursday", servings=7))

        # Change household to 4 mid-week
        save_settings(4, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        self.assertEqual(get_settings()["base_headcount"], 4)

        _, _, reloaded = get_latest_plan()
        for day in reloaded["menu"]:
            if day["day"] == "Thursday":
                self.assertEqual(day["servings"], 7)
                self.assertEqual(day["headcount"], 7)
                self.assertTrue(day["is_overridden"])
            else:
                self.assertEqual(day["servings"], 4)
                self.assertEqual(day["headcount"], 4)
                self.assertFalse(day["is_overridden"])

                # Monday scaled from 3 to 4 (ratio = 4/3 = 1.3333333333333333)
                # Chicken broth: 1.0 * (4/3) = 1.3333 -> nearest 0.25 is 1.25
                if day["day"] == "Monday":
                    broth = [i for i in day["ingredients_to_deduct"] if i["item_name"] == "Chicken Broth"][0]
                    self.assertEqual(broth["amount_used"], 1.25)
                    # Eggs: 2.0 * (4/3) = 2.6667 -> round UP to 3.0
                    eggs = [i for i in day["ingredients_to_deduct"] if i["item_name"] == "Eggs"][0]
                    self.assertEqual(eggs["amount_used"], 3.0)

    # Test 4: WEEK REFRESH: generate a new plan -> ALL days are back at the household size (4), override is gone, no day remembers 7.
    def test_week_refresh_discards_all_overrides_starts_entirely_at_household_size(self):
        save_settings(4, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        # Plan exists with Thursday overridden to 7
        plan = self._sample_plan(default_headcount=4)
        save_plan(plan)
        api_update_day_servings(DayServingsRequest(day="Thursday", servings=7))

        # Refresh week (clear or fresh generate)
        clear_saved_plans()
        fresh_plan = self._sample_plan(default_headcount=4)
        save_plan(fresh_plan)

        _, _, reloaded = get_latest_plan()
        for day in reloaded["menu"]:
            self.assertEqual(day["servings"], 4)
            self.assertEqual(day["headcount"], 4)
            self.assertFalse(day["is_overridden"])
            self.assertFalse(day.get("servings_override", False))

    # Test 5: Reset test: set a day to 6, then "Reset to household" -> day returns to household number, override cleared, later household change moves it.
    def test_reset_day_to_household_clears_override_and_tracks_subsequent_household_change(self):
        save_settings(3, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        plan = self._sample_plan(default_headcount=3)
        save_plan(plan)

        # Set Tuesday to 6
        api_update_day_servings(DayServingsRequest(day="Tuesday", servings=6))
        _, _, p1 = get_latest_plan()
        tuesday1 = [d for d in p1["menu"] if d["day"] == "Tuesday"][0]
        self.assertEqual(tuesday1["servings"], 6)
        self.assertTrue(tuesday1["is_overridden"])

        # Reset to household
        api_update_day_servings(DayServingsRequest(day="Tuesday", reset=True))
        _, _, p2 = get_latest_plan()
        tuesday2 = [d for d in p2["menu"] if d["day"] == "Tuesday"][0]
        self.assertEqual(tuesday2["servings"], 3)
        self.assertFalse(tuesday2["is_overridden"])

        # Later change household 3 -> 5
        save_settings(5, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        _, _, p3 = get_latest_plan()
        tuesday3 = [d for d in p3["menu"] if d["day"] == "Tuesday"][0]
        self.assertEqual(tuesday3["servings"], 5)
        self.assertEqual(tuesday3["headcount"], 5)
        self.assertFalse(tuesday3["is_overridden"])

    # Test 6: Save + reload mid-week -> per-day servings and override preserved. Swap meal on overridden day -> replacement is composed for that day's servings (7).
    def test_save_reload_preserves_overrides_and_swap_meal_uses_overridden_servings(self):
        save_settings(3, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")
        plan = self._sample_plan(default_headcount=3)
        save_plan(plan)
        api_update_day_servings(DayServingsRequest(day="Thursday", servings=7))

        # Reload from database
        _, _, loaded = get_latest_plan()
        thursday = [d for d in loaded["menu"] if d["day"] == "Thursday"][0]
        self.assertEqual(thursday["servings"], 7)
        self.assertTrue(thursday["is_overridden"])

        # Mock swap_single_meal to verify headcount passed to AI
        captured_headcount = []

        def mock_swap(keys, available_inv, slot, current_dish_name, other_meals, headcount, settings):
            captured_headcount.append(headcount)
            res = MealSwapOutput(
                found=True,
                dish_name="Roast Turkey Platter",
                prep_steps=["Roast with herbs"],
                cook_time_minutes=60,
                ingredients_used=[],
            )
            return res, "gemini"

        fake_inv = [{"id": 99, "location": "Fridge", "item_name": "Turkey", "category": "Protein", "quantity": 10.0, "unit": "lbs"}]
        with patch("main.swap_single_meal", side_effect=mock_swap), \
             patch("main.get_inventory", return_value=fake_inv), \
             patch("main.require_ai_keys", return_value={"gemini": "test-key"}):
                from main import SwapMealRequest, api_swap_meal
                mock_request = MagicMock()
                mock_request.headers = {"X-Gemini-Key": "test-key"}
                # Thursday is index 3
                res = api_swap_meal(SwapMealRequest(day_index=3, meal_slot="dinner"), mock_request)
                self.assertTrue(res["success"])
                # Headcount passed to AI was 7 (not global 3)
                self.assertEqual(captured_headcount, [7])
                self.assertEqual(res["plan"]["menu"][3]["dinner"]["dish_name"], "Roast Turkey Platter")
                self.assertEqual(res["plan"]["menu"][3]["dinner"]["changed_from"], "Thursday Dinner")

    # Test 7: Load pre-feature saved plan (no servings fields) -> all days at household size, no overrides, no errors.
    def test_load_pre_feature_saved_plan_without_servings_migrates_cleanly(self):
        save_settings(4, ["Balanced & Whole Foods"], "No peanuts", "Olive oil, salt")

        # Simulate old plan without servings, is_overridden, or servings_override
        old_plan = {
            "menu": [
                {
                    "day": "Monday",
                    "headcount": 4,
                    "breakfast": {"dish_name": "Pancakes", "prep_steps": ["Fry"], "cook_time_minutes": 15},
                    "lunch": {"dish_name": "Salad", "prep_steps": ["Toss"], "cook_time_minutes": 10},
                    "dinner": {"dish_name": "Soup", "prep_steps": ["Simmer"], "cook_time_minutes": 30},
                    "ingredients_to_deduct": [],
                    "cooked": False,
                }
            ],
            "shopping_list": [],
            "nutrition_notes": "None",
        }
        # Direct write to DB without normalization
        _, _, plan_tbl = db.get_table_names("Test Servings Household")
        import json
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute(f"INSERT INTO {plan_tbl} (created_at, plan_json) VALUES (?, ?)", ("2026-10-18 08:00", json.dumps(old_plan)))
        conn.commit()
        conn.close()

        # Load plan through get_latest_plan
        _, _, loaded = get_latest_plan()
        self.assertIsNotNone(loaded)
        day0 = loaded["menu"][0]
        self.assertEqual(day0["servings"], 4)
        self.assertEqual(day0["headcount"], 4)
        self.assertFalse(day0["is_overridden"])
        self.assertFalse(day0.get("servings_override", False))


if __name__ == "__main__":
    unittest.main()

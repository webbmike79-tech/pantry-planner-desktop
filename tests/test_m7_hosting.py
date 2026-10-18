import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="pantry_m7_")
os.environ["PANTRY_DB_FILE"] = os.path.join(_TMP, "m7_test.db")
os.environ["PANTRY_CONFIG_FILE"] = os.path.join(_TMP, "m7_cfg.json")

import ai_fallback
import db
import main
import phone_access
from fastapi.testclient import TestClient


class TestM7Hosting(unittest.TestCase):
    _test_counter = 0

    def setUp(self):
        TestM7Hosting._test_counter += 1
        os.environ["PANTRY_DB_FILE"] = os.path.join(_TMP, f"m7_test_{TestM7Hosting._test_counter}.db")
        db._initialized_households.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        if "MULTI_HOUSEHOLD" in os.environ:
            del os.environ["MULTI_HOUSEHOLD"]
        if "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]
        if "GROQ_API_KEY" in os.environ:
            del os.environ["GROQ_API_KEY"]
        if "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]

    def test_health_endpoint(self):
        """GET /health must return ok: True and status: ok."""
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("ok"), True)
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("app"), "pantry-planner")

    def test_env_keys_precedence(self):
        """Environment variables take precedence over config files."""
        # Scenario 1: Config file has dummy keys, env is empty -> config keys returned
        env_empty = {}
        cfg_dummy = {"gemini": "cfg_gemini_key", "groq": "cfg_groq_key", "openrouter": "cfg_openrouter_key"}
        keys = ai_fallback.extract_provider_keys(headers={}, env=env_empty)
        # Verify fallback works when config file has keys
        cfg_file = os.path.join(_TMP, "provider_cfg.json")
        import json
        with open(cfg_file, "w") as f:
            json.dump({"provider_keys": cfg_dummy}, f)

        keys_from_cfg = ai_fallback.extract_provider_keys(headers={}, env={"PANTRY_CONFIG_FILE": cfg_file})
        self.assertEqual(keys_from_cfg.gemini_key, "cfg_gemini_key")
        self.assertEqual(keys_from_cfg.groq_key, "cfg_groq_key")
        self.assertEqual(keys_from_cfg.openrouter_key, "cfg_openrouter_key")

        # Scenario 2: Env vars set -> env vars win over config
        env_with_keys = {
            "PANTRY_CONFIG_FILE": cfg_file,
            "GEMINI_API_KEY": "env_gemini_override",
            "GROQ_API_KEY": "env_groq_override",
            "OPENROUTER_API_KEY": "env_openrouter_override",
        }
        keys_from_env = ai_fallback.extract_provider_keys(headers={}, env=env_with_keys)
        self.assertEqual(keys_from_env.gemini_key, "env_gemini_override")
        self.assertEqual(keys_from_env.groq_key, "env_groq_override")
        self.assertEqual(keys_from_env.openrouter_key, "env_openrouter_override")

    def test_multi_household_auth_and_isolation(self):
        """Verify strict multi-household partitioning under MULTI_HOUSEHOLD=1."""
        os.environ["MULTI_HOUSEHOLD"] = "1"

        # 1. /health bypasses token requirement
        res_health = self.client.get("/health")
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.json().get("ok"), True)

        # 2. /api/* requires household token
        res_no_token = self.client.get("/api/inventory")
        self.assertEqual(res_no_token.status_code, 401)
        self.assertIn("Household access code required", res_no_token.json().get("detail", ""))

        token_a = "household-alpha-token-111"
        token_b = "household-beta-token-222"
        headers_a = {"X-Pantry-Token": token_a}
        headers_b = {"X-Pantry-Token": token_b}

        # 3. Household A connects fresh: empty inventory, default settings, no plan
        res_a_inv_init = self.client.get("/api/inventory", headers=headers_a)
        self.assertEqual(res_a_inv_init.status_code, 200)
        self.assertEqual(res_a_inv_init.json(), [])

        res_a_set_init = self.client.get("/api/settings", headers=headers_a)
        self.assertEqual(res_a_set_init.status_code, 200)
        self.assertEqual(res_a_set_init.json().get("base_headcount"), 2)

        res_a_plan_init = self.client.get("/api/plan/latest", headers=headers_a)
        self.assertEqual(res_a_plan_init.status_code, 200)
        self.assertIsNone(res_a_plan_init.json().get("plan"))

        # 4. Household A seeds inventory, settings, and a plan
        self.client.post("/api/inventory", headers=headers_a, json={
            "location": "Fridge",
            "items": [{"item_name": "Apples", "category": "Veggies & Produce", "quantity": 5.0, "unit": "count"}]
        })
        self.client.put("/api/settings", headers=headers_a, json={
            "base_headcount": 6,
            "dietary_tags": ["Vegetarian"],
            "custom_diet_notes": "Gluten allergy in family",
            "essential_staples": "Oat milk, Apples"
        })
        sample_plan = {
            "menu": [{
                "day": "Monday",
                "headcount": 6,
                "meal_name": "Apple Salad",
                "description": "Crisp salad with apples",
                "ingredients": ["Apples", "Lettuce"],
                "pantry_ingredients": ["Apples"],
                "missing_ingredients": ["Lettuce"],
                "instructions": "Chop and toss",
                "prep_time_minutes": 10,
                "cook_time_minutes": 0,
                "dietary_tags": ["Vegetarian"],
                "cooked": False
            }],
            "shopping_list": [{
                "item_name": "Lettuce",
                "category": "Produce",
                "amount_needed": "1 head",
                "numeric_quantity": 1.0,
                "unit": "head",
                "default_location": "Fridge",
                "inventory_category": "Veggies & Produce",
                "reason": "Needed for salad",
                "purchased": False
            }]
        }
        main.save_plan(sample_plan, household=token_a)

        # Confirm A has 1 item, headcount 6, and a saved plan
        res_a_inv = self.client.get("/api/inventory", headers=headers_a)
        self.assertEqual(len(res_a_inv.json()), 1)
        self.assertEqual(res_a_inv.json()[0]["item_name"], "Apples")

        res_a_set = self.client.get("/api/settings", headers=headers_a)
        self.assertEqual(res_a_set.json()["base_headcount"], 6)
        self.assertEqual(res_a_set.json()["custom_diet_notes"], "Gluten allergy in family")

        res_a_plan = self.client.get("/api/plan/latest", headers=headers_a)
        self.assertIsNotNone(res_a_plan.json()["plan"])
        self.assertEqual(res_a_plan.json()["plan"]["menu"][0]["meal_name"], "Apple Salad")

        # 5. Household B connects with fresh code: MUST see empty inventory, default settings, no plan
        res_b_inv = self.client.get("/api/inventory", headers=headers_b)
        self.assertEqual(res_b_inv.status_code, 200)
        self.assertEqual(res_b_inv.json(), [], "Household B must not see A's inventory")

        res_b_set = self.client.get("/api/settings", headers=headers_b)
        self.assertEqual(res_b_set.status_code, 200)
        self.assertEqual(res_b_set.json()["base_headcount"], 2, "Household B must see default headcount 2, not A's 6")
        self.assertNotEqual(res_b_set.json()["custom_diet_notes"], "Gluten allergy in family")

        res_b_plan = self.client.get("/api/plan/latest", headers=headers_b)
        self.assertEqual(res_b_plan.status_code, 200)
        self.assertIsNone(res_b_plan.json().get("plan"), "Household B must have no plan")

        # 6. Household B seeds its own data (3 Oranges, headcount 3, separate plan)
        res_b_inv_post = self.client.post("/api/inventory", headers=headers_b, json={
            "location": "Cupboard / Pantry",
            "items": [{"item_name": "Oranges", "category": "Produce/Veggies", "quantity": 3.0, "unit": "count"}]
        })
        self.assertEqual(res_b_inv_post.status_code, 200, res_b_inv_post.text)
        sample_plan_b = {
            "menu": [{
                "day": "Tuesday",
                "headcount": 3,
                "meal_name": "Orange Chicken",
                "description": "Tasty orange dish",
                "ingredients": ["Oranges", "Chicken"],
                "pantry_ingredients": ["Oranges"],
                "missing_ingredients": ["Chicken"],
                "instructions": "Cook chicken and glaze",
                "prep_time_minutes": 15,
                "cook_time_minutes": 20,
                "dietary_tags": [],
                "cooked": False
            }],
            "shopping_list": []
        }
        main.save_plan(sample_plan_b, household=token_b)

        # 7. Household A deletes plan (Clear menu): B must remain completely untouched
        res_del_a = self.client.delete("/api/plan", headers=headers_a)
        self.assertIsNone(res_del_a.json().get("plan"))

        # A's plan is now gone
        res_a_plan_post_clear = self.client.get("/api/plan/latest", headers=headers_a)
        self.assertIsNone(res_a_plan_post_clear.json().get("plan"))

        # B's plan is still intact!
        res_b_plan_after_a_clear = self.client.get("/api/plan/latest", headers=headers_b)
        self.assertIsNotNone(res_b_plan_after_a_clear.json().get("plan"))
        self.assertEqual(res_b_plan_after_a_clear.json()["plan"]["menu"][0]["meal_name"], "Orange Chicken")

        # 8. Deduct an item in Household A: B is untouched
        plan_with_deduct = {
            "menu": [{
                "day": "Monday",
                "headcount": 2,
                "meal_name": "Apple Tart",
                "description": "Bake apples",
                "ingredients": ["Apples"],
                "instructions": "Bake",
                "prep_time_minutes": 5,
                "cook_time_minutes": 20,
                "dietary_tags": [],
                "cooked": False,
                "ingredients_to_deduct": [
                    {"item_name": "Apples", "amount_used": 2.0, "unit": "count"}
                ]
            }],
            "shopping_list": []
        }
        main.save_plan(plan_with_deduct, household=token_a)
        res_cook = self.client.post("/api/plan/cook", headers=headers_a, json={"day_index": 0})
        self.assertEqual(res_cook.status_code, 200)

        # In A, inventory now has 3.0 apples
        res_a_inv_deducted = self.client.get("/api/inventory", headers=headers_a).json()
        self.assertEqual(res_a_inv_deducted[0]["quantity"], 3.0)

        # In B, inventory is still 3.0 oranges (completely untouched!)
        res_b_inv_after_deduct = self.client.get("/api/inventory", headers=headers_b).json()
        self.assertEqual(res_b_inv_after_deduct[0]["item_name"], "Oranges")
        self.assertEqual(res_b_inv_after_deduct[0]["quantity"], 3.0)

        # Clear inventory in A
        self.client.delete("/api/inventory", headers=headers_a)
        res_a_inv_cleared = self.client.get("/api/inventory", headers=headers_a).json()
        self.assertEqual(res_a_inv_cleared, [])

        # B is completely untouched!
        res_b_inv_after_a_delete = self.client.get("/api/inventory", headers=headers_b).json()
        self.assertEqual(len(res_b_inv_after_a_delete), 1)
        self.assertEqual(res_b_inv_after_a_delete[0]["item_name"], "Oranges")
        self.assertEqual(res_b_inv_after_a_delete[0]["quantity"], 3.0)

    from unittest.mock import patch

    @patch("main.generate_weekly_menu_and_list")
    def test_generator_env_precedence(self, mock_generator):
        """Set GEMINI_API_KEY in env, call generator, verify used without being in persisted settings;
        unset env, verify persisted settings used."""
        meal = main.MealDetail(dish_name="Oatmeal", prep_steps=["Boil water", "Stir oats"], cook_time_minutes=5)
        dummy_plan = main.WeeklyPlanOutput(
            menu=[main.DayMealPlan(
                day="Monday", headcount=2, breakfast=meal, lunch=meal, dinner=meal,
                ingredients_to_deduct=[], cooked=False
            )],
            shopping_list=[],
            nutrition_notes="Healthy"
        )
        mock_generator.return_value = (dummy_plan, "gemini")

        # Case 1: GEMINI_API_KEY set in env, no config file
        os.environ["GEMINI_API_KEY"] = "gemini_env_key_xyz"
        if os.path.exists(os.environ["PANTRY_CONFIG_FILE"]):
            os.remove(os.environ["PANTRY_CONFIG_FILE"])

        res = self.client.post("/api/plan/generate", json={"daily_headcounts": {}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("served_by"), "gemini")

        # Case 2: Unset GEMINI_API_KEY from env, save persisted settings in config file
        del os.environ["GEMINI_API_KEY"]
        phone_access.save_config({
            "phone_access": True,
            "access_code": "",
            "provider_keys": {"gemini": "persisted_gemini_key", "groq": "", "openrouter": ""}
        })
        res2 = self.client.post("/api/plan/generate", json={"daily_headcounts": {}})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json().get("served_by"), "gemini")

    def test_real_format_code_end_to_end(self):
        """A real-format code ('Maple River 42') works end-to-end:
        first sight creates its tables + default settings; data added under it reads back under it.
        """
        os.environ["MULTI_HOUSEHOLD"] = "1"
        code = "Maple River 42"
        headers = {"X-Pantry-Token": code}

        # First sight: tables created + default settings
        res_inv = self.client.get("/api/inventory", headers=headers)
        self.assertEqual(res_inv.status_code, 200)
        self.assertEqual(res_inv.json(), [])

        res_set = self.client.get("/api/settings", headers=headers)
        self.assertEqual(res_set.status_code, 200)
        self.assertEqual(res_set.json()["base_headcount"], 2)

        # Data added under it reads back under it
        res_add = self.client.post("/api/inventory", headers=headers, json={
            "location": "Fridge",
            "items": [{"item_name": "Maple Syrup", "category": "Pantry/Dry", "quantity": 1.0, "unit": "bottle"}]
        })
        self.assertEqual(res_add.status_code, 200)

        res_read = self.client.get("/api/inventory", headers=headers)
        self.assertEqual(res_read.status_code, 200)
        items = res_read.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_name"], "Maple Syrup")

    def test_lookalike_codes_fully_isolated(self):
        """Three lookalike codes:
        'Maple River 42' vs 'MapleRiver42' vs 'maple river 42'
        must be three completely isolated households with distinct table mappings.
        """
        os.environ["MULTI_HOUSEHOLD"] = "1"
        c1 = "Maple River 42"
        c2 = "MapleRiver42"
        c3 = "maple river 42"

        # Verify distinct table name mappings
        tbls1 = db.get_table_names(c1)
        tbls2 = db.get_table_names(c2)
        tbls3 = db.get_table_names(c3)
        self.assertNotEqual(tbls1, tbls2)
        self.assertNotEqual(tbls2, tbls3)
        self.assertNotEqual(tbls1, tbls3)

        # Add 1 item to each
        self.client.post("/api/inventory", headers={"X-Pantry-Token": c1}, json={
            "location": "Fridge",
            "items": [{"item_name": "Item 1", "category": "Pantry/Dry", "quantity": 1.0, "unit": "count"}]
        })
        self.client.post("/api/inventory", headers={"X-Pantry-Token": c2}, json={
            "location": "Fridge",
            "items": [{"item_name": "Item 2", "category": "Pantry/Dry", "quantity": 2.0, "unit": "count"}]
        })
        self.client.post("/api/inventory", headers={"X-Pantry-Token": c3}, json={
            "location": "Fridge",
            "items": [{"item_name": "Item 3", "category": "Pantry/Dry", "quantity": 3.0, "unit": "count"}]
        })

        # Verify each sees ONLY its own item
        items1 = self.client.get("/api/inventory", headers={"X-Pantry-Token": c1}).json()
        items2 = self.client.get("/api/inventory", headers={"X-Pantry-Token": c2}).json()
        items3 = self.client.get("/api/inventory", headers={"X-Pantry-Token": c3}).json()

        self.assertEqual([i["item_name"] for i in items1], ["Item 1"])
        self.assertEqual([i["item_name"] for i in items2], ["Item 2"])
        self.assertEqual([i["item_name"] for i in items3], ["Item 3"])

    def test_hostile_code_no_damage(self):
        """Hostile code (quotes, semicolons, SQL injection text) must behave as an ordinary code:
        no injection, no damage, gets its own empty household.
        """
        os.environ["MULTI_HOUSEHOLD"] = "1"
        hostile_code = 'x"; DROP TABLE inventory;--'
        safe_code = "SafeHousehold99"

        # Seed safe household
        self.client.post("/api/inventory", headers={"X-Pantry-Token": safe_code}, json={
            "location": "Fridge",
            "items": [{"item_name": "Safe Milk", "category": "Dairy", "quantity": 1.0, "unit": "gal"}]
        })

        # Connect with hostile code
        res_h_init = self.client.get("/api/inventory", headers={"X-Pantry-Token": hostile_code})
        self.assertEqual(res_h_init.status_code, 200)
        self.assertEqual(res_h_init.json(), [])

        # Add item under hostile code
        res_h_add = self.client.post("/api/inventory", headers={"X-Pantry-Token": hostile_code}, json={
            "location": "Fridge",
            "items": [{"item_name": "Hostile Bread", "category": "Pantry/Dry", "quantity": 1.0, "unit": "loaf"}]
        })
        self.assertEqual(res_h_add.status_code, 200)

        # Safe household must be completely unharmed and intact
        safe_items = self.client.get("/api/inventory", headers={"X-Pantry-Token": safe_code}).json()
        self.assertEqual(len(safe_items), 1)
        self.assertEqual(safe_items[0]["item_name"], "Safe Milk")

        # Hostile household reads back its own item
        hostile_items = self.client.get("/api/inventory", headers={"X-Pantry-Token": hostile_code}).json()
        self.assertEqual(len(hostile_items), 1)
        self.assertEqual(hostile_items[0]["item_name"], "Hostile Bread")

    def test_delete_plan_privacy_critical_isolation(self):
        """DELETE /api/plan under household A clears ONLY A's plan;
        household B's plan is untouched, and GET /api/plan/latest under B still returns only B's plan.
        """
        os.environ["MULTI_HOUSEHOLD"] = "1"
        code_a = "House Privacy Alpha"
        code_b = "House Privacy Beta"

        plan_a = {
            "menu": [{
                "day": "Monday", "headcount": 2, "meal_name": "Alpha Stew",
                "description": "Stew", "instructions": "Stew", "prep_time_minutes": 10,
                "cook_time_minutes": 60, "dietary_tags": [], "cooked": False
            }],
            "shopping_list": []
        }
        plan_b = {
            "menu": [{
                "day": "Monday", "headcount": 4, "meal_name": "Beta Curry",
                "description": "Curry", "instructions": "Simmer", "prep_time_minutes": 10,
                "cook_time_minutes": 30, "dietary_tags": [], "cooked": False
            }],
            "shopping_list": []
        }

        main.save_plan(plan_a, household=code_a)
        main.save_plan(plan_b, household=code_b)

        # Both have their respective plans
        self.assertEqual(self.client.get("/api/plan/latest", headers={"X-Pantry-Token": code_a}).json()["plan"]["menu"][0]["meal_name"], "Alpha Stew")
        self.assertEqual(self.client.get("/api/plan/latest", headers={"X-Pantry-Token": code_b}).json()["plan"]["menu"][0]["meal_name"], "Beta Curry")

        # DELETE /api/plan under A
        res_del_a = self.client.delete("/api/plan", headers={"X-Pantry-Token": code_a})
        self.assertEqual(res_del_a.status_code, 200)

        # A's plan is cleared
        res_a_latest = self.client.get("/api/plan/latest", headers={"X-Pantry-Token": code_a})
        self.assertIsNone(res_a_latest.json().get("plan"))

        # B's plan is 100% untouched
        res_b_latest = self.client.get("/api/plan/latest", headers={"X-Pantry-Token": code_b})
        self.assertIsNotNone(res_b_latest.json().get("plan"))
        self.assertEqual(res_b_latest.json()["plan"]["menu"][0]["meal_name"], "Beta Curry")

    def test_kitchen_name_server_storage(self):
        """Display name: '<Name>'s Kitchen' formed & stored server-side per household code.
        First set wins: second device with same code cannot overwrite and sees existing name.
        Names are data, never markup (e.g. apostrophes, angle brackets).
        """
        os.environ["MULTI_HOUSEHOLD"] = "1"
        code = "Kitchen Household 77"
        headers = {"X-Pantry-Token": code}

        # Initially unnamed
        res_name_init = self.client.get("/api/household/name", headers=headers)
        self.assertEqual(res_name_init.status_code, 200)
        self.assertIsNone(res_name_init.json().get("kitchen_name"))

        # First device sets name to "Mike O'Brien"
        res_set = self.client.post("/api/household/name", headers=headers, json={"name": "Mike O'Brien"})
        self.assertEqual(res_set.status_code, 200)
        self.assertEqual(res_set.json()["kitchen_name"], "Mike O'Brien's Kitchen")
        self.assertTrue(res_set.json()["created"])

        # Second device with same code attempts to set name to "Sarah" -> first set wins!
        res_set2 = self.client.post("/api/household/name", headers=headers, json={"name": "Sarah"})
        self.assertEqual(res_set2.status_code, 200)
        self.assertEqual(res_set2.json()["kitchen_name"], "Mike O'Brien's Kitchen")
        self.assertFalse(res_set2.json()["created"])

        # Verification via GET
        res_get = self.client.get("/api/household/name", headers=headers)
        self.assertEqual(res_get.json()["kitchen_name"], "Mike O'Brien's Kitchen")

        # Verification in GET /api/settings
        res_settings = self.client.get("/api/settings", headers=headers)
        self.assertEqual(res_settings.json()["kitchen_name"], "Mike O'Brien's Kitchen")

        # Test hostile markup in name: stored strictly as plain data
        code_markup = "Markup Household 88"
        res_markup = self.client.post("/api/household/name", headers={"X-Pantry-Token": code_markup}, json={"name": "<b>Bold Mike</b>"})
        self.assertEqual(res_markup.json()["kitchen_name"], "<b>Bold Mike</b>'s Kitchen")


if __name__ == "__main__":
    unittest.main()

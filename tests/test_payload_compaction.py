"""
Unit tests for Milestone 7 / Part 1: Compact menu-generation inventory payload.
Verifies:
1. No id or timestamp in the embedded string.
2. Byte size of embedded inventory <= 45% of the old indented full-record serialization on 128 items.
3. All 128 item names survive in the embedded string.
"""

import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
api_dir = ROOT / "api"
if api_dir.is_dir() and str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

import test_isolation

try:
    import main
    from main import generate_weekly_menu_and_list
except ImportError:
    from api import main
    from api.main import generate_weekly_menu_and_list


def build_synthetic_128_inventory():
    """Build a synthetic 128-item inventory resembling Mike's kitchen."""
    names = [
        "Slap Ya Mama Cajun Seasoning",
        "Clabber Girl Baking Powder",
        "Del Monte Pickled Beets",
        "Richfield Orchard Diced Pears",
        "Heinz Tomato Ketchup",
        "Premier Protein Shakes",
        "Mrs. Renfro's Ghost Pepper Salsa",
        "Louisiana Tartar Sauce",
        "Kraft Dressing",
        "Kirkland Signature Olive Oil",
        "Bob's Red Mill Rolled Oats",
        "Pacific Foods Chicken Broth",
        "Nature's Path Heritage Flakes",
        "San Marzano Peeled Tomatoes",
        "Chosen Foods Avocado Oil",
        "Organic Valley Whole Milk",
    ]
    locations = ["Cupboard", "Fridge", "Freezer"]
    categories = [
        "Pantry Staples",
        "Condiments",
        "Dairy & Eggs",
        "Meat & Seafood",
        "Produce",
        "Frozen Food",
        "Snacks",
        "Baking",
    ]
    units = ["cans", "bottles", "bags", "boxes", "jars", "lbs", "oz", "g", "items", "packages"]

    full_records = []
    for i in range(128):
        full_records.append({
            "id": 1000 + i,
            "household_id": "household_mike_kitchen",
            "location": locations[i % len(locations)],
            "item_name": f"{names[i % len(names)]} #{i+1}",
            "category": categories[i % len(categories)],
            "quantity": round(1.0 + (i % 4) * 0.5, 2),
            "unit": units[i % len(units)],
            "created_at": "2026-10-19T14:30:00.000Z",
            "updated_at": "2026-10-19T14:30:00.000Z",
        })
    return full_records


class TestPayloadCompaction(unittest.TestCase):
    def setUp(self):
        self.full_records = build_synthetic_128_inventory()
        self.df = pd.DataFrame(self.full_records)

    def test_payload_compaction_and_item_survival(self):
        settings = {
            "religious_presets": [],
            "dietary_tags": ["Gluten-Free"],
            "custom_diet_notes": "No tree nuts or shellfish",
            "base_headcount": 2,
        }
        headcounts = {
            "Monday": 2, "Tuesday": 2, "Wednesday": 2,
            "Thursday": 2, "Friday": 2, "Saturday": 2, "Sunday": 2
        }

        with patch.object(main, "_generate_structured") as mock_gen:
            mock_gen.return_value = (MagicMock(), "groq")
            generate_weekly_menu_and_list(
                keys=MagicMock(),
                inventory_df=self.df,
                settings=settings,
                daily_headcounts=headcounts,
                active_theme="Autumn Harvest",
            )
            self.assertTrue(mock_gen.called)
            captured_prompt = mock_gen.call_args[0][1]

        # Extract embedded inventory JSON from the prompt
        marker_start = "CURRENT KITCHEN INVENTORY (Cupboard, Fridge, Freezer):\n    "
        self.assertIn("CURRENT KITCHEN INVENTORY (Cupboard, Fridge, Freezer):", captured_prompt)
        start_idx = captured_prompt.find("CURRENT KITCHEN INVENTORY (Cupboard, Fridge, Freezer):") + len(marker_start)
        end_idx = captured_prompt.find("DAILY HEADCOUNT SCHEDULE")
        self.assertGreater(end_idx, start_idx)
        embedded_str = captured_prompt[start_idx:end_idx].strip()

        # Parse embedded string to verify JSON validity
        parsed_items = json.loads(embedded_str)
        self.assertEqual(len(parsed_items), 128)

        # Assertion (a): no id or timestamp in embedded string
        for item in parsed_items:
            self.assertNotIn("id", item)
            self.assertNotIn("household_id", item)
            self.assertNotIn("created_at", item)
            self.assertNotIn("updated_at", item)
            self.assertEqual(
                sorted(item.keys()),
                sorted(["item_name", "quantity", "unit", "location", "category"])
            )
        self.assertNotIn("2026-10-19", embedded_str)
        self.assertNotIn('"created_at"', embedded_str)
        self.assertNotIn('"updated_at"', embedded_str)

        # Assertion (b): byte size of embedded inventory <= 45% of old indented full-record serialization
        old_full_indented = json.dumps(self.full_records, indent=2)
        old_bytes = len(old_full_indented.encode("utf-8"))
        compact_bytes = len(embedded_str.encode("utf-8"))
        ratio = compact_bytes / old_bytes

        self.assertLessEqual(
            compact_bytes,
            0.45 * old_bytes,
            f"Compacted size ({compact_bytes} bytes) is {ratio:.1%} of old full indented ({old_bytes} bytes), expected <= 45%"
        )

        # Assertion (c): all 128 item names survive in the embedded string
        for i, rec in enumerate(self.full_records):
            expected_name = rec["item_name"]
            self.assertIn(expected_name, embedded_str)
            self.assertEqual(parsed_items[i]["item_name"], expected_name)


if __name__ == "__main__":
    unittest.main()

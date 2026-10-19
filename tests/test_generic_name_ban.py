"""
Unit tests for B1/B2/B3: Generic-name ban for storage scan results.
Verifies:
  1. test_generic_scan_names_sanitized_to_unidentified_numbered_items:
     Mocked scan result containing 'Frozen Food' twice + one real labeled item
     -> the two generics become 'Unidentified Freezer Item 1/2', labeled item untouched, order preserved.
  2. test_scan_prompt_contains_naming_rule:
     Asserts scan prompt text contains the exact required naming rules and banned examples.
  3. test_receipt_scan_path_unaffected_by_guard:
     Asserts receipt extraction and parsing paths do not apply the storage scan generic rename guard.
"""

import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
api_dir = ROOT / "api"
if api_dir.is_dir() and str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

import test_isolation

try:
    from main import (
        BARE_GENERIC_NAMES,
        DetectedItem,
        VisionInventoryResult,
        analyze_storage_photo,
        normalize_scan_location_tag,
        sanitize_scan_items,
        _receipt_prompt,
    )
except ImportError:
    from api.main import (
        BARE_GENERIC_NAMES,
        DetectedItem,
        VisionInventoryResult,
        analyze_storage_photo,
        normalize_scan_location_tag,
        sanitize_scan_items,
        _receipt_prompt,
    )


class TestGenericNameBan(unittest.TestCase):
    def test_generic_scan_names_sanitized_to_unidentified_numbered_items(self):
        """Mocked scan result with 'Frozen Food' twice + one labeled item -> Unidentified Freezer Item 1 & 2."""
        raw_items = [
            DetectedItem(item_name="Frozen Food", category="Frozen", quantity=1.0, unit="bag"),
            DetectedItem(item_name="Eggo Homestyle Waffles", category="Frozen", quantity=1.0, unit="box"),
            DetectedItem(item_name="frozen food", category="Frozen", quantity=2.0, unit="pack"),
        ]

        # Test direct sanitize_scan_items for Freezer
        sanitized = sanitize_scan_items(raw_items, "Freezer")
        self.assertEqual(len(sanitized), 3, "Order and item count preserved")
        self.assertEqual(sanitized[0].item_name, "Unidentified Freezer Item 1")
        self.assertEqual(sanitized[1].item_name, "Eggo Homestyle Waffles", "Non-generic item is untouched")
        self.assertEqual(sanitized[2].item_name, "Unidentified Freezer Item 2")

        # Test location tags for Pantry and Fridge
        pantry_items = [
            DetectedItem(item_name="Pantry Item", category="Pantry/Dry", quantity=1.0, unit="can"),
            DetectedItem(item_name="Campbell's Soup", category="Pantry/Dry", quantity=1.0, unit="can"),
            DetectedItem(item_name="Grocery", category="Pantry/Dry", quantity=3.0, unit="count"),
        ]
        sanitized_pantry = sanitize_scan_items(pantry_items, "Cupboard / Pantry")
        self.assertEqual(sanitized_pantry[0].item_name, "Unidentified Pantry Item 1")
        self.assertEqual(sanitized_pantry[1].item_name, "Campbell's Soup")
        self.assertEqual(sanitized_pantry[2].item_name, "Unidentified Pantry Item 2")

        fridge_items = [
            DetectedItem(item_name="Dairy", category="Dairy", quantity=1.0, unit="bottle"),
            DetectedItem(item_name="Food", category="Meat/Poultry", quantity=1.0, unit="pack"),
        ]
        sanitized_fridge = sanitize_scan_items(fridge_items, "Fridge")
        self.assertEqual(sanitized_fridge[0].item_name, "Unidentified Fridge Item 1")
        self.assertEqual(sanitized_fridge[1].item_name, "Unidentified Fridge Item 2")

        # Test end-to-end via analyze_storage_photo with mocked structured generator
        mock_result = VisionInventoryResult(items=[
            DetectedItem(item_name="Frozen Food", category="Frozen", quantity=1.0, unit="bag"),
            DetectedItem(item_name="Eggo Homestyle Waffles", category="Frozen", quantity=1.0, unit="box"),
            DetectedItem(item_name="Frozen Food", category="Frozen", quantity=2.0, unit="bag"),
        ])
        mock_image = MagicMock()
        with patch("main._generate_structured" if "main" in sys.modules else "api.main._generate_structured", return_value=(mock_result, "gemini")):
            res_items, served_by = analyze_storage_photo(MagicMock(), mock_image, "Freezer")
            self.assertEqual(served_by, "gemini")
            self.assertEqual(res_items[0].item_name, "Unidentified Freezer Item 1")
            self.assertEqual(res_items[1].item_name, "Eggo Homestyle Waffles")
            self.assertEqual(res_items[2].item_name, "Unidentified Freezer Item 2")

    def test_scan_prompt_contains_naming_rule(self):
        """Assert scan prompt text contains the naming rule, banned examples, and hierarchy."""
        mock_image = MagicMock()
        captured_contents = []

        def mock_gen(keys, contents, schema, temp, what):
            captured_contents.append(contents)
            return VisionInventoryResult(items=[]), "gemini"

        patch_target = "main._generate_structured" if "main" in sys.modules else "api.main._generate_structured"
        with patch(patch_target, side_effect=mock_gen):
            analyze_storage_photo(MagicMock(), mock_image, "Freezer")

        self.assertEqual(len(captured_contents), 1)
        prompt_text = captured_contents[0][1]
        self.assertIn("NAMING RULES (STRICT ENFORCEMENT)", prompt_text)
        self.assertIn("NEVER use a bare category or storage word as an item name", prompt_text)
        self.assertIn('"Frozen Food"', prompt_text)
        self.assertIn('"Pantry Item"', prompt_text)
        self.assertIn("Printed label/brand text visible on the packaging", prompt_text)
        self.assertIn("The specific food you can see", prompt_text)
        self.assertIn("Only if genuinely indeterminate, a descriptive visible-form name", prompt_text)

    def test_receipt_scan_path_unaffected_by_guard(self):
        """Assert receipt scan prompt and processing are separate and unaffected by scan guard."""
        prompt = _receipt_prompt([{"item_name": "Milk", "amount_needed": "1 gallon"}], "grocery receipt")
        self.assertNotIn("NAMING RULES (STRICT ENFORCEMENT)", prompt)
        self.assertNotIn("Unidentified Freezer Item", prompt)
        self.assertIn("ACTIVE SHOPPING LIST", prompt)


if __name__ == "__main__":
    unittest.main()

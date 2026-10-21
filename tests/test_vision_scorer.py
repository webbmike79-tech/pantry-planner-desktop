"""
Unit tests for Milestone 7 / Part 2: Scorer honesty (3 reported classes for invented items).
Verifies:
1. 'Frozen Pheasant' -> CONFABULATED (invented species / cut)
2. 'Unidentified Freezer Item 2' -> HONEST-UNKNOWN (honest descriptive label)
3. 'Duke's Hot Sauce' (with no Duke's in manifest) -> CONFABULATED (brand hallucination)
4. Near-matches like 'Tajín' vs 'Tajin Clásico Seasoning' stay MATCHED and never flip into any invented class.
5. Generic unmatched foods like 'Frozen Cookies' or 'Tomato Seasoning' -> UNMATCHED-SPECIFIC.
"""

import os
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tests_dir = ROOT / "tests"
if str(tests_dir) not in sys.path:
    sys.path.insert(0, str(tests_dir))

import test_isolation
from run_vision_benchmark import (
    classify_invented_item,
    is_match,
    score_photo_results,
)


class TestVisionScorerHonesty(unittest.TestCase):
    def test_classifier_fixed_examples(self):
        # 1. 'Frozen Pheasant' -> CONFABULATED
        self.assertEqual(
            classify_invented_item("Frozen Pheasant", ground_truth=["Corn on the Cob", "Mixed Vegetables"]),
            "CONFABULATED",
            "Frozen Pheasant should be classified as CONFABULATED"
        )

        # 2. 'Unidentified Freezer Item 2' -> HONEST-UNKNOWN
        self.assertEqual(
            classify_invented_item("Unidentified Freezer Item 2", ground_truth=["Corn on the Cob"]),
            "HONEST-UNKNOWN",
            "Unidentified Freezer Item 2 should be classified as HONEST-UNKNOWN"
        )

        # Other honest unknown examples
        self.assertEqual(classify_invented_item("Clear Bag with Frozen Meat"), "HONEST-UNKNOWN")
        self.assertEqual(classify_invented_item("Foil-Wrapped Package"), "HONEST-UNKNOWN")
        self.assertEqual(classify_invented_item("Opaque Container"), "HONEST-UNKNOWN")
        self.assertEqual(classify_invented_item("Unidentified seasoning packets"), "HONEST-UNKNOWN")
        self.assertEqual(classify_invented_item("Unidentified food in plastic container"), "HONEST-UNKNOWN")

        # 3. 'Duke's Hot Sauce' with no Duke's in ground truth -> CONFABULATED
        spice_gt = ["Slap Ya Mama Cajun Seasoning", "Tajín", "Garlic Sea Salt"]
        self.assertEqual(
            classify_invented_item("Duke's Hot Sauce", ground_truth=spice_gt),
            "CONFABULATED",
            "Duke's Hot Sauce without Duke's in manifest should be classified as CONFABULATED"
        )

        # Other confabulated brands
        self.assertEqual(classify_invented_item("Taj Pel Eggless Lasagna", ground_truth=[]), "CONFABULATED")
        self.assertEqual(classify_invented_item("Fire & Smoke Society Seasoning", ground_truth=[]), "CONFABULATED")
        self.assertEqual(classify_invented_item("Eggo Waffles", ground_truth=[]), "CONFABULATED")
        self.assertEqual(classify_invented_item("Pretzel Crisps", ground_truth=[]), "CONFABULATED")

        # 4. UNMATCHED-SPECIFIC for generic food claims that are not brand hallucinations
        self.assertEqual(
            classify_invented_item("Frozen Cookies", ground_truth=["Corn on the Cob"]),
            "UNMATCHED-SPECIFIC"
        )
        self.assertEqual(
            classify_invented_item("Tomato Seasoning", ground_truth=spice_gt),
            "UNMATCHED-SPECIFIC"
        )
        self.assertEqual(
            classify_invented_item("Frozen Spinach", ground_truth=["Corn on the Cob"]),
            "UNMATCHED-SPECIFIC"
        )

    def test_near_matches_stay_matched_never_invented(self):
        # 4. Near-matches like 'Tajín' vs 'Tajin Clásico Seasoning' staying MATCHED
        detected = "Tajin Clásico Seasoning"
        gt = ["Slap Ya Mama Cajun Seasoning", "Tajín", "Garlic Sea Salt"]

        self.assertTrue(
            is_match(detected, "Tajín"),
            "Tajin Clásico Seasoning must match Tajín under accent-insensitive token normalization"
        )

        scored = score_photo_results(
            photo_key="spice_rack",
            detected_items=[detected],
            ground_truth=gt,
        )

        self.assertEqual(scored["matched_count"], 1)
        self.assertEqual(scored["invented_count"], 0)
        self.assertNotIn(detected, scored["invented_items"])
        self.assertNotIn(detected, scored["confabulated_items"])
        self.assertNotIn(detected, scored["honest_unknown_items"])
        self.assertNotIn(detected, scored["unmatched_specific_items"])


if __name__ == "__main__":
    unittest.main()

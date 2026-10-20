"""
Unit tests for Milestone 7: Size-aware provider routing for weekly-plan generation.
Verifies:
1. 128 items, all providers mocked-available -> generate call attempts Gemini then OpenRouter, NEVER Groq; skip log line present.
2. 75 items (boundary) -> Groq IS attempted in normal position. 76 items -> Groq skipped. (Boundary exactness matters.)
3. 40 items -> normal chain order unchanged.
4. Groq-only configuration at 128 items -> Groq attempted (only-provider exception).
5. A non-generation action (e.g. scan analysis) at any inventory size -> chain order unchanged.
"""

import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
api_dir = ROOT / "api"
if api_dir.is_dir() and str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

import test_isolation

try:
    import ai_fallback
    from ai_fallback import (
        GROQ_GENERATION_MAX_ITEMS,
        ProviderKeys,
        generate_structured_with_fallback,
    )
except ImportError:
    from api import ai_fallback
    from api.ai_fallback import (
        GROQ_GENERATION_MAX_ITEMS,
        ProviderKeys,
        generate_structured_with_fallback,
    )


class DummyPlanOutput(BaseModel):
    summary: str


class TestSizeAwareRouting(unittest.TestCase):
    def test_constant_value(self):
        """Constant must be defined as GROQ_GENERATION_MAX_ITEMS = 75."""
        self.assertEqual(GROQ_GENERATION_MAX_ITEMS, 75)

    def test_128_items_skips_groq_and_logs(self):
        """128 items, all providers available -> attempts Gemini then OpenRouter, NEVER Groq; skip log line present."""
        keys = ProviderKeys(
            gemini_key="mock-gemini-key",
            groq_key="mock-groq-key",
            openrouter_key="mock-openrouter-key",
        )
        attempted_providers = []

        def mock_call_gemini(*args, **kwargs):
            attempted_providers.append("gemini")
            raise RuntimeError("503 Service Unavailable: High demand")

        def mock_call_openai(*args, **kwargs):
            base_url = kwargs.get("base_url") or (args[0] if args else "")
            if "groq" in base_url:
                attempted_providers.append("groq")
            elif "openrouter" in base_url:
                attempted_providers.append("openrouter")
            return '{"summary": "Plan generated"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_call_gemini), \
             patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_call_openai), \
             patch("time.sleep", return_value=None):
            with self.assertLogs(ai_fallback.logger, level="INFO") as cm:
                res, served_by = generate_structured_with_fallback(
                    keys=keys,
                    contents="test plan prompt",
                    schema=DummyPlanOutput,
                    temperature=0.3,
                    what="build your weekly menu",
                    item_count=128,
                    action="generate_plan",
                )

        self.assertEqual(res.summary, "Plan generated")
        self.assertEqual(served_by, "openrouter")
        self.assertNotIn("groq", attempted_providers)
        self.assertEqual(attempted_providers, ["gemini", "gemini", "openrouter"])

        # Check skip log line
        skip_logs = [m for m in cm.output if "AI route skip:" in m]
        self.assertEqual(len(skip_logs), 1)
        self.assertIn("provider='groq'", skip_logs[0])
        self.assertIn("reason='inventory_size'", skip_logs[0])
        self.assertIn("items=128", skip_logs[0])
        self.assertIn("threshold=75", skip_logs[0])
        self.assertIn("action='generate_plan'", skip_logs[0])

    def test_boundary_exactness_75_vs_76(self):
        """75 items (boundary) -> Groq IS attempted in normal position. 76 items -> Groq skipped."""
        keys = ProviderKeys(
            gemini_key="mock-gemini-key",
            groq_key="mock-groq-key",
            openrouter_key="mock-openrouter-key",
        )

        # Boundary test at 75 items: Groq attempted after Gemini fails
        attempted_75 = []
        def mock_gemini_fail(*args, **kwargs):
            attempted_75.append("gemini")
            raise RuntimeError("503 Service Unavailable")

        def mock_openai_75(*args, **kwargs):
            base_url = kwargs.get("base_url") or (args[0] if args else "")
            if "groq" in base_url:
                attempted_75.append("groq")
                return '{"summary": "Served by groq at 75"}'
            attempted_75.append("openrouter")
            return '{"summary": "Served by openrouter"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_gemini_fail), \
             patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_openai_75), \
             patch("time.sleep", return_value=None):
            res_75, served_75 = generate_structured_with_fallback(
                keys=keys,
                contents="test plan prompt",
                schema=DummyPlanOutput,
                temperature=0.3,
                what="build your weekly menu",
                item_count=75,
                action="generate_plan",
            )
        self.assertEqual(served_75, "groq")
        self.assertIn("groq", attempted_75)

        # Boundary test at 76 items: Groq skipped
        attempted_76 = []
        def mock_gemini_fail_76(*args, **kwargs):
            attempted_76.append("gemini")
            raise RuntimeError("503 Service Unavailable")

        def mock_openai_76(*args, **kwargs):
            base_url = kwargs.get("base_url") or (args[0] if args else "")
            if "groq" in base_url:
                attempted_76.append("groq")
            elif "openrouter" in base_url:
                attempted_76.append("openrouter")
                return '{"summary": "Served by openrouter at 76"}'
            return '{"summary": "Unexpected"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_gemini_fail_76), \
             patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_openai_76), \
             patch("time.sleep", return_value=None):
            res_76, served_76 = generate_structured_with_fallback(
                keys=keys,
                contents="test plan prompt",
                schema=DummyPlanOutput,
                temperature=0.3,
                what="build your weekly menu",
                item_count=76,
                action="generate_plan",
            )
        self.assertEqual(served_76, "openrouter")
        self.assertNotIn("groq", attempted_76)

    def test_40_items_normal_chain_order_unchanged(self):
        """40 items -> normal chain order unchanged: Gemini, Groq, OpenRouter."""
        keys = ProviderKeys(
            gemini_key="mock-gemini-key",
            groq_key="mock-groq-key",
            openrouter_key="mock-openrouter-key",
        )
        attempted = []

        def mock_gemini_fail(*args, **kwargs):
            attempted.append("gemini")
            raise RuntimeError("503 Service Unavailable")

        def mock_openai_40(*args, **kwargs):
            base_url = kwargs.get("base_url") or (args[0] if args else "")
            if "groq" in base_url:
                attempted.append("groq")
                return '{"summary": "Served by groq at 40"}'
            attempted.append("openrouter")
            return '{"summary": "Served by openrouter"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_gemini_fail), \
             patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_openai_40), \
             patch("time.sleep", return_value=None):
            res, served_by = generate_structured_with_fallback(
                keys=keys,
                contents="test plan prompt",
                schema=DummyPlanOutput,
                temperature=0.3,
                what="build your weekly menu",
                item_count=40,
                action="generate_plan",
            )
        self.assertEqual(served_by, "groq")
        self.assertIn("groq", attempted)

    def test_groq_only_exception_at_128_items(self):
        """Groq-only configuration at 128 items -> Groq attempted (only-provider exception)."""
        keys = ProviderKeys(groq_key="mock-groq-key")
        attempted = []

        def mock_openai_groq_only(*args, **kwargs):
            attempted.append("groq")
            return '{"summary": "Served by groq only"}'

        with patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_openai_groq_only):
            res, served_by = generate_structured_with_fallback(
                keys=keys,
                contents="test plan prompt",
                schema=DummyPlanOutput,
                temperature=0.3,
                what="build your weekly menu",
                item_count=128,
                action="generate_plan",
            )
        self.assertEqual(served_by, "groq")
        self.assertEqual(attempted, ["groq"])

    def test_non_generation_action_unaffected_at_any_size(self):
        """A non-generation action (e.g. scan analysis) at any inventory size -> chain order unchanged."""
        keys = ProviderKeys(
            gemini_key="mock-gemini-key",
            groq_key="mock-groq-key",
            openrouter_key="mock-openrouter-key",
        )
        attempted = []

        def mock_gemini_fail(*args, **kwargs):
            attempted.append("gemini")
            raise RuntimeError("503 Service Unavailable")

        def mock_openai(*args, **kwargs):
            base_url = kwargs.get("base_url") or (args[0] if args else "")
            if "groq" in base_url:
                attempted.append("groq")
                return '{"summary": "Served by groq scan"}'
            attempted.append("openrouter")
            return '{"summary": "Served by openrouter"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_gemini_fail), \
             patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_openai), \
             patch("time.sleep", return_value=None):
            res, served_by = generate_structured_with_fallback(
                keys=keys,
                contents="test scan prompt",
                schema=DummyPlanOutput,
                temperature=0.2,
                what="scan your fridge photo",
                item_count=128,
                action="scan_photo",
            )
        self.assertEqual(served_by, "groq")
        self.assertIn("groq", attempted)


if __name__ == "__main__":
    unittest.main()

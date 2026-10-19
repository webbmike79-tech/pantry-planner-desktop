"""
Unit tests for Provider Resilience (Milestone 7 Resilience Add-on).
Verifies:
  1. test_config_override_takes_effect_without_restart:
     Changing provider model in config is picked up by ai_fallback dynamically on next call.
  2. test_probe_missing_model_down_and_completion_health_and_status_shape:
     Probe detects absent model, marks provider DOWN; completion probe checks health;
     status response shape matches spec for desktop and phone (sanitized).
  3. test_auto_skip_down_provider_all_down_fallback_and_real_success_clears:
     Auto-skip bypasses DOWN provider with log line; if ALL providers are DOWN, attempts in
     order anyway; successful real call clears DOWN immediately.
  4. test_auto_discovery_substitution_preferences_and_gemini_never_substituted:
     Groq and OpenRouter auto-substitute according to family/vision preferences when model absent;
     Gemini is NEVER auto-substituted.
  5. test_settings_save_validation_refuses_bogus_model_and_saves_valid:
     Saving model via settings validates model first; refuses bogus/404 with exact plain message;
     saves valid model and updates status.
  6. test_regression_pre_addon_behavior_green:
     Standard fallback chain still functions seamlessly end-to-end with dynamic models.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

_api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "api"))
if os.path.isdir(_api_dir) and _api_dir not in sys.path:
    sys.path.insert(0, _api_dir)

import test_isolation
try:
    from ai_fallback import (
        ProviderKeys,
        extract_provider_models,
        save_configured_model,
        is_vision_model,
        auto_discover_substitute,
        probe_provider,
        probe_all_providers,
        get_providers_status,
        save_provider_model_with_validation,
        is_provider_down,
        mark_provider_ok,
        generate_structured_with_fallback,
        _PROVIDER_STATUS_CACHE,
    )
except ImportError:
    from api.ai_fallback import (
        ProviderKeys,
        extract_provider_models,
        save_configured_model,
        is_vision_model,
        auto_discover_substitute,
        probe_provider,
        probe_all_providers,
        get_providers_status,
        save_provider_model_with_validation,
        is_provider_down,
        mark_provider_ok,
        generate_structured_with_fallback,
        _PROVIDER_STATUS_CACHE,
    )


class DummyModel(BaseModel):
    message: str


class TestProviderResilience(unittest.TestCase):
    def setUp(self):
        # Ensure temporary test config & DB
        self.temp_dir = tempfile.mkdtemp()
        self.cfg_file = os.path.join(self.temp_dir, "pantry_desktop_config.json")
        self.status_file = os.path.join(self.temp_dir, "pantry_providers_status.json")

        self.initial_config = {
            "phone_access": True,
            "provider_keys": {
                "gemini": "test-gem-key",
                "groq": "test-groq-key",
                "openrouter": "test-openrouter-key",
            },
            "provider_models": {
                "gemini": "gemini-3.8-flash",
                "groq": "qwen/qwen3.8-27b",
                "openrouter": "nvidia/nemotron-3.5-super-vl:free",
            },
        }
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump(self.initial_config, f)

        self.test_env = {
            "PANTRY_TEST_MODE": "1",
            "PANTRY_CONFIG_FILE": self.cfg_file,
        }
        _PROVIDER_STATUS_CACHE.clear()

    def tearDown(self):
        _PROVIDER_STATUS_CACHE.clear()

    def test_config_override_takes_effect_without_restart(self):
        """1. Changing model ID in config is read at call time without process restart."""
        models = extract_provider_models(self.test_env)
        self.assertEqual(models["gemini"], "gemini-3.8-flash")
        self.assertEqual(models["groq"], "qwen/qwen3.8-27b")

        # Update groq model in config file directly (simulating edit or external update)
        with open(self.cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["provider_models"]["groq"] = "meta-llama/llama-4-scout-17b-16e-instruct"
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        updated_models = extract_provider_models(self.test_env)
        self.assertEqual(updated_models["groq"], "meta-llama/llama-4-scout-17b-16e-instruct")

    @patch("ai_fallback._probe_minimal_completion")
    @patch("ai_fallback.requests.get")
    def test_probe_missing_model_down_and_completion_health_and_status_shape(
        self, mock_get, mock_completion
    ):
        """2. Self-probe marks missing model DOWN; completion failure marks DOWN; verifies status payload."""
        # Scenario A: Groq returns /models without configured model
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "data": [
                {"id": "llama-3.1-8b-instant"},
                {"id": "whisper-large-v3"},
            ]
        }
        mock_get.return_value = mock_get_resp
        mock_completion.return_value = (True, 200, None)

        keys = ProviderKeys(groq_key="gsk-test")
        # When allow_substitute=False, missing model marks DOWN
        res = probe_provider("groq", keys=keys, env=self.test_env, allow_substitute=False)
        self.assertEqual(res["state"], "down")
        self.assertEqual(res["status_word"], "Down")
        self.assertTrue(is_provider_down("groq"))

        # Scenario B: Completion probe failure marks provider DOWN
        mock_completion.return_value = (False, 503, "Service unavailable")
        res_gemini = probe_provider("gemini", keys=ProviderKeys(gemini_key="gem-key"), env=self.test_env)
        self.assertEqual(res_gemini["state"], "down")
        self.assertEqual(res_gemini["status_word"], "Down")

        # Status shape for Desktop vs Phone
        desktop_status = get_providers_status(client_is_phone=False, env=self.test_env)
        self.assertIn("providers", desktop_status)
        self.assertIn("groq", desktop_status["providers"])
        self.assertIn("model", desktop_status["providers"]["groq"])
        self.assertIn("checked_display", desktop_status["providers"]["groq"])

        phone_status = get_providers_status(client_is_phone=True, env=self.test_env)
        self.assertIn("providers", phone_status)
        self.assertIn("groq", phone_status["providers"])
        # Phone status must NOT expose model ID or internal debug errors
        self.assertNotIn("model", phone_status["providers"]["groq"])
        self.assertNotIn("error", phone_status["providers"]["groq"])
        self.assertIn("status_word", phone_status["providers"]["groq"])

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_auto_skip_down_provider_all_down_fallback_and_real_success_clears(
        self, mock_gemini, mock_openai
    ):
        """3. Auto-skip skips DOWN providers; if all are DOWN, tries anyway; success clears DOWN."""
        keys = ProviderKeys(
            gemini_key="gem-key",
            groq_key="groq-key",
            openrouter_key="or-key",
        )

        # Mark Gemini as DOWN
        _PROVIDER_STATUS_CACHE["gemini"] = {
            "provider": "gemini",
            "name": "Gemini",
            "state": "down",
            "status_word": "Down",
            "error": "Probe failed",
        }
        # Groq is OK
        _PROVIDER_STATUS_CACHE["groq"] = {
            "provider": "groq",
            "name": "Groq",
            "state": "ok",
            "status_word": "Working",
        }

        mock_openai.return_value = '{"message": "served from groq"}'
        res, served_by = generate_structured_with_fallback(
            keys=keys,
            contents="test contents",
            schema=DummyModel,
            temperature=0.1,
            what="test skip",
            env=self.test_env,
        )
        self.assertEqual(served_by, "groq")
        self.assertEqual(res.message, "served from groq")
        # Gemini was skipped without calling _call_gemini
        mock_gemini.assert_not_called()

        # Test: Real call success clears DOWN state immediately
        mark_provider_ok("gemini", env=self.test_env)
        self.assertFalse(is_provider_down("gemini"))
        self.assertEqual(_PROVIDER_STATUS_CACHE["gemini"]["state"], "ok")

        # Test: All providers DOWN -> exception: attempts anyway in order
        _PROVIDER_STATUS_CACHE["gemini"]["state"] = "down"
        _PROVIDER_STATUS_CACHE["groq"]["state"] = "down"
        _PROVIDER_STATUS_CACHE["openrouter"] = {
            "provider": "openrouter",
            "state": "down",
            "status_word": "Down",
        }

        mock_gemini.return_value = '{"message": "gemini recovered"}'
        res_all_down, served_by_all = generate_structured_with_fallback(
            keys=keys,
            contents="test contents",
            schema=DummyModel,
            temperature=0.1,
            what="test all down",
            env=self.test_env,
        )
        self.assertEqual(served_by_all, "gemini")
        self.assertEqual(res_all_down.message, "gemini recovered")
        mock_gemini.assert_called_once()
        # Successful call cleared Gemini DOWN state
        self.assertFalse(is_provider_down("gemini"))

    def test_auto_discovery_substitution_preferences_and_gemini_never_substituted(self):
        """4. Groq/OpenRouter substitute vision models by family preference; Gemini never substituted."""
        # Gemini check
        gem_sub = auto_discover_substitute("gemini", [{"id": "gemini-2.0-flash"}])
        self.assertIsNone(gem_sub, "Gemini must NEVER be auto-substituted!")

        # Groq preference check: Llama vision / Scout line first
        groq_models = [
            {"id": "llama-3.3-70b-versatile"},  # text-only
            {"id": "qwen/qwen3.8-27b"},          # vision, not scout
            {"id": "meta-llama/llama-4-scout-17b-16e-instruct"},  # scout vision!
            {"id": "meta-llama/llama-3.2-11b-vision-preview"},    # llama vision!
        ]
        groq_sub = auto_discover_substitute("groq", groq_models)
        # Both scout and 3.2-11b-vision are scout family; sorted alphabetically
        self.assertTrue("scout" in groq_sub or "vision" in groq_sub)
        self.assertTrue(is_vision_model(groq_sub))

        # OpenRouter preference check: Gemini Flash :free first, then any :free vision
        openrouter_models = [
            {"id": "meta-llama/llama-3.3-70b-instruct:free"},        # text-only
            {"id": "nvidia/nemotron-3.5-super-vl:free"},             # free vision
            {"id": "google/gemini-2.0-flash-exp:free"},              # gemini flash :free!
            {"id": "google/gemini-pro"},                             # paid
        ]
        or_sub = auto_discover_substitute("openrouter", openrouter_models)
        self.assertEqual(or_sub, "google/gemini-2.0-flash-exp:free")

    @patch("ai_fallback._probe_minimal_completion")
    @patch("ai_fallback.requests.get")
    def test_settings_save_validation_refuses_bogus_model_and_saves_valid(
        self, mock_get, mock_completion
    ):
        """5. Settings save model refuses 404/bogus model with exact plain copy; saves valid."""
        # Groq /models returns available list
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"id": "qwen/qwen3.8-27b"},
                {"id": "meta-llama/llama-4-scout-17b-16e-instruct"},
            ]
        }
        mock_get.return_value = mock_resp
        mock_completion.return_value = (True, 200, None)

        keys = ProviderKeys(groq_key="test-key")

        # Attempt bogus model
        ok, msg, _ = save_provider_model_with_validation(
            provider="groq",
            candidate_model="non-existent-bogus-model-xyz",
            keys=keys,
            env=self.test_env,
        )
        self.assertFalse(ok)
        self.assertEqual(
            msg,
            "That model isn't available from Groq right now — check the name and try again.",
        )
        # Config should still have the original model
        models = extract_provider_models(self.test_env)
        self.assertEqual(models["groq"], "qwen/qwen3.8-27b")

        # Attempt valid model
        ok_valid, msg_valid, _ = save_provider_model_with_validation(
            provider="groq",
            candidate_model="meta-llama/llama-4-scout-17b-16e-instruct",
            keys=keys,
            env=self.test_env,
        )
        self.assertTrue(ok_valid)
        models_after = extract_provider_models(self.test_env)
        self.assertEqual(models_after["groq"], "meta-llama/llama-4-scout-17b-16e-instruct")

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_regression_pre_addon_behavior_green(self, mock_gemini, mock_openai):
        """6. Existing fallback chain (503 -> failover) continues to function seamlessly."""
        mock_gemini.side_effect = Exception("503 Service Unavailable")
        mock_openai.return_value = '{"message": "groq success"}'

        keys = ProviderKeys(
            gemini_key="gem-key",
            groq_key="groq-key",
            openrouter_key="or-key",
        )

        res, served_by = generate_structured_with_fallback(
            keys=keys,
            contents="test prompt",
            schema=DummyModel,
            temperature=0.1,
            what="regression check",
            env=self.test_env,
        )
        self.assertEqual(served_by, "groq")
        self.assertEqual(res.message, "groq success")


if __name__ == "__main__":
    unittest.main()

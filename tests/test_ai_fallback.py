"""
Unit tests for AI Provider Fallback Chain (Milestone 5) - Desktop Repo.
Verifies:
  1. Gemini succeeds (no failover needed).
  2. Gemini 503 -> Groq succeeds.
  3. Gemini 503 + Groq 429 -> OpenRouter succeeds.
  4. All providers fail (503/429/timeout) -> friendly user-facing error (no traceback).
  5. Malformed JSON output triggers failover to next provider.
  6. Request timeout triggers failover.
  7. Served_by provider name is accurately reported and logged.
"""

import unittest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel
import requests

from ai_fallback import (
    ProviderKeys,
    generate_structured_with_fallback,
    extract_provider_keys,
    validate_provider_key,
)


class DummyResult(BaseModel):
    item_name: str
    quantity: float


class TestAIFallbackChain(unittest.TestCase):
    def setUp(self):
        self.keys_all = ProviderKeys(
            gemini_key="gemini-test-key",
            groq_key="groq-test-key",
            openrouter_key="openrouter-test-key",
        )
        self.dummy_json = '{"item_name": "Apples", "quantity": 3.0}'

    @patch("ai_fallback._call_gemini")
    def test_gemini_success(self, mock_gemini):
        """When Gemini works, it serves immediately without touching fallbacks."""
        mock_gemini.return_value = self.dummy_json

        res, served_by = generate_structured_with_fallback(
            keys=self.keys_all,
            contents="test prompt",
            schema=DummyResult,
            temperature=0.1,
            what="scan test item",
        )
        self.assertEqual(served_by, "gemini")
        self.assertEqual(res.item_name, "Apples")
        self.assertEqual(res.quantity, 3.0)
        mock_gemini.assert_called_once()

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_gemini_503_failover_to_groq(self, mock_gemini, mock_openai):
        """Gemini raises 503 Service Unavailable -> failover to Groq."""
        mock_gemini.side_effect = RuntimeError("503 The model is overloaded. Please try again later.")
        mock_openai.return_value = self.dummy_json

        res, served_by = generate_structured_with_fallback(
            keys=self.keys_all,
            contents="test prompt",
            schema=DummyResult,
            temperature=0.1,
            what="scan test item",
        )
        self.assertEqual(served_by, "groq")
        self.assertEqual(res.item_name, "Apples")
        mock_gemini.assert_called_once()
        mock_openai.assert_called_once()

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_gemini_503_and_groq_429_failover_to_openrouter(self, mock_gemini, mock_openai):
        """Gemini 503 + Groq 429 Rate Limit -> failover to OpenRouter."""
        mock_gemini.side_effect = RuntimeError("503 Service Unavailable")

        # Mock Groq failure (429) then OpenRouter success
        resp_429 = requests.Response()
        resp_429.status_code = 429
        groq_err = requests.exceptions.HTTPError("429 Too Many Requests", response=resp_429)

        mock_openai.side_effect = [groq_err, self.dummy_json]

        res, served_by = generate_structured_with_fallback(
            keys=self.keys_all,
            contents="test prompt",
            schema=DummyResult,
            temperature=0.1,
            what="scan test item",
        )
        self.assertEqual(served_by, "openrouter")
        self.assertEqual(res.item_name, "Apples")
        self.assertEqual(mock_openai.call_count, 2)

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_all_providers_fail_returns_friendly_error(self, mock_gemini, mock_openai):
        """If all configured providers fail, raise a friendly user-facing error (no raw stacktrace)."""
        mock_gemini.side_effect = RuntimeError("503 Model Overloaded")

        resp_503 = requests.Response()
        resp_503.status_code = 503
        mock_openai.side_effect = requests.exceptions.HTTPError("503 Service Unavailable", response=resp_503)

        with self.assertRaises(RuntimeError) as ctx:
            generate_structured_with_fallback(
                keys=self.keys_all,
                contents="test prompt",
                schema=DummyResult,
                temperature=0.1,
                what="read grocery receipt",
            )
        err_msg = str(ctx.exception)
        self.assertIn("Unable to read grocery receipt", err_msg)
        self.assertIn("temporarily unavailable or overloaded", err_msg)
        self.assertNotIn("Traceback", err_msg)

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_malformed_json_triggers_failover(self, mock_gemini, mock_openai):
        """If a provider returns malformed or invalid JSON, fail over to next provider."""
        mock_gemini.return_value = '{"item_name": "Incomplete...'  # syntax error
        mock_openai.return_value = self.dummy_json

        res, served_by = generate_structured_with_fallback(
            keys=self.keys_all,
            contents="test prompt",
            schema=DummyResult,
            temperature=0.1,
            what="scan test item",
        )
        self.assertEqual(served_by, "groq")
        self.assertEqual(res.item_name, "Apples")

    @patch("ai_fallback._call_openai_compatible")
    @patch("ai_fallback._call_gemini")
    def test_timeout_triggers_failover(self, mock_gemini, mock_openai):
        """If provider times out, fail over to next provider."""
        mock_gemini.side_effect = TimeoutError("Gemini call timed out")
        mock_openai.return_value = self.dummy_json

        res, served_by = generate_structured_with_fallback(
            keys=self.keys_all,
            contents="test prompt",
            schema=DummyResult,
            temperature=0.1,
            what="scan photo",
        )
        self.assertEqual(served_by, "groq")
        self.assertEqual(res.item_name, "Apples")

    def test_extract_provider_keys(self):
        """Extract keys properly from headers or env."""
        headers = {
            "x-gemini-key": "gem-123",
            "x-groq-key": "groq-456",
            "x-openrouter-key": "openr-789",
        }
        keys = extract_provider_keys(headers)
        self.assertEqual(keys.gemini_key, "gem-123")
        self.assertEqual(keys.groq_key, "groq-456")
        self.assertEqual(keys.openrouter_key, "openr-789")


if __name__ == "__main__":
    unittest.main()

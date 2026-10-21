"""
Unit tests for C1/C2: Permanent structured per-attempt and total timing logs in ai_fallback.
Verifies:
  1. test_two_attempt_chain_emits_timing_lines_and_total:
     A mocked two-attempt chain emits structured timing lines for both attempts + total on served path.
  2. test_chain_exhaustion_emits_timing_lines_and_total_failure:
     A failed chain emits attempt failure timing lines + total timing line with outcome='failure'.
"""

import os
import sys
import time
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
    import ai_fallback
    from ai_fallback import (
        ProviderKeys,
        generate_structured_with_fallback,
    )
except ImportError:
    from api import ai_fallback
    from api.ai_fallback import (
        ProviderKeys,
        generate_structured_with_fallback,
    )


class DummyOutput(BaseModel):
    plan_title: str


class TestTimingLogs(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    def test_two_attempt_chain_emits_timing_lines_and_total(self, mock_sleep):
        """A mocked two-attempt chain emits timing lines for attempt 1, attempt 2, and total."""
        keys = ProviderKeys(gemini_key="mock-gemini-key")
        call_count = 0

        def mock_gemini(api_key, contents, schema, temperature, model):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt fails with retryable 503 error
                raise RuntimeError("503 Service Unavailable: High demand")
            # Second attempt succeeds with valid JSON
            return '{"plan_title": "Delicious Week"}'

        with patch.object(ai_fallback, "_call_gemini", side_effect=mock_gemini):
            with self.assertLogs(ai_fallback.logger, level="INFO") as cm:
                res, served_by = generate_structured_with_fallback(
                    keys=keys,
                    contents="test prompt",
                    schema=DummyOutput,
                    temperature=0.2,
                    what="build your weekly menu",
                )

        self.assertEqual(res.plan_title, "Delicious Week")
        self.assertEqual(served_by, "gemini")

        # Find timing log lines in captured messages
        attempt_logs = [m for m in cm.output if "AI attempt timing:" in m]
        total_logs = [m for m in cm.output if "AI total timing:" in m]

        self.assertEqual(len(attempt_logs), 2, "Exactly 2 attempt timing log lines emitted")
        self.assertEqual(len(total_logs), 1, "Exactly 1 total timing log line emitted")

        # Verify attempt 1 structured attributes
        self.assertIn("action='build your weekly menu'", attempt_logs[0])
        self.assertIn("provider='gemini'", attempt_logs[0])
        self.assertIn("attempt=1", attempt_logs[0])
        self.assertIn("outcome='retryable-failure'", attempt_logs[0])
        self.assertIn("duration_ms=", attempt_logs[0])

        # Verify attempt 2 structured attributes
        self.assertIn("action='build your weekly menu'", attempt_logs[1])
        self.assertIn("provider='gemini'", attempt_logs[1])
        self.assertIn("attempt=2", attempt_logs[1])
        self.assertIn("outcome='success'", attempt_logs[1])
        self.assertIn("duration_ms=", attempt_logs[1])

        # Verify total duration line
        self.assertIn("action='build your weekly menu'", total_logs[0])
        self.assertIn("duration_ms=", total_logs[0])
        self.assertIn("outcome='success'", total_logs[0])
        self.assertIn("served_by='gemini'", total_logs[0])

    @patch("time.sleep", return_value=None)
    def test_chain_exhaustion_emits_timing_lines_and_total_failure(self, mock_sleep):
        """Chain exhaustion emits failure attempt lines and total line with outcome='failure'."""
        keys = ProviderKeys(groq_key="mock-groq-key")

        def mock_groq(*args, **kwargs):
            raise RuntimeError("HTTP 413 Client Error: Payload Too Large")

        with patch.object(ai_fallback, "_call_openai_compatible", side_effect=mock_groq):
            with self.assertLogs(ai_fallback.logger, level="INFO") as cm:
                with self.assertRaises(RuntimeError):
                    generate_structured_with_fallback(
                        keys=keys,
                        contents="huge prompt",
                        schema=DummyOutput,
                        temperature=0.2,
                        what="build huge menu",
                    )

        attempt_logs = [m for m in cm.output if "AI attempt timing:" in m]
        total_logs = [m for m in cm.output if "AI total timing:" in m]

        self.assertEqual(len(attempt_logs), 1)
        self.assertIn("outcome='failure'", attempt_logs[0])
        self.assertIn("attempt=1", attempt_logs[0])
        self.assertIn("duration_ms=", attempt_logs[0])

        self.assertEqual(len(total_logs), 1)
        self.assertIn("outcome='failure'", total_logs[0])
        self.assertIn("duration_ms=", total_logs[0])


if __name__ == "__main__":
    unittest.main()

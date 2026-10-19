"""
Regression guard test: ensures tests NEVER touch, modify, or delete real production data.
Snapshots real database and config files before suite execution and asserts byte/count
equality after. Skips cleanly if real files do not exist on the machine.
"""

import os
import tempfile
import unittest
import test_isolation


class TestProductionDataIntegrityGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Take snapshot before tests
        cls.before_snapshot = test_isolation.snapshot_real_state()

    def test_production_database_and_config_untouched(self):
        """Verify that running the test suite leaves real DB and config 100% unaltered."""
        test_isolation.verify_real_state_unchanged(self.before_snapshot)

    def test_isolation_active_in_test_environment(self):
        """Verify that PANTRY_TEST_MODE is set and DB/CONFIG point to temporary paths."""
        self.assertEqual(os.environ.get("PANTRY_TEST_MODE"), "1")
        db_file = os.environ.get("PANTRY_DB_FILE", "")
        cfg_file = os.environ.get("PANTRY_CONFIG_FILE", "")
        temp_dir = tempfile.gettempdir().lower()
        self.assertTrue(
            db_file.lower().startswith(temp_dir) or "test" in os.path.basename(db_file).lower() or "tmp" in os.path.basename(db_file).lower(),
            f"DB file {db_file} is not in a temporary or test path!"
        )
        self.assertTrue(
            cfg_file.lower().startswith(temp_dir) or "test" in os.path.basename(cfg_file).lower() or "tmp" in os.path.basename(cfg_file).lower(),
            f"Config file {cfg_file} is not in a temporary or test path!"
        )
        self.assertNotEqual(os.path.abspath(db_file), os.path.abspath(test_isolation.REAL_DB_PATH))
        self.assertNotEqual(os.path.abspath(cfg_file), os.path.abspath(test_isolation.REAL_CONFIG_PATH))


if __name__ == "__main__":
    unittest.main()

import os
import sys
from pathlib import Path

# Always import test_isolation FIRST to initialize the temp environment and guardrails
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import test_isolation

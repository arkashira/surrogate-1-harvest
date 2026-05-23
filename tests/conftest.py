"""Shared pytest fixtures + path setup for the harvest test suite."""
from __future__ import annotations

import sys
from pathlib import Path

# Make `bin/` importable so `from lib.foo import bar` works under pytest.
REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

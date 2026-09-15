"""Compatibility wrapper — prefer tools/build_school_zones.py --state qld."""

from __future__ import annotations

import sys
from pathlib import Path

# Re-export via the parameterized builder.
sys.argv = [sys.argv[0], "--state", "qld"] + sys.argv[1:]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_school_zones import main

if __name__ == "__main__":
    raise SystemExit(main())

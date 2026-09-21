"""Compatibility wrapper — prefer tools/build_school_zones.py --state qld."""

from __future__ import annotations

import sys
from pathlib import Path


def _rewrite_legacy_out(argv: list[str]) -> list[str]:
    """Map legacy `--out FILE.gz` to `--out-dir PARENT`.

    The multi-state builder only accepts `--out-dir`. Older CI/docs used
    `--out dist/qld_school_zones.csv.gz`. Without this rewrite, argparse
    abbreviation can treat `--out` as `--out-dir` and mkdir the .gz path.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--out" and i + 1 < len(argv):
            out.extend(["--out-dir", str(Path(argv[i + 1]).expanduser().resolve().parent)])
            i += 2
            continue
        if arg.startswith("--out=") and not arg.startswith("--out-dir"):
            out.extend(["--out-dir", str(Path(arg.split("=", 1)[1]).expanduser().resolve().parent)])
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


sys.argv = [sys.argv[0], "--state", "qld"] + _rewrite_legacy_out(sys.argv[1:])
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_school_zones import main

if __name__ == "__main__":
    raise SystemExit(main())
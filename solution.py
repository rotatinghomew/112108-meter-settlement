#!/usr/bin/env python3
"""Convenience entry point: python solution.py [--input ...]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from billing_facts.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

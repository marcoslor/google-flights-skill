#!/opt/homebrew/bin/python3.14
"""flights-search.py — thin launcher; implementation lives in flights_search/.

Usage unchanged — see ../SKILL.md:
  flights-search.py --from GRU --to JFK --date 2026-09-01 [flags]
"""
import os
import sys

# resolve symlinks so the flights_search package next to the real file is importable
sys.path.insert(0, os.path.dirname(os.path.realpath(os.path.abspath(__file__))))

from flights_search.cli import main

if __name__ == "__main__":
    main()

"""Seed the database with 30 fake jobs (idempotent). Same as: python -m job_hunter seed"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_hunter.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["seed", *sys.argv[1:]]))

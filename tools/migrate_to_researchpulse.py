"""
Migrate complete academic data from Old DB (scientific_journal_db) to New DB (researchpulse).

Unified wrapper calling tools.migration_middleware.

Usage:
  python tools/migrate_to_researchpulse.py
  python tools/migrate_to_researchpulse.py --branch vn
  python tools/migrate_to_researchpulse.py --branch global
  python tools/migrate_to_researchpulse.py --clean
  python tools/migrate_to_researchpulse.py --table Article Author
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.migration_middleware import (
    ALL_TABLES,
    GLOBAL_TABLES,
    MigrationMiddleware,
    VN_TABLES,
    main_cli,
)

if __name__ == "__main__":
    main_cli()

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_REGISTRY = REPO_ROOT / "data" / "vietnam_journals" / "vn_journals_registry.json"


def load_env() -> None:
    """Load local env files without overriding already-exported variables."""
    for name in (".env.local", ".env.vercel", ".env"):
        path = REPO_ROOT / name
        if path.exists():
            load_dotenv(path, override=False)


def get_supabase_url() -> str:
    """Resolve a Supabase Postgres URL from env, then legacy tool fallback."""
    for key in (
        "SUPABASE_DATABASE_URL",
        "SUPABASE_DB_URL",
        "SUPABASE_POSTGRES_URL",
        "SUPABASE_URL",
        "LOCAL_DATABASE_URL",
        "DATABASE_URL",
    ):
        value = os.getenv(key)
        if value and value.startswith(("postgresql://", "postgresql+psycopg2://")):
            return value

    # Backward-compatible fallback used by existing export tools in this repo.
    try:
        from tools.export_custom_sql import SUPABASE_URL as legacy_supabase_url

        return legacy_supabase_url
    except Exception as exc:  # pragma: no cover - only used for CLI diagnostics
        raise RuntimeError(
            "Missing Supabase Postgres URL. Set SUPABASE_DATABASE_URL or SUPABASE_DB_URL."
        ) from exc


def load_journal(registry_path: Path, journal_code: str) -> dict[str, Any]:
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    journal = data.get(journal_code)
    if not isinstance(journal, dict):
        raise KeyError(f"Journal code not found in registry: {journal_code}")
    return journal


def normalize_issn(journal: dict[str, Any]) -> str | None:
    values = []
    for key in ("issn_print", "issn_online"):
        value = str(journal.get(key) or "").strip().replace("-", "")
        if value and value not in values:
            values.append(value)
    return ", ".join(values) if values else None


def preview_mapping(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "Publisher.display_name": journal.get("publisher") or journal.get("university"),
        "Zone": {
            "zone_id": 81,
            "code": "VN",
            "name": "Viet Nam",
            "type": "COUNTRY",
            "source": "existing Zone row",
        },
        "Journal": {
            "source_id": journal.get("base_url"),
            "display_name": journal.get("name_en") or journal.get("name_vi"),
            "type": journal.get("type") or "journal",
            "coverage": journal.get("coverage"),
            "issn": normalize_issn(journal),
            "subject_hint_not_in_db": journal.get("subject_hint"),
            "is_deleted": False,
        },
    }


def sync_identity_sequence(conn, table_name: str, id_column: str) -> None:
    """Move an identity/serial sequence past the current max id.

    Some existing SQL imports insert explicit IDs, leaving PostgreSQL's identity
    sequence behind. The next insert can then try to reuse an existing primary
    key. This keeps one-row VN imports safe without altering table data.
    """
    sequence_name = conn.execute(
        text("SELECT pg_get_serial_sequence(:table_name, :id_column)"),
        {"table_name": f'"{table_name}"', "id_column": id_column},
    ).scalar_one_or_none()
    if not sequence_name:
        return

    max_id = conn.execute(text(f'SELECT COALESCE(MAX("{id_column}"), 0) FROM "{table_name}"')).scalar_one()
    conn.execute(text("SELECT setval(:sequence_name, :next_value, true)"), {"sequence_name": sequence_name, "next_value": int(max_id) + 1})


def upsert_publisher(conn, display_name: str | None) -> int | None:
    if not display_name:
        return None
    row = conn.execute(
        text('SELECT "publisher_id" FROM "Publisher" WHERE "display_name" = :display_name'),
        {"display_name": display_name},
    ).fetchone()
    if row:
        return int(row[0])
    sync_identity_sequence(conn, "Publisher", "publisher_id")
    return int(
        conn.execute(
            text(
                'INSERT INTO "Publisher" ("display_name") '
                'VALUES (:display_name) RETURNING "publisher_id"'
            ),
            {"display_name": display_name},
        ).scalar_one()
    )


def get_vn_country_zone(conn) -> int:
    """Use the existing VN country row from Zone; fallback to code lookup."""
    row = conn.execute(
        text('SELECT "zone_id" FROM "Zone" WHERE "zone_id" = 81 AND "code" = :code'),
        {"code": "VN"},
    ).fetchone()
    if row:
        return int(row[0])

    row = conn.execute(
        text('SELECT "zone_id" FROM "Zone" WHERE "code" = :code AND "type" = :type ORDER BY "zone_id" LIMIT 1'),
        {"code": "VN", "type": "COUNTRY"},
    ).fetchone()
    if row:
        return int(row[0])

    raise RuntimeError('Cannot find existing Viet Nam Zone row. Expected Zone.zone_id=81 or code=VN.')


def upsert_journal(conn, journal: dict[str, Any], publisher_id: int | None, country_id: int | None) -> int:
    display_name = journal.get("name_en") or journal.get("name_vi")
    if not display_name:
        raise ValueError("Journal requires name_en or name_vi")

    source_id = journal.get("base_url")
    if source_id and ("about.lens.org" in source_id.lower() or "example.com" in source_id.lower()):
        source_id = None

    payload = {
        "owning_institution": journal.get("owning_institution"),
        "source_id": source_id,
        "publisher_id": publisher_id,
        "country": country_id,
        "display_name": display_name,
        "type": (journal.get("type") or "journal").lower(),
        "coverage": journal.get("coverage"),
        "issn": normalize_issn(journal),
        "is_deleted": False,
    }

    # Match by display_name first (case-insensitive)
    existing = conn.execute(
        text(
            'SELECT "journal_id" FROM "Journal" '
            'WHERE lower("display_name") = lower(:display_name) '
            'ORDER BY "journal_id" LIMIT 1'
        ),
        {"display_name": display_name},
    ).fetchone()

    # Match by source_id next if name wasn't found and we have a valid source_id
    if not existing and source_id:
        existing = conn.execute(
            text(
                'SELECT "journal_id" FROM "Journal" '
                'WHERE "source_id" = :source_id '
                'ORDER BY "journal_id" LIMIT 1'
            ),
            {"source_id": source_id},
        ).fetchone()

    if existing:
        journal_id = int(existing[0])
        conn.execute(
            text(
                'UPDATE "Journal" SET '
                '"owning_institution" = :owning_institution, '
                '"source_id" = :source_id, '
                '"publisher_id" = :publisher_id, '
                '"country" = :country, '
                '"display_name" = :display_name, '
                '"type" = :type, '
                '"coverage" = :coverage, '
                '"issn" = :issn, '
                '"is_deleted" = :is_deleted '
                'WHERE "journal_id" = :journal_id'
            ),
            {**payload, "journal_id": journal_id},
        )
        return journal_id

    sync_identity_sequence(conn, "Journal", "journal_id")
    return int(
        conn.execute(
            text(
                'INSERT INTO "Journal" '
                '("owning_institution", "source_id", "publisher_id", "country", "display_name", "type", "coverage", "issn", "is_deleted") '
                'VALUES (:owning_institution, :source_id, :publisher_id, :country, :display_name, :type, :coverage, :issn, :is_deleted) '
                'RETURNING "journal_id"'
            ),
            payload,
        ).scalar_one()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview/import one VN journal registry entry into Supabase")
    parser.add_argument("--journal-code", default="Acta_Mathematica_Vietnamica")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--execute", action="store_true", help="Actually write to Supabase. Omit for dry-run preview.")
    args = parser.parse_args()

    load_env()
    journal = load_journal(args.registry, args.journal_code)
    mapping = preview_mapping(journal)

    print("[PREVIEW] VN journal -> Supabase mapping")
    print(json.dumps(mapping, ensure_ascii=False, indent=2))

    if not args.execute:
        print("\n[DRY RUN] No database changes were made. Add --execute to import this journal.")
        return

    supabase_url = get_supabase_url()
    engine = create_engine(supabase_url)
    with engine.begin() as conn:
        publisher_id = upsert_publisher(conn, journal.get("publisher") or journal.get("university"))
        country_id = get_vn_country_zone(conn)
        journal_id = upsert_journal(conn, journal, publisher_id, country_id)

    print("\n[OK] Imported one journal into Supabase")
    print(json.dumps({"publisher_id": publisher_id, "country_zone_id": country_id, "journal_id": journal_id}, indent=2))


if __name__ == "__main__":
    main()

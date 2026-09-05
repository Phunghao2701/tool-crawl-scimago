from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

try:
    from tools.vn_journals.import_one_journal_supabase import sync_identity_sequence
except ImportError:
    from import_one_journal_supabase import sync_identity_sequence  # type: ignore

OPENALEX_PREFIXES = {
    "author": "A",
    "institution": "I",
    "work": "W",
}


@dataclass
class AffiliationStats:
    authors_unresolved: int = 0
    institutions_found: int = 0
    institution_links_inserted: int = 0
    skipped_missing_year: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "authors_unresolved": self.authors_unresolved,
            "institutions_found": self.institutions_found,
            "institution_links_inserted": self.institution_links_inserted,
            "skipped_missing_year": self.skipped_missing_year,
        }


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = re.sub(r"\s+", " ", str(value)).strip()
    return text_value or None


def normalize_openalex_id(value: Any, kind: str | None = None) -> str | None:
    text_value = clean_text(value)
    if not text_value:
        return None
    token = text_value.rstrip("/").split("/")[-1]
    if ":" in token:
        token = token.split(":")[-1]
    token = token.strip()
    if not token:
        return None
    prefix = OPENALEX_PREFIXES.get(kind or "")
    if prefix and token[:1].upper() != prefix and token.isdigit():
        token = f"{prefix}{token}"
    if prefix and token[:1].upper() == prefix:
        token = prefix + token[1:]
    return f"https://openalex.org/{token}"


def openalex_id_variants(value: Any, kind: str | None = None) -> dict[str, str | None]:
    normalized = normalize_openalex_id(value, kind)
    return {
        "openalex_id": normalized,
        "short_openalex_id": normalized.rsplit("/", 1)[-1] if normalized else None,
    }


def normalize_orcid(value: Any) -> str | None:
    text_value = clean_text(value)
    if not text_value:
        return None
    text_value = re.sub(r"^https?://orcid\.org/", "", text_value, flags=re.IGNORECASE)
    text_value = text_value.strip().upper()
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-[\dX]{4}", text_value):
        return None
    return text_value


def normalize_name_key(value: Any) -> str | None:
    text_value = clean_text(value)
    if not text_value:
        return None
    return re.sub(r"\s+", " ", text_value).casefold()


def normalize_institution_payload(inst: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(inst, dict):
        return None
    display_name = clean_text(inst.get("display_name") or inst.get("name"))
    if not display_name:
        return None
    country_code = clean_text(inst.get("country_code"))
    inst_type = clean_text(inst.get("type"))
    return {
        "openalex_id": normalize_openalex_id(
            inst.get("id") or inst.get("openalex_id"), "institution"
        ),
        "display_name": display_name,
        "country_code": country_code.upper() if country_code else None,
        "type": inst_type.lower() if inst_type else None,
    }


def normalized_author_payload(author_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_name": clean_text(
            author_data.get("name") or author_data.get("display_name")
        ),
        "openalex_id": normalize_openalex_id(
            author_data.get("openalex_author_id")
            or author_data.get("id")
            or author_data.get("openalex_id"),
            "author",
        ),
        "orcid": normalize_orcid(author_data.get("orcid")),
    }


def _first_mapping(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if isinstance(row, dict):
        return row
    return None


def resolve_author_id(conn, author_data: dict[str, Any]) -> int | None:
    payload = normalized_author_payload(author_data)
    openalex_id = payload["openalex_id"]
    orcid = payload["orcid"]
    name_key = normalize_name_key(payload["display_name"])

    if openalex_id:
        rows = conn.execute(
            text(
                'SELECT "author_id", "openalex_id" FROM "Author" '
                'WHERE "openalex_id" = :openalex_id OR "openalex_id" = :short_openalex_id'
            ),
            openalex_id_variants(openalex_id, "author"),
        ).fetchall()
        matching_rows = [
            row
            for row in rows
            if normalize_openalex_id(row[1], "author") == openalex_id
        ]
        matching_ids = {int(row[0]) for row in matching_rows}
        if len(matching_ids) > 1:
            raise ValueError(
                f"Ambiguous Author OpenAlex ID {openalex_id}: {sorted(matching_ids)}"
            )
        if matching_ids:
            return next(iter(matching_ids))

    if orcid:
        row = conn.execute(
            text(
                'SELECT "author_id", "openalex_id" FROM "Author" '
                'WHERE "orcid" = :orcid OR "orcid" = :orcid_url LIMIT 1'
            ),
            {"orcid": orcid, "orcid_url": f"https://orcid.org/{orcid}"},
        ).fetchone()
        if row:
            # ORCID is a unique identifier. If it matches, we assume it's the same author
            # even if OpenAlex has created a duplicate ID for them.
            return int(row[0])

    if name_key:
        rows = conn.execute(
            text(
                'SELECT "author_id", "openalex_id", "orcid" FROM "Author" '
                'WHERE lower(trim("display_name")) = :name_key'
            ),
            {"name_key": name_key},
        ).fetchall()
        safe_rows = []
        for row in rows:
            existing_openalex = normalize_openalex_id(row[1], "author")
            existing_orcid = normalize_orcid(row[2])
            if openalex_id and existing_openalex not in (None, openalex_id):
                continue
            if orcid and existing_orcid not in (None, orcid):
                continue
            safe_rows.append(row)
        if len(safe_rows) == 1:
            return int(safe_rows[0][0])
    return None


def upsert_author_safe(
    conn, author_data: dict[str, Any], cache: dict[str, int] | None = None
) -> int:
    payload = normalized_author_payload(author_data)
    if not payload["display_name"]:
        raise ValueError("Author requires display_name/name")

    cache_key = (
        payload["openalex_id"]
        or payload["orcid"]
        or normalize_name_key(payload["display_name"])
    )
    if cache is not None and cache_key and cache_key in cache:
        return cache[cache_key]

    author_id = resolve_author_id(conn, author_data)
    if author_id is not None:
        conn.execute(
            text(
                'UPDATE "Author" SET '
                '"openalex_id" = COALESCE("openalex_id", :openalex_id), '
                '"orcid" = COALESCE("orcid", :orcid), '
                '"display_name" = COALESCE("display_name", :display_name) '
                'WHERE "author_id" = :author_id'
            ),
            {**payload, "author_id": author_id},
        )
        if cache is not None and cache_key:
            cache[cache_key] = author_id
        return author_id

    sync_identity_sequence(conn, "Author", "author_id")
    try:
        if payload["openalex_id"]:
            author_id = int(
                conn.execute(
                    text(
                        'INSERT INTO "Author" ("orcid", "display_name", "openalex_id") '
                        'VALUES (:orcid, :display_name, :openalex_id) '
                        'ON CONFLICT ("openalex_id") DO UPDATE SET '
                        '"orcid" = COALESCE(EXCLUDED."orcid", "Author"."orcid"), '
                        '"display_name" = COALESCE(EXCLUDED."display_name", "Author"."display_name") '
                        'RETURNING "author_id"'
                    ),
                    payload,
                ).scalar_one()
            )
        elif payload["orcid"]:
            author_id = int(
                conn.execute(
                    text(
                        'INSERT INTO "Author" ("orcid", "display_name", "openalex_id") '
                        'VALUES (:orcid, :display_name, :openalex_id) '
                        'ON CONFLICT ("orcid") DO UPDATE SET '
                        '"display_name" = COALESCE(EXCLUDED."display_name", "Author"."display_name") '
                        'RETURNING "author_id"'
                    ),
                    payload,
                ).scalar_one()
            )
        else:
            author_id = int(
                conn.execute(
                    text(
                        'INSERT INTO "Author" ("orcid", "display_name", "openalex_id") '
                        'VALUES (:orcid, :display_name, :openalex_id) RETURNING "author_id"'
                    ),
                    payload,
                ).scalar_one()
            )
    except Exception:
        row = conn.execute(
            text('SELECT "author_id" FROM "Author" WHERE lower(trim("display_name")) = lower(trim(:display_name)) LIMIT 1'),
            payload,
        ).fetchone()
        if row:
            author_id = int(row[0])
        else:
            raise
    if cache is not None and cache_key:
        cache[cache_key] = author_id
    return author_id


def upsert_institution(conn, inst: dict[str, Any]) -> int | None:
    payload = normalize_institution_payload(inst)
    if not payload:
        return None

    if payload["openalex_id"]:
        row = conn.execute(
            text(
                'SELECT "institution_id" FROM "Institution" '
                'WHERE "openalex_id" = :openalex_id OR "openalex_id" = :short_openalex_id'
            ),
            {**payload, **openalex_id_variants(payload["openalex_id"], "institution")},
        ).fetchone()
        if row:
            institution_id = int(row[0])
            conn.execute(
                text(
                    'UPDATE "Institution" SET '
                    '"display_name" = COALESCE(:display_name, "display_name"), '
                    '"country_code" = COALESCE(:country_code, "country_code"), '
                    '"type" = COALESCE(:type, "type"), '
                    '"is_deleted" = false '
                    'WHERE "institution_id" = :institution_id'
                ),
                {**payload, "institution_id": institution_id},
            )
            return institution_id

    rows = conn.execute(
        text(
            'SELECT "institution_id", "openalex_id" FROM "Institution" '
            'WHERE lower(trim("display_name")) = lower(trim(:display_name)) '
            "AND COALESCE(upper(trim(\"country_code\")), '') = COALESCE(:country_code, '') "
            "AND COALESCE(lower(trim(\"type\")), '') = COALESCE(:type, '')"
        ),
        payload,
    ).fetchall()
    if len(rows) > 1:
        return None
    safe_rows = []
    for row in rows:
        existing_openalex_id = normalize_openalex_id(row[1], "institution")
        if payload["openalex_id"] and existing_openalex_id not in (
            None,
            payload["openalex_id"],
        ):
            return None
        safe_rows.append(row)
    if len(safe_rows) == 1:
        row = safe_rows[0]
        if payload["openalex_id"] and normalize_openalex_id(
            row[1], "institution"
        ) not in (None, payload["openalex_id"]):
            return None
        institution_id = int(row[0])
        conn.execute(
            text(
                'UPDATE "Institution" SET '
                '"openalex_id" = COALESCE("openalex_id", :openalex_id), '
                '"is_deleted" = false '
                'WHERE "institution_id" = :institution_id'
            ),
            {**payload, "institution_id": institution_id},
        )
        return institution_id

    if rows:
        return None

    sync_identity_sequence(conn, "Institution", "institution_id")
    try:
        if payload["openalex_id"]:
            return int(
                conn.execute(
                    text(
                        'INSERT INTO "Institution" ("openalex_id", "display_name", "country_code", "type") '
                        'VALUES (:openalex_id, :display_name, :country_code, :type) '
                        'ON CONFLICT ("openalex_id") DO UPDATE SET '
                        '"display_name" = COALESCE(EXCLUDED."display_name", "Institution"."display_name"), '
                        '"country_code" = COALESCE(EXCLUDED."country_code", "Institution"."country_code"), '
                        '"type" = COALESCE(EXCLUDED."type", "Institution"."type"), '
                        '"is_deleted" = false '
                        'RETURNING "institution_id"'
                    ),
                    payload,
                ).scalar_one()
            )
        return int(
            conn.execute(
                text(
                    'INSERT INTO "Institution" ("openalex_id", "display_name", "country_code", "type") '
                    'VALUES (:openalex_id, :display_name, :country_code, :type) '
                    'RETURNING "institution_id"'
                ),
                payload,
            ).scalar_one()
        )
    except Exception:
        row = conn.execute(
            text('SELECT "institution_id" FROM "Institution" WHERE lower(trim("display_name")) = lower(trim(:display_name)) LIMIT 1'),
            payload,
        ).fetchone()
        if row:
            return int(row[0])
        raise


def insert_institution_author_link(
    conn, author_id: int, institution_id: int, year: int
) -> int:
    result = conn.execute(
        text(
            'INSERT INTO "Institution_Author" ("author_id", "institution_id", "year") '
            "VALUES (:author_id, :institution_id, :year) "
            "ON CONFLICT DO NOTHING"
        ),
        {"author_id": author_id, "institution_id": institution_id, "year": int(year)},
    )
    return int(result.rowcount or 0)


def persist_article_authorship_institutions(
    conn,
    article_id: int,
    publication_year: int | None,
    authorships: list[dict[str, Any]],
    author_cache: dict[str, int] | None = None,
) -> AffiliationStats:
    stats = AffiliationStats()
    if publication_year is None:
        stats.skipped_missing_year += sum(
            len(a.get("institutions") or []) for a in authorships
        )
        return stats

    for authorship in authorships:
        author_id = upsert_author_safe(conn, authorship, author_cache)
        conn.execute(
            text(
                'INSERT INTO "Author_Article" ("author_id", "article_id", "author_position") '
                "VALUES (:author_id, :article_id, :author_position) "
                'ON CONFLICT ("author_id", "article_id") DO UPDATE SET '
                '"author_position" = COALESCE(EXCLUDED."author_position", "Author_Article"."author_position")'
            ),
            {
                "author_id": author_id,
                "article_id": article_id,
                "author_position": authorship.get("author_position"),
            },
        )
        for inst in authorship.get("institutions") or []:
            institution_id = upsert_institution(conn, inst)
            if institution_id is None:
                continue
            stats.institutions_found += 1
            stats.institution_links_inserted += insert_institution_author_link(
                conn, author_id, institution_id, int(publication_year)
            )
    return stats

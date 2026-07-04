"""
Export a subset of specific tables with a custom row limit to a SQL file,
and optionally import it directly into Supabase.

Usage:
  python tools/export_subset_sql.py --tables Journal Article --limit 500 --output data/subset.sql
  python tools/export_subset_sql.py --tables ALL --limit 100 --import-to-supabase
"""

import argparse
import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db",
)
SUPABASE_URL = "postgresql://postgres.egyrzaqtmxmcezxchfrl:TeamSWP3912006@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require"


def format_sql_value(val):
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, datetime):
        return f"'{val.isoformat()}'"
    if isinstance(val, (dict, list)):
        escaped = json.dumps(val, ensure_ascii=False).replace("'", "''")
        return f"CAST('{escaped}' AS JSONB)"
    # String types
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


def get_all_tables(engine):
    query = text('''
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    ''')
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    return [r[0] for r in rows]


def export_subset(tables: list, limit: int, output_path: str):
    engine = create_engine(DATABASE_URL)
    all_available = get_all_tables(engine)

    # Resolve "ALL"
    target_tables = []
    if len(tables) == 1 and tables[0].upper() == "ALL":
        target_tables = all_available
    else:
        for t in tables:
            # Case insensitive check
            matched = [a for a in all_available if a.lower() == t.lower()]
            if matched:
                target_tables.append(matched[0])
            else:
                print(f"[WARNING] Table '{t}' not found in database. Skipping.")

    if not target_tables:
        print("[ERROR] No valid tables to export.")
        return False

    print(f"Exporting tables: {', '.join(target_tables)}")
    print(f"Row limit per table: {'ALL' if limit == 0 else limit}")
    print(f"Output SQL file: {output_path}")

    # Ensure dir exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        # Header setup to bypass FK checks on target
        f.write("-- Sub-set SQL Dump\n")
        f.write("SET session_replication_role = replica;\n\n")

        for table in target_tables:
            f.write(f"-- ----------------------------------------\n")
            f.write(f"-- Table: {table}\n")
            f.write(f"-- ----------------------------------------\n")

            # Fetch columns
            col_query = text(f'SELECT * FROM "{table}" LIMIT 0')
            with engine.connect() as conn:
                res = conn.execute(col_query)
                columns = res.keys()

            # Fetch rows
            select_sql = f'SELECT * FROM "{table}"'
            if limit > 0:
                select_sql += f' LIMIT {limit}'
            
            with engine.connect() as conn:
                rows = conn.execute(text(select_sql)).fetchall()

            print(f"  - Table '{table}': exporting {len(rows)} row(s)...")

            if not rows:
                f.write(f"-- No data in table {table}\n\n")
                continue

            col_names_str = ", ".join([f'"{col}"' for col in columns])

            for row in rows:
                vals = []
                for val in row:
                    vals.append(format_sql_value(val))
                vals_str = ", ".join(vals)
                f.write(f'INSERT INTO "{table}" ({col_names_str}) VALUES ({vals_str}) ON CONFLICT DO NOTHING;\n')
            
            f.write("\n")

        f.write("SET session_replication_role = DEFAULT;\n")

    print(f"[OK] Exported successfully to {output_path}")
    return True


def import_to_supabase(sql_path: str):
    print(f"\n[INFO] Importing {sql_path} into Supabase...")
    try:
        engine_supa = create_engine(SUPABASE_URL)
        with open(sql_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Split SQL statements
        statements = content.split(";\n")
        
        with engine_supa.begin() as conn:
            # Set replica first
            conn.execute(text("SET session_replication_role = replica;"))
            
            executed = 0
            for stmt in statements:
                stmt_clean = stmt.strip()
                if not stmt_clean or stmt_clean.startswith("--"):
                    continue
                conn.execute(text(stmt_clean))
                executed += 1
                
            conn.execute(text("SET session_replication_role = DEFAULT;"))

        print(f"[OK] Executed {executed} SQL statements on Supabase successfully!")
    except Exception as e:
        print(f"[ERROR] Supabase import failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Export database subset to SQL inserts for Supabase")
    parser.add_argument(
        "--tables",
        nargs="+",
        required=True,
        help="List of tables to export (e.g. Journal Article), or ALL to export all tables",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows per table (default 100, 0 means all)",
    )
    parser.add_argument(
        "--output",
        default="data/subset_export.sql",
        help="Path to save the output SQL file",
    )
    parser.add_argument(
        "--import-to-supabase",
        action="store_true",
        help="Directly import the generated SQL into Supabase",
    )

    args = parser.parse_args()
    success = export_subset(args.tables, args.limit, args.output)
    if success and args.import_to_supabase:
        import_to_supabase(args.output)


if __name__ == "__main__":
    main()

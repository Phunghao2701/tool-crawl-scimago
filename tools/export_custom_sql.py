"""
Export custom database subsets to SQL inserts, filtering by columns, WHERE conditions, and limits.
Optionally imports the generated SQL file directly into Supabase.

Usage:
  python tools/export_custom_sql.py --config data/export_config.json --output data/custom_subset.sql
  python tools/export_custom_sql.py --config data/export_config.json --import-to-supabase
"""

import argparse
import os
import sys
import json
import subprocess
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
DOCKER_CONTAINER = os.getenv("LOCAL_PG_CONTAINER", "scientific_journal_postgres")
DOCKER_SQL_PATH = "/tmp/custom_export.sql"


def resolve_project_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


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


def get_table_columns(engine, table_name: str) -> list:
    query = text(f'SELECT * FROM "{table_name}" LIMIT 0')
    with engine.connect() as conn:
        res = conn.execute(query)
        return list(res.keys())


def get_primary_key_columns(engine, table_name: str) -> list:
    query = text('''
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = :table_name
        ORDER BY kcu.ordinal_position
    ''')
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(query, {"table_name": table_name}).fetchall()]


def generate_default_config(engine, path: str):
    path = resolve_project_path(path)
    tables = get_all_tables(engine)
    config = {"tables": []}
    
    # Priority order for tables to avoid FK issues if possible
    # though replica role bypasses this, it's nice to keep clean
    priority_order = [
        "Region", "Country", "Publisher", "Topic", "Journal",
        "Article", "Author", "ArticleAuthor"
    ]
    
    sorted_tables = []
    # Add prioritized first
    for pt in priority_order:
        for t in tables:
            if t.lower() == pt.lower():
                sorted_tables.append(t)
                break
    # Add the rest
    for t in tables:
        if t not in sorted_tables:
            sorted_tables.append(t)

    for t in sorted_tables:
        cols = get_table_columns(engine, t)
        # Default config setup
        t_conf = {
            "name": t,
            "columns": cols,
            "where": "",
            "order_by": "",
            "limit": 100
        }
        # Add custom logic to defaults for demonstration
        if t == "Article":
            t_conf["where"] = "reference_count > 0"
            t_conf["order_by"] = "reference_count DESC"
            t_conf["limit"] = 500
        elif t == "Journal":
            t_conf["limit"] = 100
        elif t == "Country" or t == "Region" or t == "Topic":
            t_conf["limit"] = 0  # Export all lookups
            
        config["tables"].append(t_conf)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"[INFO] Generated default configuration at: {path}")


def export_custom(config_path: str, output_path: str):
    config_path = resolve_project_path(config_path)
    output_path = resolve_project_path(output_path)
    engine = create_engine(DATABASE_URL)
    
    if not os.path.exists(config_path):
        generate_default_config(engine, config_path)
        print("[INFO] Please edit the configuration file and run again if you wish to customize it.")
        
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    all_available = get_all_tables(engine)
    tables_config = config.get("tables", [])
    dependency_order = [
        "Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric",
        "Topic", "Journal", "Journal_Subject_Category",
        "Volume", "Issue",
        "Article", "Author", "Author_Article",
        "Keyword", "Keyword_Article", "Sub_Topic",
        "Journal_Ranking", "Journal_Ranking_Subject_Category",
        "user", "Password_Reset_Token", "Project", "Project_Journal",
        "Project_Keyword", "Subject_Category_Project",
    ]
    order_map = {name.lower(): idx for idx, name in enumerate(dependency_order)}
    tables_config = sorted(
        tables_config,
        key=lambda t: order_map.get((t.get("name") or "").lower(), len(order_map)),
    )

    print(f"Reading configuration from: {config_path}")
    print(f"Output SQL file: {output_path}")

    # Ensure output dir exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("-- Custom SQL Dump\n")
        f.write("-- Tables are exported in dependency order for Supabase import.\n")
        f.write("SET statement_timeout = 0;\n")
        f.write("SET lock_timeout = 0;\n")
        f.write("BEGIN;\n\n")

        for t_conf in tables_config:
            name = t_conf.get("name")
            matched = [a for a in all_available if a.lower() == name.lower()]
            if not matched:
                print(f"[WARNING] Table '{name}' not found in database. Skipping.")
                continue
            table = matched[0]

            # Columns to export
            req_cols = t_conf.get("columns") or []
            db_cols = get_table_columns(engine, table)
            
            # Filter valid columns
            columns = []
            for col in req_cols:
                if col in db_cols:
                    columns.append(col)
                else:
                    print(f"  [WARNING] Column '{col}' not found in table '{table}'. Skipping column.")
            
            if not columns:
                columns = db_cols  # Fallback to all if none valid

            where = t_conf.get("where", "").strip()
            order_by = t_conf.get("order_by", "").strip()
            limit = t_conf.get("limit", 0)

            # Build query
            col_select = ", ".join([f'"{c}"' for c in columns])
            select_sql = f'SELECT {col_select} FROM "{table}"'
            
            if where:
                select_sql += f' WHERE {where}'
            if order_by:
                select_sql += f' ORDER BY {order_by}'
            if limit > 0:
                select_sql += f' LIMIT {limit}'

            f.write(f"-- ----------------------------------------\n")
            f.write(f"-- Table: {table}\n")
            f.write(f"-- Query: {select_sql}\n")
            f.write(f"-- ----------------------------------------\n")

            try:
                with engine.connect() as conn:
                    rows = conn.execute(text(select_sql)).fetchall()
            except Exception as e:
                print(f"[ERROR] Failed to query table '{table}': {e}")
                f.write(f"-- ERROR querying table {table}: {e}\n\n")
                continue

            print(f"  - Table '{table}': exporting {len(rows)} row(s)...")

            if not rows:
                f.write(f"-- No data matched for table {table}\n\n")
                continue

            col_names_str = ", ".join([f'"{col}"' for col in columns])
            pk_columns = [col for col in get_primary_key_columns(engine, table) if col in columns]
            non_pk_columns = [col for col in columns if col not in pk_columns]
            if pk_columns and non_pk_columns:
                conflict_cols = ", ".join([f'"{col}"' for col in pk_columns])
                update_set = ", ".join([f'"{col}" = EXCLUDED."{col}"' for col in non_pk_columns])
                conflict_clause = f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}"
            else:
                conflict_clause = "ON CONFLICT DO NOTHING"

            chunk_size = 500
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i:i + chunk_size]
                chunk_vals_strs = []
                for row in chunk:
                    vals = []
                    for val in row:
                        vals.append(format_sql_value(val))
                    chunk_vals_strs.append("(" + ", ".join(vals) + ")")
                
                all_vals = ",\n  ".join(chunk_vals_strs)
                f.write(f'INSERT INTO "{table}" ({col_names_str}) VALUES\n  {all_vals}\n{conflict_clause};\n')
            
            f.write("\n")

        f.write("COMMIT;\n")

    print(f"[OK] Exported successfully to {output_path}")
    return True


def import_to_supabase(sql_path: str):
    sql_path = resolve_project_path(sql_path)
    print(f"\n[INFO] Importing {sql_path} into Supabase via Docker + psql...")
    print(f"[INFO] Docker container: {DOCKER_CONTAINER}")

    copy_cmd = ["docker", "cp", sql_path, f"{DOCKER_CONTAINER}:{DOCKER_SQL_PATH}"]
    import_cmd = [
        "docker", "exec", "-i", DOCKER_CONTAINER,
        "psql", SUPABASE_URL,
        "-v", "ON_ERROR_STOP=1",
        "-f", DOCKER_SQL_PATH,
    ]

    try:
        print("[1/2] Copying SQL file into Docker container...")
        subprocess.run(copy_cmd, check=True)

        print("[2/2] Running psql import to Supabase...")
        subprocess.run(import_cmd, check=True)

        print("[OK] Supabase import finished.")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Supabase import failed with exit code {e.returncode}.")
        raise


def main():
    parser = argparse.ArgumentParser(description="Export database custom subsets to SQL inserts")
    parser.add_argument(
        "--config",
        default="data/export_config.json",
        help="Path to the JSON configuration file",
    )
    parser.add_argument(
        "--output",
        default="data/custom_export.sql",
        help="Path to save the output SQL file",
    )
    parser.add_argument(
        "--import-to-supabase",
        action="store_true",
        help="Directly import the generated SQL into Supabase",
    )

    args = parser.parse_args()
    success = export_custom(args.config, args.output)
    if success and args.import_to_supabase:
        import_to_supabase(args.output)


if __name__ == "__main__":
    main()

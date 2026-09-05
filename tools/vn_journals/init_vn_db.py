import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from tools.vn_journals.import_one_journal_supabase import load_env, get_supabase_url

def main():
    print("Database Initializer for ScienceJournalTrendingVN")
    print("=================================================")
    load_env()
    try:
        db_url = get_supabase_url()
        print(f"[INFO] Using database URL: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    except Exception as e:
        print(f"[ERROR] Could not resolve database URL: {e}")
        print("Please configure SUPABASE_DATABASE_URL in .env or .env.local")
        sys.exit(1)

    schema_file = REPO_ROOT / "database" / "schema.sql"
    if not schema_file.exists():
        print(f"[ERROR] Schema file not found at: {schema_file}")
        sys.exit(1)

    print(f"[INFO] Reading schema from: {schema_file.name}")
    with open(schema_file, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    # Filter out empty statements or lines if needed, but psycopg2 can execute whole SQL script
    print("[INFO] Connecting to Supabase database and creating tables...")
    engine = create_engine(db_url)
    
    try:
        # Use raw connection to execute the entire schema.sql script containing multiple statements
        with engine.connect() as conn:
            # We use execution_options(autocommit=True) or transaction block
            trans = conn.begin()
            try:
                # Splitting by statement is sometimes safer in SQLAlchemy, but executing the script as-is is standard
                # We can execute using raw connection to avoid SQLAlchemy's parser parsing bindings
                raw_conn = conn.connection
                with raw_conn.cursor() as cursor:
                    cursor.execute(schema_sql)
                trans.commit()
                print("[OK] Database schema initialized successfully.")
            except Exception as e:
                trans.rollback()
                print(f"[ERROR] Transaction failed, rolled back: {e}")
                sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}")
        sys.exit(1)

    # Optional: Seed ranking metric
    seed_file = REPO_ROOT / "database" / "seed_ranking_metric.sql"
    if seed_file.exists():
        print(f"[INFO] Seeding default metrics from: {seed_file.name}")
        with open(seed_file, "r", encoding="utf-8") as f:
            seed_sql = f.read()
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    raw_conn = conn.connection
                    with raw_conn.cursor() as cursor:
                        cursor.execute(seed_sql)
                    trans.commit()
                    print("[OK] Seed data initialized successfully.")
                except Exception as e:
                    trans.rollback()
                    print(f"[WARNING] Seed transaction failed: {e}")
        except Exception as e:
            print(f"[WARNING] Could not run seed script: {e}")

    # Run migrations to guarantee all VN tables, columns, indexes exist
    try:
        from tools.vn_journals.migrate_vn_db import main as run_migration
        print("\n[INFO] Running VN database migrations to ensure schema alignment...")
        run_migration()
    except Exception as e:
        print(f"[WARNING] Migration step had warning/error: {e}")

if __name__ == "__main__":
    main()

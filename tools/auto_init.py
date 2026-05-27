import os
import sys
import time
import subprocess
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:1234@postgres:5432/scientific_journal_db"
)
engine = create_engine(DATABASE_URL)

def wait_for_postgres():
    print("[auto-init] Waiting for PostgreSQL database to be ready...")
    for i in range(15):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                print("[auto-init] PostgreSQL port is open and responding.")
                return True
        except Exception:
            time.sleep(2)
    print("[auto-init] PostgreSQL is not ready after 30s.")
    return False

def wait_for_schema():
    print("[auto-init] Waiting for database schema initialization...")
    for i in range(15):
        try:
            with engine.connect() as conn:
                # Kiểm tra xem bảng Journal đã được tạo bởi docker-entrypoint-initdb chưa
                conn.execute(text('SELECT COUNT(*) FROM "Journal"'))
                print("[auto-init] Database schema is ready.")
                return True
        except Exception:
            time.sleep(2)
    print("[auto-init] Database schema is not ready yet.")
    return False

def main():
    if not wait_for_postgres():
        sys.exit(1)
        
    if not wait_for_schema():
        sys.exit(1)

    try:
        with engine.connect() as conn:
            count = conn.execute(text('SELECT COUNT(*) FROM "Journal"')).scalar()
            
        if count > 0:
            print(f"[auto-init] Database already has {count} journals. Skipping auto-import.")
        else:
            print("[auto-init] Database is empty. Starting automatic ETL pipeline...")
            filepath = "data/scimagojr 2025.csv"
            if os.path.exists(filepath):
                print(f"[auto-init] Importing {filepath}...")
                subprocess.run([
                    sys.executable, 
                    "tools/scimago_etl.py", 
                    "import", 
                    "--file", filepath, 
                    "--year", "2025"
                ], check=True)
                
                print("[auto-init] Syncing with OpenAlex (top 50 journals)...")
                subprocess.run([
                    sys.executable, 
                    "tools/openalex_sync.py", 
                    "sync", 
                    "--limit", "50"
                ], check=True)
                
                print("[auto-init] Automatic ETL setup completed successfully!")
            else:
                print(f"[auto-init] Warning: File {filepath} not found. Cannot auto-import.")
    except Exception as e:
        print(f"[auto-init] Error during initialization: {e}")

if __name__ == "__main__":
    main()

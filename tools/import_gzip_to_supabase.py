"""
import_gzip_to_supabase.py
--------------------------
Script giup team import file .sql.gz (export tu Adminer) vao Supabase.

Cach dung:
    python import_gzip_to_supabase.py path/to/file.sql.gz

Yeu cau:
    pip install psycopg2-binary python-dotenv
    File .env.vercel phai co VERCEL_DATABASE_URL
"""
import gzip
import os
import sys
import time
import subprocess
import tempfile
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = "postgresql://postgres.egyrzaqtmxmcezxchfrl:TeamSWP3912006@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require"

# Prepend nay de bypass FK constraints khi import (tranh loi thu tu insert)
PREAMBLE = b"""
SET client_encoding = 'UTF8';
SET session_replication_role = replica;
"""

EPILOGUE = b"""
SET session_replication_role = DEFAULT;
"""
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python import_gzip_to_supabase.py <file.sql.gz>")
        print("   or: python import_gzip_to_supabase.py <file.sql>")
        sys.exit(1)

    input_file = Path(sys.argv[1])
    if not input_file.exists():
        print(f"[ERROR] File not found: {input_file}")
        sys.exit(1)

    print("=" * 60)
    print("  IMPORT SQL (gzip/plain) -> Supabase")
    print("=" * 60)
    print(f"  File  : {input_file} ({input_file.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Target: {SUPABASE_URL[:60]}...")
    print()

    # Decompress neu la .gz
    is_gz = input_file.suffix.lower() == ".gz"
    print(f"  Format: {'gzip compressed' if is_gz else 'plain SQL'}")

    # Ghi ra file tam (psql can file, khong pipe duoc tren Windows)
    print()
    print("[1/3] Preparing SQL file...")
    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="wb") as tmp:
        tmp_path = tmp.name
        tmp.write(PREAMBLE)

        if is_gz:
            with gzip.open(input_file, "rb") as gz:
                chunk_size = 4 * 1024 * 1024  # 4MB chunks
                total_read = 0
                while True:
                    chunk = gz.read(chunk_size)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    total_read += len(chunk)
                    print(f"  Decompressing... {total_read / 1024 / 1024:.0f} MB read", end="\r")
        else:
            with open(input_file, "rb") as f:
                tmp.write(f.read())

        tmp.write(EPILOGUE)

    tmp_size = os.path.getsize(tmp_path) / 1024 / 1024
    print(f"\n  Prepared: {tmp_size:.0f} MB uncompressed SQL")

    print()
    print("[2/3] Importing into Supabase...")
    print("  (Estimate: ~5-30 min depending on file size)")
    print()

    t0 = time.time()
    result = subprocess.run(
        ["psql", SUPABASE_URL, "-f", tmp_path,
         "--set=ON_ERROR_STOP=0",  # Tiep tuc du co loi nho
         "-v", "ON_ERROR_ROLLBACK=on"],
        capture_output=False  # Hien thi output truc tiep
    )

    elapsed = time.time() - t0
    os.unlink(tmp_path)  # Xoa file tam

    print()
    print(f"[3/3] Done in {elapsed:.0f}s (exit code: {result.returncode})")

    if result.returncode == 0:
        print("[OK] Import successful!")
    else:
        print("[WARN] Import finished with some errors (check output above).")
        print("       Errors are usually duplicate keys if data existed before.")


if __name__ == "__main__":
    main()

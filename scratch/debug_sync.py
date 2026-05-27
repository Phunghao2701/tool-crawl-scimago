import os
import sys
import time
import requests
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

# Reset 45 journals để phục vụ debug
print("Resetting openalex_synced_at for test journals...")
with engine.begin() as conn:
    conn.execute(text("""
        UPDATE "Journal" 
        SET openalex_synced_at = NULL 
        WHERE openalex_id IS NULL AND openalex_synced_at IS NOT NULL
    """))

# Lấy 5 journal chưa sync để chạy thử
query = """
    SELECT journal_id, display_name, issn 
    FROM "Journal" 
    WHERE openalex_synced_at IS NULL
    LIMIT 5
"""
with engine.connect() as conn:
    journals = conn.execute(text(query)).fetchall()

print("\n--- Test Journals ---")
issn_list = []
issn_map = {}
for j in journals:
    print(f"Name: {j[1]}, ISSN: {j[2]}")
    # split issn
    import re
    parts = re.split(r"[,;/\s]+", j[2] or "")
    for p in parts:
        p = p.strip().replace("-", "").upper()
        if re.match(r"^\d{7}[\dX]$", p):
            issn_list.append(p)
            issn_map[p] = j[1]

if not issn_list:
    print("No valid ISSNs found for test!")
    sys.exit(0)

# Gom nhóm ISSN bằng bộ lọc OR
formatted_issns = []
for issn in issn_list:
    if len(issn) == 8:
        formatted_issns.append(f"{issn[:4]}-{issn[4:]}")
    else:
        formatted_issns.append(issn)

filter_str = "|".join(formatted_issns)
url = f"https://api.openalex.org/sources?filter=issn:{filter_str}&per_page=50"
print(f"\nRequesting: {url}")

r = requests.get(url)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    results = data.get("results", [])
    print(f"Results returned: {len(results)}")
    if results:
        print("\n--- Example Source Keys and Values related to ISSN ---")
        first_source = results[0]
        for key, val in first_source.items():
            if "issn" in key.lower():
                print(f"Key: {key} -> Type: {type(val)} -> Value: {val}")
    for source in results:
        print(f"\nOpenAlex Source Name: {source.get('display_name')}")
        # In tất cả các trường chứa chữ 'issn'
        for key, val in source.items():
            if "issn" in key.lower():
                print(f"  Field '{key}': {val}")
        matched = False
        # Thử lấy các trường issn có thể có
        issns_to_test = []
        if isinstance(source.get('issns'), list):
            issns_to_test.extend(source.get('issns'))
        elif isinstance(source.get('issns'), str):
            issns_to_test.append(source.get('issns'))
        if source.get('issn_l'):
            issns_to_test.append(source.get('issn_l'))
            
        for s_issn in issns_to_test:
            clean_s = s_issn.replace("-", "").upper()
            if clean_s in issn_map:
                print(f"  -> MATCHED WITH DB JOURNAL: '{issn_map[clean_s]}' (ISSN in DB: {clean_s})")
                matched = True
        if not matched:
            print("  -> Could not match with any test journals!")
else:
    print(r.text)

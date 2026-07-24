"""
Research Graph Sync
Standalone synchronization tool for PostgreSQL to Neo4j.
Optimized with Bulk Insert (UNWIND) for Volume and Issue hierarchies.
"""
import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
if TOOLS_DIR.is_dir() and str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire

# Load environment variables
load_dotenv()

import psycopg2
from neo4j import GraphDatabase

def execute_batch(session, query, batch, batch_size=500):
    if not batch:
        return
    for i in range(0, len(batch), batch_size):
        session.run(query, batch=batch[i:i + batch_size])

def run_sync(sync_type="full", limit=None):
    limit_str = f"{limit} records per entity" if limit else "all records"
    print(f"🚀 Starting {sync_type} sync (Limit: {limit_str})...")
    success = False
    
    postgres_host = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port = os.getenv("POSTGRES_PORT", "5432")
    postgres_db = os.getenv("POSTGRES_DATABASE", "postgres")
    postgres_user = os.getenv("POSTGRES_USER", "postgres")
    postgres_password = os.getenv("POSTGRES_PASSWORD", "")
    
    # Neo4j connection (support both local bolt:// and Aura neo4j+s://)
    # Priority: NEO4J_URI > NEO4J_HOST/NEO4J_PORT
    neo4j_uri = os.getenv(
        "NEO4J_URI",
        f"bolt://{os.getenv('NEO4J_HOST', 'localhost')}:{os.getenv('NEO4J_PORT', '7687')}"
    )

    # Priority: NEO4J_USERNAME/NEO4J_PASSWORD > NEO4J_USER/NEO4J_PASSWORD
    neo4j_user = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password = os.getenv("NEO4J_PASSWORD", "")

    # Log connection target without leaking password
    neo4j_target_for_log = neo4j_uri
    print(f"  -> Neo4j URI: {neo4j_target_for_log}")

    
    limit_clause = f" LIMIT {limit}" if (limit is not None and limit > 0) else ""
    
    try:
        print("⏳ Connecting to Databases...")
        pg_conn = psycopg2.connect(
            host=postgres_host, port=postgres_port, dbname=postgres_db,
            user=postgres_user, password=postgres_password
        )
        pg_cursor = pg_conn.cursor()
        
        neo4j_driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        neo4j_driver.verify_connectivity()
        print("✅ Databases connected successfully!")
 
        with neo4j_driver.session() as session:
            # --- 0. Initialize Neo4j Schema (Constraints & Indexes) ---
            print("  -> Initializing Neo4j Schema (Constraints & Indexes)...")
            session.run("CREATE CONSTRAINT publisher_id_constraint IF NOT EXISTS FOR (p:Publisher) REQUIRE p.id IS UNIQUE")
            session.run("CREATE CONSTRAINT journal_id_constraint IF NOT EXISTS FOR (j:Journal) REQUIRE j.id IS UNIQUE")
            session.run("CREATE CONSTRAINT volume_id_constraint IF NOT EXISTS FOR (v:Volume) REQUIRE v.id IS UNIQUE")
            session.run("CREATE CONSTRAINT issue_id_constraint IF NOT EXISTS FOR (i:Issue) REQUIRE i.id IS UNIQUE")
            session.run("CREATE CONSTRAINT author_id_constraint IF NOT EXISTS FOR (a:Author) REQUIRE a.id IS UNIQUE")
            session.run("CREATE CONSTRAINT article_id_constraint IF NOT EXISTS FOR (a:Article) REQUIRE a.id IS UNIQUE")
            session.run("CREATE CONSTRAINT topic_id_constraint IF NOT EXISTS FOR (t:Topic) REQUIRE t.id IS UNIQUE")
            session.run("CREATE CONSTRAINT keyword_id_constraint IF NOT EXISTS FOR (k:Keyword) REQUIRE k.id IS UNIQUE")
            session.run("CREATE CONSTRAINT institution_id_constraint IF NOT EXISTS FOR (i:Institution) REQUIRE i.id IS UNIQUE")
            session.run("CREATE INDEX article_doi_idx IF NOT EXISTS FOR (a:Article) ON (a.doi)")
            
            # --- 1. Sync Publishers ---
            print("  -> Syncing Publishers...")
            pg_cursor.execute(f'SELECT publisher_id, display_name FROM "Publisher"{limit_clause}')
            pub_data = [{"id": row[0], "name": row[1]} for row in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (p:Publisher {id: row.id}) SET p.name = row.name
            """, pub_data)
                
            # --- 2. Sync Journals ---
            print("  -> Syncing Journals...")
            pg_cursor.execute(f'SELECT journal_id, display_name, type, is_open_access, is_oa_diamond, issn, publisher_id FROM "Journal" WHERE is_deleted = false{limit_clause}')
            journal_data = []
            for row in pg_cursor.fetchall():
                journal_data.append({
                    "id": row[0], "name": row[1], "type": row[2], 
                    "oa": row[3], "doa": row[4], "issn": row[5], "pub_id": row[6]
                })
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (j:Journal {id: row.id})
                SET j.name = row.name, j.type = row.type, j.open_access = row.oa, j.diamond_oa = row.doa, j.issn = row.issn
                WITH j, row WHERE row.pub_id IS NOT NULL
                MATCH (p:Publisher {id: row.pub_id})
                MERGE (p)-[:PUBLISHES]->(j)
            """, journal_data)
 
            # --- 3. Sync Volumes ---
            print("  -> Syncing Volumes...")
            pg_cursor.execute(f'SELECT volume_id, journal_id, volume_number, publication_year FROM "Volume" WHERE is_deleted = false{limit_clause}')
            vol_data = [{"id": r[0], "journal_id": r[1], "vol_num": r[2], "year": r[3]} for r in pg_cursor.fetchall()]
            
            print(f"     [Info] Found {len(vol_data)} volumes to sync.")
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (v:Volume {id: row.id})
                SET v.volume_number = row.vol_num, v.publication_year = row.year
                WITH v, row WHERE row.journal_id IS NOT NULL
                MATCH (j:Journal {id: row.journal_id})
                MERGE (v)-[:BELONGS_TO_JOURNAL]->(j)
            """, vol_data)
 
            # --- 4. Sync Issues ---
            print("  -> Syncing Issues...")
            pg_cursor.execute(f'SELECT issue_id, volume_id, issue_number, publication_year FROM "Issue" WHERE is_deleted = false{limit_clause}')
            issue_data = [{"id": r[0], "volume_id": r[1], "iss_num": r[2], "year": r[3]} for r in pg_cursor.fetchall()]
            
            print(f"     [Info] Found {len(issue_data)} issues to sync.")
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (i:Issue {id: row.id})
                SET i.issue_number = row.iss_num, i.publication_year = row.year
                WITH i, row WHERE row.volume_id IS NOT NULL
                MATCH (v:Volume {id: row.volume_id})
                MERGE (i)-[:BELONGS_TO_VOLUME]->(v)
            """, issue_data)
 
            # --- 5. Sync Authors ---
            print("  -> Syncing Authors...")
            pg_cursor.execute(f'SELECT author_id, display_name, h_index, cited_by_count FROM "Author" WHERE is_deleted = false{limit_clause}')
            author_data = [{"id": r[0], "name": r[1], "h_index": r[2], "cited_by": r[3]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (auth:Author {id: row.id})
                SET auth.name = row.name, auth.h_index = row.h_index, auth.cited_by_count = row.cited_by
            """, author_data)

            # --- 5.1 Sync Institutions ---
            print("  -> Syncing Institutions...")
            pg_cursor.execute(f'SELECT institution_id, display_name, type FROM "Institution"{limit_clause}')
            author_data = [{"id": r[0], "name": r[1], "type": r[2]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (i:Institution {id: row.id})
                SET i.name = row.name, i.type = row.type
            """, author_data)
 
            # --- 6. Sync Articles & Link to Issues ---
            print("  -> Syncing Articles & Issue Connections...")
            pg_cursor.execute(f'SELECT article_id, title, doi, publication_year, issue_id, "references" FROM "Article" WHERE is_deleted = false{limit_clause}')
            article_data = []
            for r in pg_cursor.fetchall():
                raw_refs = r[5]
                refs_list = []
                if isinstance(raw_refs, list):
                    refs_list = [ref.strip().lower() for ref in raw_refs if isinstance(ref, str) and ref.strip()]
                
                article_data.append({
                    "id": r[0],
                    "title": r[1],
                    "doi": r[2].strip().lower() if (r[2] and r[2].strip()) else None,
                    "year": r[3],
                    "issue_id": r[4],
                    "references": refs_list
                })
 
            # Sync Articles (using unique Postgres ID to prevent duplicate DOI constraint failures)
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (a:Article {id: row.id})
                SET a.title = row.title, a.doi = row.doi, a.publication_year = row.year
                WITH a, row WHERE row.issue_id IS NOT NULL
                MATCH (i:Issue {id: row.issue_id})
                MERGE (a)-[:PUBLISHED_IN_ISSUE]->(i)
            """, article_data)
 
            # Link Article to Article via REFERENCES relationship (Only process articles that have references)
            articles_with_refs = [art for art in article_data if art["references"]]
            if articles_with_refs:
                print(f"  -> Connecting Referenced Articles ({len(articles_with_refs)} articles)...")
                execute_batch(session, """
                    UNWIND $batch AS row
                    MATCH (a:Article {id: row.id})
                    WITH a, row
                    UNWIND row.references AS ref_doi
                    MERGE (ref:Article {doi: ref_doi})
                    MERGE (a)-[:REFERENCES]->(ref)
                """, articles_with_refs, batch_size=100)
 
            # Resolve/Merge placeholder nodes into real Article nodes
            print("  -> Merging citation placeholders...")
            session.run("""
                MATCH (p:Article) WHERE p.id IS NULL AND p.doi IS NOT NULL
                MATCH (r:Article) WHERE r.id IS NOT NULL AND r.doi = p.doi
                WITH p, r
                MATCH (src)-[rel:REFERENCES]->(p)
                MERGE (src)-[:REFERENCES]->(r)
                DETACH DELETE p
            """)
 
            # --- 7. Direct Article-Journal Shortcut ---
            print("  -> Mapping Direct Article-Journal Shortcuts...")
            pg_cursor.execute(f'''
                SELECT a.article_id, v.journal_id 
                FROM "Article" a 
                JOIN "Issue" i ON a.issue_id = i.issue_id 
                JOIN "Volume" v ON i.volume_id = v.volume_id
                WHERE a.is_deleted = false AND i.is_deleted = false AND v.is_deleted = false
                {limit_clause}
            ''')
            art_j_data = [{"article_id": r[0], "journal_id": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (a:Article {id: row.article_id})
                MATCH (j:Journal {id: row.journal_id})
                MERGE (a)-[:PUBLISHED_IN]->(j)
            """, art_j_data)
 
            # --- 8. Sync Author-Article Relationships ---
            print("  -> Syncing Author-Article relationships...")
            pg_cursor.execute(f'SELECT author_id, article_id FROM "Author_Article"{limit_clause}')
            aa_data = [{"author_id": r[0], "article_id": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (auth:Author {id: row.author_id})
                MATCH (art:Article {id: row.article_id})
                MERGE (auth)-[:WRITES]->(art)
            """, aa_data)

            # --- 8.1 Sync Author-Institution Relationships ---
            print("  -> Syncing Author-Institution relationships...")
            pg_cursor.execute(f'SELECT author_id, institution_id, year FROM "Institution_Author"{limit_clause}')
            auth_inst_data = [{"author_id": r[0], "institution_id": r[1], "year": r[2]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (auth:Author {id: row.author_id})
                MATCH (inst:Institution {id: row.institution_id})
                MERGE (auth)-[r:AFFILIATED_WITH {year: row.year}]->(inst)
            """, auth_inst_data)
 
            # --- 9. Sync Topics ---
            print("  -> Syncing Topics...")
            pg_cursor.execute(f'SELECT topic_id, display_name FROM "Topic" WHERE is_deleted = false{limit_clause}')
            topic_data = [{"id": r[0], "name": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (t:Topic {id: row.id}) SET t.name = row.name
            """, topic_data)
 
            pg_cursor.execute(f'SELECT article_id, topic_id FROM "Sub_Topic"{limit_clause}')
            at_data = [{"article_id": r[0], "topic_id": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (a:Article {id: row.article_id})
                MATCH (t:Topic {id: row.topic_id})
                MERGE (a)-[:HAS_TOPIC]->(t)
            """, at_data)
 
            # --- 10. Sync Keywords ---
            print("  -> Syncing Keywords...")
            pg_cursor.execute(f'SELECT keyword_id, display_name FROM "Keyword"{limit_clause}')
            kw_data = [{"id": r[0], "name": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MERGE (k:Keyword {id: row.id}) SET k.name = row.name
            """, kw_data)
 
            pg_cursor.execute(f'SELECT article_id, keyword_id FROM "Keyword_Article"{limit_clause}')
            ak_data = [{"article_id": r[0], "keyword_id": r[1]} for r in pg_cursor.fetchall()]
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (a:Article {id: row.article_id})
                MATCH (k:Keyword {id: row.keyword_id})
                MERGE (a)-[:HAS_KEYWORD]->(k)
            """, ak_data)
                
            # --- 11. Các mạng lưới tự sinh (Derived) ---
            print("  -> Building Analytics Networks (Collaboration, Keywords, Topics)...")
            
            # Fetch all article IDs to batch over them
            art_res = session.run("MATCH (a:Article) RETURN a.id AS id")
            article_ids = [{"id": r["id"]} for r in art_res]

            print("     [Info] Processing Author Collaborations...")
            session.run("MATCH ()-[r:COLLABORATES_WITH]->() DELETE r")
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (art:Article {id: row.id})
                MATCH (a1:Author)-[:WRITES]->(art)<-[:WRITES]-(a2:Author)
                WHERE a1.id < a2.id
                MERGE (a1)-[c:COLLABORATES_WITH]-(a2)
                ON CREATE SET c.paper_count = 1
                ON MATCH SET c.paper_count = coalesce(c.paper_count, 0) + 1
            """, article_ids, batch_size=250)

            print("     [Info] Processing Keyword Relations...")
            session.run("MATCH (k:Keyword)-[r:RELATED_TO]->() DELETE r")
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (art:Article {id: row.id})
                MATCH (k1:Keyword)<-[:HAS_KEYWORD]-(art)-[:HAS_KEYWORD]->(k2:Keyword)
                WHERE k1.id < k2.id
                MERGE (k1)-[r:RELATED_TO]-(k2)
                ON CREATE SET r.frequency = 1
                ON MATCH SET r.frequency = coalesce(r.frequency, 0) + 1
            """, article_ids, batch_size=250)

            print("     [Info] Processing Topic Relations...")
            session.run("MATCH (t:Topic)-[r:RELATED_TO]->() DELETE r")
            execute_batch(session, """
                UNWIND $batch AS row
                MATCH (art:Article {id: row.id})
                MATCH (t1:Topic)<-[:HAS_TOPIC]-(art)-[:HAS_TOPIC]->(t2:Topic)
                WHERE t1.id < t2.id
                MERGE (t1)-[r:RELATED_TO]-(t2)
                ON CREATE SET r.frequency = 1
                ON MATCH SET r.frequency = coalesce(r.frequency, 0) + 1
            """, article_ids, batch_size=250)

        success = True
 
    except Exception as e:
        print(f"❌ Error during sync: {e}")
        import traceback
        traceback.print_exc() # In chi tiết lỗi để debug dễ dàng hơn
    finally:
        if 'pg_cursor' in locals() and pg_cursor: pg_cursor.close()
        if 'pg_conn' in locals() and pg_conn: pg_conn.close()
        if 'neo4j_driver' in locals() and neo4j_driver: neo4j_driver.close()
    
    if success:
        print(f"✅ Sync process '{sync_type}' completed.")
    else:
        print(f"❌ Sync process '{sync_type}' failed.")
    return success

def main():
    parser = argparse.ArgumentParser(description="Synchronize research data from PostgreSQL to Neo4j")
    parser.add_argument("--type", choices=["full", "incremental"], default="full", help="Sync type")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of records synchronized per entity (omit or 0 for all)")
    parser.add_argument("--all", dest="all_records", action="store_true", help="Sync all records without an interactive prompt")
    args = parser.parse_args()

    acquire("pg_to_neo4j")
    
    limit = args.limit
    limit_was_explicit = args.limit is not None or args.all_records
    if args.all_records or limit == 0:
        limit = None
        
    # If not running in loop mode, and limit is not set via CLI, ask the user interactively
    if not args.loop and limit is None and not limit_was_explicit:
        try:
            if sys.stdin.isatty():
                print("\n📊 Research Graph Sync - Select Mode")
                print("1. Sync all records (default)")
                print("2. Sync a limited number of records")
                choice = input("Enter choice (1 or 2): ").strip()
                if choice == "2":
                    qty_str = input("Enter quantity to sync (number of records per entity): ").strip()
                    if qty_str.isdigit():
                        limit = int(qty_str)
                        print(f"✔️ Selected limit: {limit} records per entity.")
                    else:
                        print("⚠️ Invalid number. Syncing all records.")
                else:
                    print("✔️ Syncing all records.")
        except Exception:
            # Fallback if standard input is closed or there is any terminal reading issue
            pass
            
    if args.loop:
        print("🔄 Running in continuous mode...")
        import time
        try:
            while True:
                if not run_sync(args.type, limit=limit):
                    return 1
                time.sleep(86400)
        except KeyboardInterrupt:
            print("🛑 Stopping loop...")
            return 0
    else:
        return 0 if run_sync(args.type, limit=limit) else 1

if __name__ == "__main__":
    sys.exit(main())

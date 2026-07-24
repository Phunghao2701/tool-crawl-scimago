import psycopg2
from psycopg2.extras import RealDictCursor
from meilisearch import Client
from dotenv import load_dotenv
from pathlib import Path
import os
import sys
import time
import gc


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=False)

REPO_ROOT = BASE_DIR.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required. Set it in {ENV_PATH}.")
    return value


def positive_int_env(name, default):
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


# ==========================================
# CẤU HÌNH KẾT NỐI HỆ THỐNG
# ==========================================
PG_CONFIG = {
    "dbname": required_env("PG_DATABASE"),
    "user": required_env("PG_USER"),
    "password": required_env("PG_PASSWORD"),
    "host": required_env("PG_HOST"),
    "port": positive_int_env("PG_PORT", 5432),
}

MEILI_CONFIG = {
    "host": required_env("MEILI_HOST").rstrip("/"),
    "api_key": required_env("MEILI_API_KEY"),
    "timeout": positive_int_env("MEILI_TIMEOUT", 30),
}

# THAM SỐ TỐI ƯU HÓA HIỆU NĂNG HIGH-SPEED
BATCH_SIZE = positive_int_env("MEILI_BATCH_SIZE", 500)
MAX_CONCURRENT_TASKS = positive_int_env("MEILI_MAX_CONCURRENT_TASKS", 3)

C_RESET  = "\033[0m"
C_BLUE   = "\033[36m"
C_GREEN  = "\033[32m"
C_YELLOW = "\033[33m"
C_RED    = "\033[31m"
C_WHITE  = "\033[37m"

meili_client = Client(
    MEILI_CONFIG["host"],
    MEILI_CONFIG["api_key"],
    timeout=MEILI_CONFIG["timeout"],
)

def clear_stuck_tasks():
    """Tự động kill sạch các task bị kẹt từ lượt chạy trước mà không làm mất dữ liệu đã index"""
    print(f"{C_YELLOW}⏳ Đang quét và dọn dẹp hàng đợi cũ trên Meilisearch để tránh bị nghẽn...{C_RESET}", flush=True)
    try:
        cancel_res = meili_client.cancel_tasks({'statuses': ['enqueued', 'processing']})
        task_uid = cancel_res.task_uid if hasattr(cancel_res, 'task_uid') else cancel_res.get('task_uid')
        
        while True:
            status_res = meili_client.get_task(task_uid)
            status = status_res.status if hasattr(status_res, 'status') else status_res.get('status')
            
            if status in ['succeeded', 'failed']:
                print(f"{C_GREEN}✅ Đã dọn sạch hàng đợi! Hệ thống sẵn sàng làm việc.{C_RESET}\n", flush=True)
                break
            time.sleep(1)
    except Exception as e:
        print(f"{C_YELLOW}⚠️ Thông báo: Hàng đợi hiện tại đã sạch hoặc không thể hủy (Bỏ qua để chạy tiếp): {e}{C_RESET}\n", flush=True)

def get_pg_connection():
    try:
        conn = psycopg2.connect(**PG_CONFIG, cursor_factory=RealDictCursor)
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"{C_RED}❌ Không thể kết nối PostgreSQL: {e}{C_RESET}", flush=True)
        sys.exit(1)

def ensure_connection_alive(conn):
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1;")
        cursor.close()
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        print(f"\n{C_YELLOW}⚠️ Phát hiện kết nối PostgreSQL bị ngắt ngầm từ server. Đang thiết lập lại...{C_RESET}", flush=True)
        try:
            conn.close()
        except Exception:
            pass
        return get_pg_connection()

def safely_control_queue():
    while True:
        try:
            tasks = meili_client.get_tasks({'statuses': ['enqueued', 'processing'], 'limit': 10})
            current_queue_size = len(tasks.results) if hasattr(tasks, 'results') else (len(tasks['results']) if isinstance(tasks, dict) else len(tasks))
            
            if current_queue_size < MAX_CONCURRENT_TASKS:
                break
            else:
                print(f"   {C_YELLOW}⏳ Hàng đợi đầy ({current_queue_size}/{MAX_CONCURRENT_TASKS}). Đang đợi tiêu thụ...{C_RESET}", end="\r", flush=True)
                time.sleep(1.5)
        except Exception as e:
            print(f"\n{C_YELLOW}⚠️ Cảnh báo: Lỗi hệ thống khi kiểm tra hàng đợi (Đang thử lại sau 3s)... Chi tiết: {e}{C_RESET}", flush=True)
            time.sleep(3)

def render_progress_bar(entity_name, current_id, max_id, total_synced, elapsed_time):
    bar_length = 30
    progress_ratio = min(1.0, max(0.0, current_id / max_id if max_id > 0 else 1.0))
    completed_len = int(progress_ratio * bar_length)
    remaining_len = bar_length - completed_len
    
    bar = f"{C_GREEN}{'█' * completed_len}{C_RESET}{C_WHITE}{'░' * remaining_len}{C_RESET}"
    percentage = int(progress_ratio * 100)
    speed = total_synced / elapsed_time if elapsed_time > 0 else 0
    
    sys.stdout.write(
        f"\r{C_BLUE}[{entity_name.upper()}]{C_RESET} {bar} {C_YELLOW}{percentage:3d}%{C_RESET} "
        f"| Đã đồng bộ: {C_WHITE}{total_synced:,}{C_RESET} | Tốc độ: {C_GREEN}{speed:.0f} d/s{C_RESET} | ⏱️ {elapsed_time:.1f}s"
    )
    sys.stdout.flush()

def get_or_create_index(index_name, settings):
    try:
        index = meili_client.index(index_name)
        index.get_stats()
        print(f"🔄 Phát hiện Index cũ [{index_name}]. Tiến hành đồng bộ nối tiếp (Upsert)...", flush=True)
    except Exception:
        print(f"🆕 Index [{index_name}] chưa tồn tại. Tiến hành khởi tạo mới...", flush=True)
        try:
            meili_client.create_index(uid=index_name, options={'primaryKey': 'id'})
            time.sleep(1)
            index = meili_client.index(index_name)
        except Exception as e:
            print(f"{C_RED}❌ Lỗi nghiêm trọng khi tạo Index: {e}{C_RESET}", flush=True)
            sys.exit(1)
        
    try:
        index.update_settings(settings)
        print(f"   ↳ Áp dụng bộ quy tắc Settings thành công!", flush=True)
    except Exception as e:
        print(f"{C_YELLOW}⚠️ Không thể cập nhật Settings (Bỏ qua): {e}{C_RESET}", flush=True)
        
    return index

def get_start_id_from_meili(index):
    try:
        result = index.search("", {"limit": 1, "sort": ["id:desc"]})
        if result and result.get('hits'):
            last_id = int(result['hits'][0]['id'])
            print(f"📍 Tìm thấy ID lớn nhất trên Meilisearch: {last_id}. Tiếp tục chạy từ ID: {last_id + 1}", flush=True)
            return last_id + 1
    except Exception:
        print(f"⚠️ Index trống hoặc chưa cấu hình Sortable. Bắt đầu chạy từ ID đầu tiên: 1", flush=True)
    return 1

def sync_large_table_optimized(conn, entity_name, table_name, index_name, select_fields_sql, id_column, is_deleted_col, settings, sync_mode, run_limit):
    print(f"\n{C_BLUE}⏳ KHỞI ĐỘNG PHÂN HỆ: [{entity_name.upper()}]{C_RESET}", flush=True)
    print("=" * 95, flush=True)
    
    index = get_or_create_index(index_name, settings)
    
    current_sortable = settings.get('sortableAttributes', [])
    if 'id' not in current_sortable:
        current_sortable.append('id')
        index.update_settings({'sortableAttributes': current_sortable})
        time.sleep(1)

    current_id = get_start_id_from_meili(index)
    
    conn = ensure_connection_alive(conn)
    raw_id_column = id_column.split('.')[-1]
    
    cursor = conn.cursor()
    cursor.execute(f'SELECT MAX("{raw_id_column}") as max_id FROM "{table_name}";')
    bounds = cursor.fetchone()
    max_id = bounds['max_id'] if (bounds and bounds['max_id'] is not None) else 1
    cursor.close()

    total_synced = 0
    start_time = time.time()

    print(f"▶️ Bắt đầu quét dữ liệu bằng chiến lược Keyset Pagination... (ID trần: {max_id})", flush=True)
    print("-" * 95, flush=True)

    is_first_batch = True

    while current_id <= max_id:
        if sync_mode == "limit" and total_synced >= run_limit:
            break

        safely_control_queue()
        
        current_batch_size = BATCH_SIZE
        if sync_mode == "limit" and (total_synced + BATCH_SIZE) > run_limit:
            current_batch_size = run_limit - total_synced

        conn = ensure_connection_alive(conn)
        
        op = ">=" if is_first_batch else ">"
        
        if is_deleted_col.strip().lower() == "true":
            where_clause = f"WHERE {id_column} {op} {current_id}"
        else:
            where_clause = f"WHERE {id_column} {op} {current_id} AND ({is_deleted_col} = false OR {is_deleted_col} IS NULL)"

        cursor = conn.cursor()
        batch_query = f"""
            {select_fields_sql}
            {where_clause}
            ORDER BY {id_column} ASC
            LIMIT {current_batch_size};
        """
        cursor.execute(batch_query)
        records = cursor.fetchall()
        cursor.close()
        
        # GIẢI PHÁP NHẢY VỌT VÙNG ID TRỐNG ĐỂ TRÁNH NGHẼN
        if not records:
            conn = ensure_connection_alive(conn)
            cursor = conn.cursor()
            cursor.execute(f'SELECT MIN("{raw_id_column}") as next_id FROM "{table_name}" WHERE "{raw_id_column}" > %s;', (current_id,))
            next_row = cursor.fetchone()
            cursor.close()
            
            if next_row and next_row['next_id'] is not None:
                current_id = int(next_row['next_id'])
                is_first_batch = True
                elapsed = time.time() - start_time
                render_progress_bar(entity_name, min(current_id, max_id), max_id, total_synced, elapsed)
                continue
            else:
                break
            
        retries = 5
        while retries > 0:
            try:
                index.add_documents(records)
                break
            except Exception as e:
                retries -= 1
                print(f"\n{C_YELLOW}⚠️ Lỗi đẩy dữ liệu lên Meilisearch. Đang thử lại sau 3s... Lỗi: {e}{C_RESET}", flush=True)
                time.sleep(3)
                if retries == 0:
                    print(f"\n{C_RED}❌ Lỗi kết nối liên tục từ phía Meilisearch. Dừng hệ thống.{C_RESET}", flush=True)
                    sys.exit(1)
        
        batch_len = len(records)
        total_synced += batch_len
        elapsed = time.time() - start_time
        
        last_record_id = int(records[-1]['id'])
        current_id = last_record_id
        is_first_batch = False
        
        render_progress_bar(entity_name, min(current_id, max_id), max_id, total_synced, elapsed)

    elapsed = time.time() - start_time
    render_progress_bar(entity_name, max_id, max_id, total_synced, elapsed)
    print(f"\n\n{C_GREEN}✅ HOÀN THÀNH PHÂN HỆ [{entity_name.upper()}]. Đã xử lý tổng cộng: {total_synced:,} dòng.{C_RESET}")
    print("=" * 95, flush=True)
    
    gc.collect()
    return conn

def main():
    acquire("pg_to_meilisearch")
    exit_code = 0
    print(f"{C_GREEN}🚀 HỆ THỐNG ĐỒNG BỘ STREAMING PIPELINE V3.0 - SMART CLEAN & HIGH PERFORMANCE{C_RESET}\n" + "="*95, flush=True)
    
    # Kích hoạt tính năng tự dọn hàng đợi kẹt ngay khi khởi động
    clear_stuck_tasks()
    
    print("Chọn chế độ đồng bộ:")
    print("   1. Đồng bộ TOÀN BỘ dữ liệu còn lại (ALL)")
    print("   2. Đồng bộ GIỚI HẠN số lượng dòng (LIMIT)")
    choice = input("Nhập lựa chọn của bạn (1 hoặc 2): ").strip()
    
    sync_mode = "all"
    run_limit = 0
    
    if choice == "2":
        sync_mode = "limit"
        try:
            run_limit = int(input("Nhập số lượng dòng tối đa muốn đồng bộ cho MỖI bảng: ").strip())
            if run_limit <= 0:
                print(f"{C_RED}❌ Số lượng phải lớn hơn 0. Tự động quy về chế độ ALL.{C_RESET}")
                sync_mode = "all"
        except ValueError:
            print(f"{C_RED}❌ Định dạng số không hợp lệ. Tự động quy về chế độ ALL.{C_RESET}")
            sync_mode = "all"

    conn = get_pg_connection()
    
    sync_registry = [
        {
            "entity_name": "articles",
            "table_name": "Article",
            "index_name": "articles",
            "id_column": "a.article_id",
            "is_deleted_col": "a.is_deleted",
            "select_fields_sql": """
                SELECT a.article_id::text as id, a.article_id::text as entity_id, 'article' as entity_type,
                       a.title, COALESCE(a.abstract, '') as abstract, COALESCE(a.semantic_tldr, '') as semantic_tldr,
                       COALESCE(a.doi, '') as doi, COALESCE(a.publication_year, 0) as publication_year,
                       COALESCE(a.citation_count, 0) as citation_count, COALESCE(a.semantic_influential_citation_count, 0) as influential_citation_count
                FROM "Article" a
            """,
            "settings": {
                'searchableAttributes': ['title', 'abstract', 'semantic_tldr', 'doi'],
                'filterableAttributes': ['publication_year', 'citation_count', 'influential_citation_count'],
                'sortableAttributes': ['id'],
                'rankingRules': ['words', 'typo', 'attribute', 'sort', 'exactness']
            }
        },
        {
            "entity_name": "authors",
            "table_name": "Author",
            "index_name": "authors",
            "id_column": "au.author_id",
            "is_deleted_col": "au.is_deleted",
            "select_fields_sql": """
                SELECT au.author_id::text as id, au.author_id::text as entity_id, 'author' as entity_type,
                       au.display_name, COALESCE(au.orcid, '') as orcid, COALESCE(au.openalex_id, '') as openalex_id,
                       au.works_count, au.cited_by_count, au.h_index, au.i10_index
                FROM "Author" au
            """,
            "settings": {
                'searchableAttributes': ['display_name', 'orcid', 'openalex_id'],
                'filterableAttributes': ['h_index', 'i10_index', 'works_count', 'cited_by_count'],
                'sortableAttributes': ['id'],
                'rankingRules': ['words', 'typo', 'attribute', 'sort', 'exactness']
            }
        },
        {
            "entity_name": "topics",
            "table_name": "Topic",
            "index_name": "topics",
            "id_column": "t.topic_id",
            "is_deleted_col": "t.is_deleted",
            "select_fields_sql": """
                SELECT t.topic_id::text as id, t.topic_id::text as entity_id, 'topic' as entity_type,
                       t.display_name
                FROM "Topic" t
            """,
            "settings": {
                'searchableAttributes': ['display_name'],
                'filterableAttributes': [],
                'sortableAttributes': ['id']
            }
        },
        {
            "entity_name": "institutions",
            "table_name": "Institution",
            "index_name": "institutions",
            "id_column": "i.institution_id",
            "is_deleted_col": "i.is_deleted",
            "select_fields_sql": """
                SELECT i.institution_id::text as id, i.institution_id::text as entity_id, 'institution' as entity_type,
                       i.display_name, COALESCE(i.country_code, '') as country_code, COALESCE(i.type, '') as type
                FROM "Institution" i
            """,
            "settings": {
                'searchableAttributes': ['display_name', 'country_code', 'type'],
                'filterableAttributes': ['country_code', 'type'],
                'sortableAttributes': ['id']
            }
        },
        {
            "entity_name": "journals",
            "table_name": "Journal",
            "index_name": "journals",
            "id_column": "j.journal_id",
            "is_deleted_col": "j.is_deleted",
            "select_fields_sql": """
                SELECT j.journal_id::text as id, j.journal_id::text as entity_id, 'journal' as entity_type,
                       j.display_name, COALESCE(j.issn, '') as issn, COALESCE(j.publisher_id::text, '') as publisher_id
                FROM "Journal" j
            """,
            "settings": {
                'searchableAttributes': ['display_name', 'issn', 'publisher_id'],
                'filterableAttributes': ['publisher_id'],
                'sortableAttributes': ['id']
            }
        },
        {
            "entity_name": "keywords",
            "table_name": "Keyword",
            "index_name": "keywords",
            "id_column": "k.keyword_id",
            "is_deleted_col": "true",
            "select_fields_sql": """
                SELECT k.keyword_id::text as id, k.keyword_id::text as entity_id, 'keyword' as entity_type,
                       k.display_name
                FROM "Keyword" k
            """,
            "settings": {
                'searchableAttributes': ['display_name'],
                'filterableAttributes': [],
                'sortableAttributes': ['id']
            }
        }
    ]

    try:
        for target in sync_registry:
            conn = sync_large_table_optimized(
                conn=conn,
                entity_name=target["entity_name"],
                table_name=target["table_name"],
                index_name=target["index_name"],
                select_fields_sql=target["select_fields_sql"],
                id_column=target["id_column"],
                is_deleted_col=target["is_deleted_col"],
                settings=target["settings"],
                sync_mode=sync_mode,
                run_limit=run_limit
            )

    except KeyboardInterrupt:
        exit_code = 130
        print(f"\n{C_RED}🛑 Tiến trình bị dừng đột ngột bởi người dùng (Ctrl+C).{C_RESET}", flush=True)
    except Exception as e:
        exit_code = 1
        print(f"\n{C_RED}❌ Lỗi hệ thống phát sinh: {e}{C_RESET}", flush=True)
    finally:
        try:
            conn.close()
            print(f"\n🔌 Kết nối Đã đóng an toàn. Hoàn thành toàn bộ tiến trình.", flush=True)
        except Exception:
            pass
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())

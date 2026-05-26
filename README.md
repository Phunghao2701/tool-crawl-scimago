# Scimago & OpenAlex ETL Pipeline

Hệ thống ETL (Extract, Transform, Load) nội bộ được viết bằng Python giúp thu thập, staging và chuẩn hóa dữ liệu xếp hạng tạp chí khoa học từ **Scimago** phối hợp làm phong phú thông tin chi tiết qua **OpenAlex API** vào cơ sở dữ liệu PostgreSQL.

---

## 📈 Quy trình xử lý (ETL Pipeline)

```
[Scimago CSV/XLS Export]
        │ (Tải thủ công từ trình duyệt)
        ▼
[Staging Table: raw_scimago_journal] (Lưu trữ JSON thô để đối chiếu/debug)
        │ (Chuẩn hóa & Deduplication)
        ▼
[Bảng cơ sở dữ liệu chuẩn hóa] (Publisher, Journal, ISSN, Subject Category...)
        │
        ▼ (OpenAlex Sync bằng ISSN qua Polite Pool)
[Làm giàu dữ liệu Tạp chí] (Tổng số bài báo, Lượt trích dẫn, Link OpenAlex, Homepage URL)
```

---

## 🛠️ Yêu cầu hệ thống

*   Python 3.11+
*   Docker & Docker Compose
*   Thư viện Python: `pandas`, `sqlalchemy`, `psycopg2-binary`, `requests`, `python-dotenv`

---

## 🚀 Hướng dẫn cài đặt & Khởi động nhanh (Quick Start)

### 1. Chuẩn bị môi trường Python
Cài đặt các thư viện Python cần thiết:
```bash
pip install -r requirements.txt
```

### 2. Thiết lập cơ sở dữ liệu (PostgreSQL qua Docker)
Do cổng mặc định `5432` trên máy của bạn có thể đã bị chiếm dụng bởi PostgreSQL cài sẵn cục bộ, Docker container này được cấu hình chạy trên cổng **`5433`**.

Chạy **1 dòng lệnh duy nhất** trên PowerShell để khởi động và thiết lập toàn bộ database:
```powershell
docker compose up -d --build; Start-Sleep 3; Get-Content "database/schema.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db; Get-Content "database/seed_ranking_metric.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db
```

*Nếu chạy trên hệ điều hành Linux/macOS (Bash):*
```bash
docker compose up -d --build && sleep 3 && docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db < database/schema.sql && docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db < database/seed_ranking_metric.sql
```

---

## 📊 Hướng dẫn sử dụng các công cụ ETL

### Bước 1: Import dữ liệu Scimago
Do Scimago chặn bot tự động tải file rất mạnh, bạn cần tải file dữ liệu thủ công qua trình duyệt:

1.  Truy cập: [scimagojr.com/journalrank.php](https://www.scimagojr.com/journalrank.php).
2.  Nhấn nút **Download data** (tải file dạng `.xls` thực chất là CSV ngăn cách bởi dấu `;`).
3.  Lưu file vào thư mục `data/` trong dự án.
4.  Chạy lệnh import:

> ⚠️ **Chú ý:** Nếu tên file của bạn chứa khoảng trắng (ví dụ: `scimagojr 2025.csv`), bạn **bắt buộc** phải bọc đường dẫn file trong dấu nháy kép `""`.

*   **Chạy thử nghiệm (Giới hạn 100 dòng đầu):**
    ```bash
    python tools/scimago_etl.py import --file "data/scimagojr 2025.csv" --year 2025 --limit 100
    ```
*   **Chạy import toàn bộ file:**
    ```bash
    python tools/scimago_etl.py import --file "data/scimagojr 2025.csv" --year 2025
    ```
*   **Kiểm tra số liệu thống kê trong cơ sở dữ liệu:**
    ```bash
    python tools/scimago_etl.py stats
    ```

---

### Bước 2: Đồng bộ hóa metadata chi tiết từ OpenAlex
Bổ sung các trường thông tin: website tạp chí, tổng số bài báo công bố (`works_count`), tổng số lượt trích dẫn (`cited_by_count`).

#### 1. Cấu hình Email
OpenAlex yêu cầu gửi kèm email liên hệ của bạn trong Header (`User-Agent`) để đưa vào **Polite Pool** giúp phản hồi nhanh và an toàn hơn.
Hãy cấu hình email của bạn trong file `.env`:
```env
OPENALEX_EMAIL=your-email@example.com
```

#### 2. Kích hoạt đồng bộ
*   **Đồng bộ thử nghiệm 10 tạp chí:**
    ```bash
    python tools/openalex_sync.py sync --limit 10
    ```
*   **Đồng bộ toàn bộ cơ sở dữ liệu:**
    ```bash
    python tools/openalex_sync.py sync
    ```
*   **Xem thống kê đồng bộ:**
    ```bash
    python tools/openalex_sync.py stats
    ```

---

### Bước 3: Xuất dữ liệu báo cáo chi tiết (Enriched Data)
Xuất bảng kết hợp đầy đủ thông tin từ Scimago (Rank, SJR Score, Quartile) và OpenAlex (Works, Citations, Homepage URL, OpenAlex ID) ra file báo cáo:

*   **Xem nhanh Top 20 tạp chí có chỉ số SJR cao nhất trên terminal:**
    ```bash
    python tools/openalex_sync.py export --limit 20
    ```
*   **Xuất toàn bộ dữ liệu ra file CSV:**
    ```bash
    python tools/openalex_sync.py export
    ```
    *(Mặc định file kết quả lưu tại `data/enriched_journals.csv`. Có thể tùy biến qua tham số `--output <đường_dẫn>`)*

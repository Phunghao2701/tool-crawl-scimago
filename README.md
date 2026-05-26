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
[Bảng cơ sở dữ liệu chuẩn hóa] (Publisher, Journal với cột ISSN trực tiếp, Subject Category...)
        │
        ▼ (OpenAlex Sync bằng ISSN qua Polite Pool)
[Làm giàu dữ liệu Tạp chí] (Tổng số bài báo, Lượt trích dẫn, Link OpenAlex, Homepage URL)
```

---

## 🛠️ Yêu cầu hệ thống

*   Python 3.11+
*   Docker & Docker Compose
*   Thư viện Python: `pandas`, `sqlalchemy`, `psycopg2-binary`, `requests`, `python-dotenv`, `openpyxl` (để xuất Excel)

---

## 🚀 Hướng dẫn cài đặt & Khởi động nhanh (Quick Start)

### 1. Chuẩn bị môi trường Python
Cài đặt các thư viện Python cần thiết:
```bash
pip install -r requirements.txt
# Hoặc cài đặt thêm openpyxl để hỗ trợ xuất file Excel
pip install openpyxl
```

### 2. Cấu hình biến môi trường (`.env`)
Tạo hoặc cập nhật file `.env` tại thư mục gốc của dự án:
```env
DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db
OPENALEX_EMAIL=your-email@example.com
```
*(Lưu ý: Mật khẩu mặc định của Docker Compose là `1234`, cổng kết nối là `5433` để tránh xung đột với cổng `5432` cục bộ).*

### 3. Thiết lập cơ sở dữ liệu (PostgreSQL qua Docker)
Chạy **1 dòng lệnh duy nhất** trên PowerShell (đường dẫn tuyệt đối) để khởi động Docker và tạo cấu trúc bảng:
```powershell
docker compose up -d --build; Start-Sleep 3; Get-Content "E:\tool-crawl-scimago\database\schema.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db; Get-Content "E:\tool-crawl-scimago\database\seed_ranking_metric.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db
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

> ⚠️ **Chú ý:** Nếu tên file của bạn chứa khoảng trắng (ví dụ: `scimagojr 2025.csv`), bạn **bắt buộc** phải bọc đường dẫn file trong dấu nháy kép `""`. Bạn nên cung cấp đường dẫn tuyệt đối nếu terminal đang đứng ở thư mục khác.

*   **Chạy thử nghiệm (Giới hạn 100 dòng đầu):**
    ```bash
    python tools/scimago_etl.py import --file "E:\tool-crawl-scimago\data\scimagojr 2025.csv" --year 2025 --limit 100
    ```
*   **Chạy import toàn bộ file:**
    ```bash
    python tools/scimago_etl.py import --file "E:\tool-crawl-scimago\data\scimagojr 2025.csv" --year 2025
    ```
*   **Kiểm tra số liệu thống kê trong cơ sở dữ liệu:**
    ```bash
    python tools/scimago_etl.py stats
    ```

---

### Bước 2: Đồng bộ hóa metadata chi tiết từ OpenAlex
Bổ sung các trường thông tin: website tạp chí, tổng số bài báo công bố (`works_count`), tổng số lượt trích dẫn (`cited_by_count`).

*   **Đồng bộ thử nghiệm 20 tạp chí:**
    ```bash
    python tools/openalex_sync.py sync --limit 20
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
Xuất bảng kết hợp đầy đủ thông tin từ Scimago (Rank, SJR Score, Quartile) và OpenAlex (Works, Citations, Homepage URL, OpenAlex ID, ISSN) ra file báo cáo:

*   **Xem nhanh Top 20 tạp chí có chỉ số SJR cao nhất trên terminal:**
    ```bash
    python tools/openalex_sync.py export --limit 20
    ```
*   **Xuất toàn bộ dữ liệu ra file báo cáo:**
    ```bash
    python tools/openalex_sync.py export
    ```
    *   Lệnh này sẽ tự động xuất đồng thời **2 định dạng file báo cáo** cùng chứa cột **`issn`**:
        1.  **Excel:** `data/enriched_journals.xlsx` (Tiện lợi để xem và làm báo cáo)
        2.  **CSV:** `data/enriched_journals.csv` (Định dạng UTF-8-BOM tương thích tốt với Excel Việt hóa)

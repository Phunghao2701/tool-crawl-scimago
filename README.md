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

## ⚡ Hướng dẫn chạy nhanh bằng File `.bat` (Khuyên dùng trên Windows)

Để thuận tiện nhất trên Windows, mình đã tạo sẵn file **`run_pipeline.bat`** tích hợp toàn bộ các bước từ setup môi trường cho đến chạy pipeline. Bạn chỉ cần:

1.  Click đúp chuột vào file **`run_pipeline.bat`** (hoặc chạy lệnh `.\run_pipeline.bat` trong PowerShell/CMD).
2.  Bảng điều khiển tương tác sẽ xuất hiện cho phép bạn bấm số chọn chức năng:
    *   **Phím 1:** Tự động cài đặt thư viện Python, bật Docker Postgres và khởi tạo bảng.
    *   **Phím 2:** Nhập đường dẫn file Scimago thô để import.
    *   **Phím 3:** Đồng bộ dữ liệu với OpenAlex API.
    *   **Phím 4:** Xuất báo cáo (Excel & CSV) ra thư mục `data/`.
    *   **Phím 5:** Xem thống kê các bảng dữ liệu hiện tại.
    *   **Phím 6 (FULL Pipeline):** Tự động chạy tuần tự cả 3 bước: Import -> Sync -> Export.

---

## 🐳 Hướng dẫn chạy tự động hoàn toàn bằng Docker

Cách này phù hợp khi chia sẻ dự án cho người khác. Người dùng **chỉ cần cài đặt Docker** là có thể chạy toàn bộ tiến trình ETL và xem kết quả trực tiếp trên giao diện Web mà không cần cài đặt Python, PostgreSQL, hay chạy bất kỳ câu lệnh nào khác.

### 1. Khởi động hệ thống
Mở terminal tại thư mục dự án và chạy câu lệnh duy nhất:
```bash
docker compose up -d --build
```

### 2. Xem dữ liệu trực quan trên giao diện Web
Sau khi chạy lệnh trên, hệ thống sẽ tự động thực hiện tuần tự các bước sau trong chế độ nền:
1. **Thiết lập database & bảng dữ liệu** (`schema.sql` và `seed_ranking_metric.sql`).
2. **Tự động import** dữ liệu Scimago từ file mẫu `data/scimagojr 2025.csv`.
3. **Tự động đồng bộ** thông tin làm giàu từ OpenAlex (cho 50 tạp chí hàng đầu).

Bạn không cần tạo file Excel báo cáo mà có thể truy cập thẳng vào giao diện Web quản lý Database:
*   **Địa chỉ**: [http://localhost:8080](http://localhost:8080) (Sử dụng công cụ Adminer siêu nhẹ)
*   **Thông tin đăng nhập**:
    *   **Hệ quản trị (System)**: `PostgreSQL`
    *   **Máy chủ (Server)**: `postgres`
    *   **Tên người dùng (Username)**: `postgres`
    *   **Mật khẩu (Password)**: `1234`
    *   **Cơ sở dữ liệu (Database)**: `scientific_journal_db`

### 3. (Tùy chọn) Import dữ liệu năm khác (Ví dụ: Năm 2024)
Nếu bạn muốn nạp dữ liệu của năm khác (ví dụ: 2024):
1.  Copy file dữ liệu đó vào thư mục `data/` (ví dụ: đặt tên là `scimagojr 2024.csv`).
2.  Chạy lệnh chỉ định đường dẫn file và năm tương ứng:
    ```bash
    docker compose exec app python tools/scimago_etl.py import --file "data/scimagojr 2024.csv" --year 2024
    ```
3.  (Tùy chọn) Đồng bộ hóa với OpenAlex cho các tạp chí mới nạp:
    ```bash
    docker compose exec app python tools/openalex_sync.py sync --limit 100
    ```

*   **Xem thống kê số lượng dữ liệu hiện có trong DB**:
    ```bash
    docker compose exec app python tools/scimago_etl.py stats
    ```

---

## 🚀 Hướng dẫn cài đặt & Khởi động thủ công (Manual Setup)

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

> 💡 **Cơ chế Ưu tiên theo Thứ hạng (Rank-based Priority):** 
> Thay vì đồng bộ ngẫu nhiên, hệ thống đã được tối ưu để **tự động ưu tiên đồng bộ các tạp chí có thứ hạng Rank cao nhất (SJR lớn nhất) lên đầu trước**. Điều này giúp bạn chỉ cần chạy sync một lượng nhỏ (ví dụ 50, 100 tạp chí), thông tin OpenAlex đã lập tức xuất hiện đầy đủ ở các hàng đầu tiên của file Excel báo cáo mà không cần đợi sync hết cả 32,000 dòng.

*   **Đồng bộ thử nghiệm 50 tạp chí hàng đầu:**
    ```bash
    python tools/openalex_sync.py sync --limit 50
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

> 🔬 **Bố cục cột khoa học (Columns Layout):**
> Thứ tự các cột trong file xuất ra được tự động sắp xếp theo nhóm logic từ trái sang phải để tối ưu hóa trải nghiệm đọc:
> `Định danh tạp chí` (Rank, Title, Issn, Publisher, Country, Region...) ➔ `Chỉ số Scimago chính` (SJR, Quartile, H index) ➔ `Open Access` ➔ `Thông tin OpenAlex` (ID, Homepage, Works, Citations) ➔ `Chỉ số Scimago chi tiết` (Total Docs, Total Citations...) ➔ `Phân loại ngành` (Areas, Categories).

> ⚠️ **Lưu ý sửa lỗi Permission Denied (Errno 13):**
> Khi bạn đang mở file `enriched_journals.xlsx` hoặc `enriched_journals.csv` bằng Microsoft Excel hoặc ứng dụng khác, hệ điều hành Windows sẽ khóa file lại và chặn quyền ghi đè của Python, dẫn đến lỗi `Permission denied`. 
> **Khắc phục:** Hãy đóng toàn bộ các cửa sổ Excel hoặc CSV liên quan trên máy của bạn trước khi thực hiện lệnh Xuất báo cáo.

---

## 🗄️ Cấu trúc cơ sở dữ liệu mới (Database Schema)

Cơ sở dữ liệu được thiết kế tối ưu bằng PostgreSQL chạy trên Docker:
*   **Khóa chính UUID**: Toàn bộ các bảng chính sử dụng khóa định danh UUID tự sinh (`gen_random_uuid()`) để tăng tính toàn vẹn dữ liệu.
*   **Kiểu dữ liệu ENUM**: Quản lý chuẩn hóa các kiểu phân loại thông qua 7 kiểu dữ liệu enum của Postgres (`role_account`, `status_account`, `auth_provider`, `type_zone`, `source_zone`, `ranking_source`, `ranking_metric_type`).
*   **Chống trùng lặp dữ liệu**: Sử dụng các index duy nhất bán phần (partial indexes) trên bảng xếp hạng `"Journal_Ranking"` giúp hệ thống có thể import đè dữ liệu nhiều lần mà không bao giờ bị trùng lặp bản ghi.


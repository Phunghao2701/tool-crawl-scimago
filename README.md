# Scimago ETL Tool

Công cụ ETL nội bộ được viết bằng Python giúp tải/đọc, staging và chuẩn hóa dữ liệu xếp hạng tạp chí khoa học từ Scimago để import vào cơ sở dữ liệu PostgreSQL.

---

## Quy trình xử lý (Pipeline)

```
Scimago export (.xls thực chất là semicolon-delimited CSV)
                      ↓
Lưu dữ liệu thô vào bảng staging (raw_scimago_journal) để kiểm tra/debug
                      ↓
Chuẩn hóa & ánh xạ dữ liệu (Normalize to main tables)
                      ↓
Publisher / Journal / ISSN / Subject / Ranking Metric / Journal Ranking
```

---

## Yêu cầu hệ thống

*   Python 3.11+
*   Docker & Docker Compose
*   Thư viện Python: `pandas`, `sqlalchemy`, `psycopg2-binary`, `requests`, `python-dotenv`

---

## Hướng dẫn cài đặt & Chạy thử nhanh

### 1. Chuẩn bị môi trường & Khởi động Database

1.  Cài đặt các thư viện Python cần thiết:
    ```bash
    pip install -r requirements.txt
    ```
2.  Khởi động PostgreSQL thông qua Docker:
    ```bash
    docker compose up -d
    ```
    *(Lưu ý: Để tránh xung đột với PostgreSQL có sẵn trên máy của bạn, Docker container được cấu hình để lắng nghe trên port **5433**).*

### 2. Thiết lập Schema Database

1.  Khởi tạo cấu trúc bảng (schema):
    ```powershell
    Get-Content "database/schema.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db
    ```
2.  Seed sẵn các định nghĩa metric:
    ```powershell
    Get-Content "database/seed_ranking_metric.sql" | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db
    ```

### 3. Chạy thử nghiệm Import dữ liệu mẫu

Bạn có thể chạy thử pipeline với file dữ liệu mẫu (13 tạp chí hàng đầu) đã được chuẩn bị sẵn:
```bash
python tools/scimago_etl.py import --file data/scimago_sample.xls --year 2024
```

Sau khi chạy xong, kiểm tra kết quả thống kê dữ liệu đã được import vào các bảng chính:
```bash
python tools/scimago_etl.py stats
```

---

## Hướng dẫn Import dữ liệu thật từ Scimago

Vì Scimago cấu hình chặn bot rất chặt chẽ (gây lỗi 403 Forbidden khi download tự động qua script), bạn nên tải dữ liệu thủ công từ trình duyệt:

1.  Truy cập vào trang xếp hạng của Scimago: [scimagojr.com/journalrank.php](https://www.scimagojr.com/journalrank.php).
2.  Nhấn nút **Download data** để tải file `.xls` (dữ liệu phân tách bằng dấu chấm phẩy `;`).
3.  Lưu file vào thư mục `data/` trong dự án (ví dụ: đặt tên là `scimago_2024.xls`).
4.  Tiến hành import:
    *   **Chạy thử nghiệm (Giới hạn 100 dòng đầu để kiểm tra):**
        ```bash
        python tools/scimago_etl.py import --file data/scimago_2024.xls --year 2024 --limit 100
        ```
    *   **Chạy import toàn bộ file:**
        ```bash
        python tools/scimago_etl.py import --file data/scimago_2024.xls --year 2024
        ```
5.  Kiểm tra số lượng bản ghi và xem thử dữ liệu sau import:
    ```bash
    python tools/scimago_etl.py stats
    ```

---

## Đồng bộ hóa với OpenAlex API (Đồng bộ metadata tạp chí)

Dữ liệu Scimago chỉ chứa các số liệu xếp hạng. Bạn có thể sử dụng công cụ đồng bộ OpenAlex để lấy thêm thông tin chi tiết (trang chủ tạp chí, tổng số bài báo, tổng số lượt trích dẫn) dựa trên mã số ISSN.

### 1. Cấu hình Email
OpenAlex yêu cầu gửi kèm email liên hệ của bạn trong Header (`User-Agent`) để sử dụng Polite Pool giúp tốc độ phản hồi nhanh hơn.
Hãy cấu hình email của bạn trong file `.env`:
```env
OPENALEX_EMAIL=your-email@example.com
```

### 2. Tiến hành Đồng bộ (Sync)
*   **Đồng bộ thử nghiệm (Chỉ đồng bộ 10 tạp chí chưa đồng bộ):**
    ```bash
    python tools/openalex_sync.py sync --limit 10
    ```
*   **Đồng bộ toàn bộ các tạp chí:**
    ```bash
    python tools/openalex_sync.py sync
    ```

### 3. Xem thống kê đồng bộ hóa
Kiểm tra tiến trình đồng bộ và xem thử dữ liệu lấy từ OpenAlex:
```bash
python tools/openalex_sync.py stats
```


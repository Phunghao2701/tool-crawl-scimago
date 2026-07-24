# 🗄️ Hướng dẫn Quản lý Database (Local ↔ Vercel)

Tài liệu này hướng dẫn cách quản lý **hai môi trường database song song**: Local Docker (để phát triển nhanh) và Vercel Prisma (production).

---

## 📐 Tổng quan kiến trúc

```
┌─────────────────────────────────┐        ┌──────────────────────────────────┐
│        LOCAL DOCKER DB          │        │         VERCEL PRISMA DB         │
│  postgresql://localhost:5433    │  ──M►  │  postgresql://db.prisma.io:5432  │
│                                 │        │                                  │
│  • Phát triển & sync nhanh      │        │  • Production / Backend API      │
│  • Dữ liệu đầy đủ 32k+ tạp chí │        │  • Được sync từ Local lên        │
└─────────────────────────────────┘        └──────────────────────────────────┘
         ▲                                            ▲
    .env.local                                   .env.vercel
```

---

## ⚙️ Cấu hình file `.env`

Dự án sử dụng **3 file env**:

| File | Mục đích |
|------|----------|
| `.env` | DB đang **active** (được tất cả script Python đọc) |
| `.env.local` | Thông tin kết nối Local Docker DB |
| `.env.vercel` | Thông tin kết nối Vercel Prisma DB |

**`.env.local`:**
```env
LOCAL_DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db
OPENALEX_EMAIL=your@email.com
OPENALEX_API_KEY=your_key
```

**`.env.vercel`:**
```env
VERCEL_DATABASE_URL=postgresql+psycopg2://<user>:<pass>@db.prisma.io:5432/postgres?sslmode=require
OPENALEX_EMAIL=your@email.com
OPENALEX_API_KEY=your_key
```

### Chuyển đổi DB đang dùng

Dùng **Option 0** trong `run_pipeline.bat` để switch nhanh:
```
0 → Switch DB → 1 (Local) hoặc 2 (Vercel)
```

Hoặc sửa thủ công dòng `DATABASE_URL` trong file `.env`.

---

## 🔄 Workflow phát triển chuẩn

```
1. Dùng LOCAL DB  (Option 0 → chọn 1)
        ↓
2. Chạy pipeline (Import, Sync...)
        ↓
3. Khi muốn đẩy lên Vercel:
   Option M → chọn 1 (INCREMENTAL)
        ↓
4. Kiểm tra trên Vercel
   Option 0 → chọn 2 → Option 7 (Stats)
        ↓
5. Quay về Local để tiếp tục phát triển
   Option 0 → chọn 1
```

---

## 📤 Migrate Local → Vercel (Option M)

Chạy từ menu `run_pipeline.bat` → **Option M**, hoặc:

```bash
# Incremental (khuyên dùng): chỉ copy những gì Vercel chưa có
python tools/migrate_local_to_vercel.py

# Full Reset (nguy hiểm): xóa hết Vercel rồi copy lại từ đầu
python tools/migrate_local_to_vercel.py --reset

# Non-interactive (cho automation / agent, ví dụ Javis): bỏ qua menu chọn phạm vi và câu hỏi xác nhận
python tools/migrate_local_to_vercel.py --profile 3 --yes   # 1=Journals, 2=+Articles, 3=Full
```

### So sánh 2 chế độ

| | Incremental | Full Reset |
|---|---|---|
| **Hành vi** | INSERT row còn thiếu, giữ data Vercel | Xóa hết Vercel, copy toàn bộ |
| **Thời gian** | Nhanh (bỏ qua row đã có) | Lâu hơn |
| **An toàn** | ✅ Không mất data | ⚠️ Mất toàn bộ data Vercel |
| **Dùng khi** | Sync định kỳ | Vercel bị lỗi / muốn reset hoàn toàn |
| **Journal** | UPSERT `works_synced_at` | Copy fresh |

> 💡 **Lưu ý quan trọng:** Sau khi migrate, file `.env` vẫn trỏ về **Local DB**. Script không tự động đổi.

---

## ➕ Thêm bảng mới vào cả 2 DB

> ❌ **Đừng chạy lại Option 1 (Setup)** — sẽ DROP tất cả bảng và mất toàn bộ data!

Khi BE thêm bảng mới, làm theo 4 bước:

### Bước 1 — Viết file SQL migration

Tạo file `scratch/migrate_add_<ten_bang>.sql`:

```sql
CREATE TABLE IF NOT EXISTS "TenBang" (
    "id"         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    "user_id"    uuid NOT NULL,
    "created_at" timestamp NOT NULL DEFAULT now(),
    CONSTRAINT "TenBang_user_id_fkey"
        FOREIGN KEY ("user_id") REFERENCES "user"("user_id") ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS "idx_tenbang_user" ON "TenBang" ("user_id");
```

### Bước 2 — Chạy vào Local Docker DB

```bash
# Copy file vào container và chạy
docker cp scratch/migrate_add_tenbang.sql scientific_journal_postgres:/tmp/mig.sql
docker exec scientific_journal_postgres psql -U postgres -d scientific_journal_db -f /tmp/mig.sql
```

### Bước 3 — Chạy lên Vercel DB

Tạo file `scratch/migrate_add_<ten_bang>.py`:

```python
import os, sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(".env.vercel", override=True)
VERCEL_URL = os.getenv("VERCEL_DATABASE_URL") or os.getenv("DATABASE_URL")
engine = create_engine(VERCEL_URL)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS "TenBang" (
    -- ... cấu trúc giống Bước 1
);
"""

with engine.begin() as conn:
    conn.execute(text(CREATE_SQL))
    print("[OK] Table created on Vercel.")
```

```bash
python scratch/migrate_add_tenbang.py
```

### Bước 4 — Cập nhật `database/schema.sql`

Thêm định nghĩa bảng mới vào `database/schema.sql` để những lần **setup từ đầu** (môi trường mới) cũng có bảng này.

---

## 🔍 Kiểm tra nhanh

### Kiểm tra số bảng & dữ liệu

```bash
# Local DB
python tools/openalex_sync.py stats
python tools/openalex_sync.py stats-works

# Kiểm tra cấu trúc bảng trong Adminer
# Mở http://localhost:8080
```

### Kiểm tra bảng tồn tại trên Vercel

```bash
python scratch/check_vercel_schema.py
```

---

## 🚫 Những điều KHÔNG nên làm

| Hành động | Hậu quả |
|-----------|---------|
| Chạy Option 1 (Setup) khi đã có data | DROP tất cả bảng, mất toàn bộ data |
| Đổi `.env` sang Vercel rồi chạy pipeline | Sync thẳng lên production |
| Chạy migrate `--reset` khi Vercel đang có data quan trọng | Mất toàn bộ data Vercel |
| Commit file `.env` lên Git | Lộ credentials |

---

## 📁 Cấu trúc file liên quan

```
tool-crawl-scimago/
├── .env                          # DB đang active (không commit)
├── .env.local                    # Local Docker config (không commit)
├── .env.vercel                   # Vercel Prisma config (không commit)
├── .env.example                  # Template cấu hình
├── database/
│   └── schema.sql                # Schema đầy đủ (cập nhật khi thêm bảng)
├── tools/
│   └── migrate_local_to_vercel.py  # Script sync Local → Vercel
└── scratch/
    ├── migrate_add_*.sql         # Migration files (thêm bảng mới)
    └── migrate_add_*.py          # Migration scripts cho Vercel
```

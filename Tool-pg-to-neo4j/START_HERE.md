# 🚀 BƯỚC ĐẦU TIÊN - START HERE

## ⚡ Cách Chạy Tool Setup (3 Bước Đơn Giản)

### Bước 1: Cài Đặt Yêu Cầu
Đảm bảo bạn có:
- ✅ Python 3.9+
- ✅ Docker Desktop (download từ https://www.docker.com/)
- ✅ Docker Compose (thường kèm theo Docker Desktop)

**Kiểm tra:**
```bash
python --version
docker --version
docker-compose --version
```

---

### Bước 2: Chạy Tool Setup

#### 🪟 Trên Windows:
**Cách dễ nhất - Double-click file này:**
```
setup.bat
```

Hoặc chạy trong PowerShell/Command Prompt:
```bash
python setup_tool.py
```

#### 🍎 Trên macOS / 🐧 Linux:
```bash
python3 quick_start.py
```

---

### Bước 3: Làm Theo Hướng Dẫn

Tool sẽ tự động:
1. ✅ Tạo project structure hoàn chỉnh
2. ✅ Tạo tất cả config files
3. ✅ Tạo Docker setup
4. ✅ Tạo database schema
5. ✅ Tạo Python modules sẵn chạy

**Mất khoảng 2-3 phút để hoàn thành.**

---

## 🎯 Sau Khi Tool Chạy Xong

### 1️⃣ Vào thư mục project:
```bash
cd research-graph-sync
```

### 2️⃣ (Tuỳ chọn) Cấu hình environment:
- Mở file `.env`
- Thay đổi passwords nếu cần
- Save file

### 3️⃣ Khởi động tất cả services:

**Windows:**
```bash
docker-compose -f docker\docker-compose.yml up -d
```

**macOS / Linux:**
```bash
docker-compose -f docker/docker-compose.yml up -d
```

Chờ 30-60 giây để services khởi động.

### 4️⃣ Kiểm tra xem mọi thứ chạy ok không:

**Kiểm tra services:**
```bash
docker-compose -f docker/docker-compose.yml ps
```

**Kiểm tra API:**
```bash
curl http://localhost:8000/health
```

Hoặc mở browser: `http://localhost:8000/health`

---

## 📍 Truy Cập Các Dịch Vụ

Sau khi services chạy, bạn có thể truy cập:

| Dịch Vụ | URL | Thông Tin |
|---------|-----|----------|
| **API** | http://localhost:8000 | FastAPI Server |
| **API Docs** | http://localhost:8000/docs | Swagger UI |
| **Neo4j Browser** | http://localhost:7474 | Graph Database |
| **PostgreSQL** | localhost:5432 | Data Database |

---

## 🔄 Chạy Sync

Sau khi services chạy, bạn có thể bắt đầu sync dữ liệu:

### Full Sync (Lần Đầu):
```bash
curl -X POST http://localhost:8000/sync/full
```

### Incremental Sync (Cập Nhật):
```bash
curl -X POST http://localhost:8000/sync/incremental
```

### Kiểm Tra Trạng Thái:
```bash
curl http://localhost:8000/sync/status
```

---

## 📊 File Structure Được Tạo

```
research-graph-sync/
├── src/                 # Source code
├── config/              # Configuration files
├── docker/              # Docker setup
├── database/            # Database schemas
├── logs/                # Application logs
├── .env                 # Environment config
├── README.md            # Documentation
├── requirements.txt     # Python packages
└── SETUP_GUIDE.md       # Troubleshooting
```

---

## ❓ Thường Gặp Vấn Đề

### ❌ "Docker not found"
**Giải Pháp:** Cài đặt Docker Desktop từ https://www.docker.com/

### ❌ "Port 8000 already in use"
**Giải Pháp:**
```bash
# Tìm process sử dụng port
lsof -i :8000
# Kill nó
kill -9 <PID>
```

### ❌ "Connection refused"
**Giải Pháp:**
```bash
# Kiểm tra services
docker-compose -f docker/docker-compose.yml ps

# Xem logs
docker-compose logs app

# Restart services
docker-compose down
docker-compose up -d
```

---

## 📚 Tài Liệu Đầy Đủ

- **TOOLKIT_README.md** - Hướng dẫn chi tiết về toolkit
- **README.md** - Tài liệu project chính
- **SETUP_GUIDE.md** - Khắc phục sự cố chi tiết

---

## ✅ Checklist Kiểm Tra

Sau setup, kiểm tra:

- [ ] Docker Desktop đang chạy
- [ ] Tất cả services đang running (`docker-compose ps`)
- [ ] API responds (`curl http://localhost:8000/health`)
- [ ] Neo4j accessible (`http://localhost:7474`)
- [ ] PostgreSQL responsive
- [ ] Logs in Docker (`docker-compose logs app`)

---

## 🎬 Bước Tiếp Theo

1. Load dữ liệu vào PostgreSQL
2. Chạy full sync lần đầu
3. Check Neo4j Browser để xem knowledge graph
4. Customize config nếu cần
5. Set up backup & monitoring

---

## 📞 Hỗ Trợ

Nếu gặp vấn đề:
1. Xem **SETUP_GUIDE.md** - Troubleshooting section
2. Kiểm tra **logs**: `docker-compose logs -f app`
3. Verify docker: `docker-compose -f docker/docker-compose.yml ps`

---

## 🎉 Xong!

**Project của bạn đã 100% sẵn sàng chạy!**

```
✅ Project structure      - DONE
✅ Configuration files    - DONE
✅ Docker setup          - DONE
✅ Database schema       - DONE
✅ Python modules        - DONE
✅ API server            - READY
✅ Documentation         - COMPLETE
```

**Bây giờ chỉ cần:**
1. Chạy `docker-compose up -d`
2. Load dữ liệu
3. Bắt đầu sync!

---

**Made with ❤️ by Research Graph Sync Setup Tool**

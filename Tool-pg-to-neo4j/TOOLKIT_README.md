# Research Graph Sync - Complete Setup Toolkit

**Tạo tool tự động setup 100% project structure, environment, và configuration**

## 📋 Tệp tin trong Toolkit

### 1. **setup_tool.py** - Main Setup Automation Script
Tool chính tự động tạo toàn bộ project structure, configuration files, Docker setup, và documentation.

**Tính năng:**
- ✅ Tạo directory structure đầy đủ
- ✅ Tạo .env environment files
- ✅ Tạo config.json configuration
- ✅ Tạo Python requirements (với tất cả dependencies)
- ✅ Tạo Dockerfile & docker-compose.yml
- ✅ Tạo Python modules (.py files) đã sẵn sàng chạy
- ✅ Tạo Database schema (init.sql)
- ✅ Tạo Documentation (README.md, SETUP_GUIDE.md)
- ✅ Tạo Utility scripts (setup, stop, logs)
- ✅ Tạo .gitignore & pre-commit config

### 2. **quick_start.py** - Interactive Quick Start Guide
Script tương tác giúp kiểm tra prerequisites và chạy setup với validation.

**Tính năng:**
- ✅ Kiểm tra Python version
- ✅ Kiểm tra Docker installation
- ✅ Kiểm tra Docker Compose
- ✅ Hỏi user về project name & path
- ✅ Chạy setup_tool.py tự động
- ✅ Verify kết quả setup
- ✅ Hướng dẫn next steps

### 3. **setup.bat** - Windows Batch Setup Script
Script dành cho Windows users, tự động kiểm tra & chạy setup.

**Tính năng:**
- ✅ Kiểm tra Docker & Docker Compose
- ✅ Tạo .env từ template
- ✅ Pull Docker images
- ✅ Start tất cả services
- ✅ Display service status & next steps

## 🚀 Cách Sử Dụng

### Option 1: Interactive Mode (Recommended - Dễ nhất)

#### Windows:
```bash
# Double-click setup.bat hoặc chạy:
setup.bat
```

#### macOS / Linux:
```bash
python3 quick_start.py
```

### Option 2: Direct Tool Usage

#### Windows:
```bash
python setup_tool.py --name research-graph-sync --path .
```

#### macOS / Linux:
```bash
python3 setup_tool.py --name research-graph-sync --path .
```

### Option 3: Custom Project Name & Location

```bash
python setup_tool.py --name my-project-name --path /path/to/location
```

## 📦 Những Gì Sẽ Được Setup

### Directory Structure:
```
research-graph-sync/
├── src/
│   ├── postgres/        # PostgreSQL loaders
│   ├── graph/           # Neo4j operations
│   ├── sync/            # Sync logic
│   ├── jobs/            # Background jobs
│   ├── api/             # FastAPI app (ready to run)
│   └── tests/           # Test suite
├── config/
│   └── config.json      # Configuration
├── database/
│   └── init.sql         # Database schema
├── docker/
│   ├── Dockerfile       # Container definition
│   └── docker-compose.yml # Services orchestration
├── scripts/
│   ├── setup.sh         # Linux setup
│   ├── stop.sh          # Stop services
│   └── logs.sh          # View logs
├── logs/                # Application logs
├── .env                 # Environment variables (configured)
├── .env.example         # Environment template
├── .gitignore           # Git ignore file
├── .pre-commit-config.yaml
├── requirements.txt     # Python dependencies
├── requirements-dev.txt # Dev dependencies
├── README.md            # Project documentation
└── SETUP_GUIDE.md       # Troubleshooting guide
```

### Environment Configuration (.env):
```ini
# Database connections
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
NEO4J_HOST=localhost
NEO4J_PORT=7687

# Application settings
APP_ENV=development
APP_LOG_LEVEL=INFO

# Sync configuration
SYNC_BATCH_SIZE=1000
SYNC_TIMEOUT=300

# Feature flags
ENABLE_FULL_SYNC=true
ENABLE_INCREMENTAL_SYNC=true
```

### Python Dependencies (requirements.txt):
- FastAPI & Uvicorn
- PostgreSQL & SQLAlchemy
- Neo4j driver
- Pandas & NumPy
- Testing tools (pytest)
- Code quality tools (black, flake8)
- Docker support

### Docker Services:
- **PostgreSQL**: Database (port 5432)
- **Neo4j**: Graph database (port 7687, UI 7474)
- **App**: FastAPI application (port 8000)

## ✅ Verification Checklist

After setup completes, verify:

```bash
# 1. Check all services are running
docker-compose -f docker/docker-compose.yml ps

# 2. Test API health
curl http://localhost:8000/health

# 3. Check Neo4j Browser
open http://localhost:7474

# 4. Test PostgreSQL connection
psql -h localhost -U research_user -d research_db

# 5. View application logs
docker-compose logs -f app
```

## 🐳 Starting Services After Setup

### First Time (Build & Start):
```bash
cd research-graph-sync
docker-compose -f docker/docker-compose.yml up -d
```

### Subsequent Times (Just Start):
```bash
docker-compose -f docker/docker-compose.yml start
```

### Stop Services:
```bash
docker-compose -f docker/docker-compose.yml stop
```

### Clean Up (Remove volumes):
```bash
docker-compose -f docker/docker-compose.yml down -v
```

## 📡 API Endpoints

After setup, these endpoints are available:

```bash
# Health check
GET http://localhost:8000/health

# Root endpoint
GET http://localhost:8000/

# Start full sync
POST http://localhost:8000/sync/full

# Start incremental sync
POST http://localhost:8000/sync/incremental

# Get sync status
GET http://localhost:8000/sync/status
```

## 🔧 Configuration Files Created

### config/config.json
Main configuration with all settings:
- Database connections
- Sync behavior
- API settings
- Logging configuration
- Performance tuning

### .env File
Environment-specific variables that override defaults.

### Dockerfile
Containerized app with:
- Python 3.11 slim image
- Health checks
- Proper signal handling

### docker-compose.yml
Complete multi-container setup with:
- PostgreSQL database
- Neo4j graph database
- FastAPI application
- Health checks
- Volume persistence
- Network isolation

## 📚 Documentation Included

### README.md
- Project overview
- Quick start guide
- API endpoints
- Project structure
- Configuration reference
- Troubleshooting

### SETUP_GUIDE.md
- Detailed installation steps
- Common issues & solutions
- Database connection testing
- Performance tuning tips

### This File
- Complete toolkit documentation
- Usage instructions
- Verification checklist

## 🛠️ Additional Scripts

After setup, these scripts are available in the `scripts/` directory:

### Linux/macOS:
```bash
./scripts/setup.sh      # Run initial setup
./scripts/stop.sh       # Stop all services
./scripts/logs.sh       # View logs
```

### Windows:
```bash
# Use docker-compose directly:
docker-compose -f docker\docker-compose.yml up -d
docker-compose -f docker\docker-compose.yml stop
docker-compose -f docker\docker-compose.yml logs -f
```

## 🚨 Troubleshooting

### Port Already in Use
```bash
# Find process using port
lsof -i :8000

# Kill it
kill -9 <PID>

# Or restart Docker
docker-compose down
docker-compose up -d
```

### Cannot Connect to Database
1. Check .env credentials
2. Verify services are running: `docker-compose ps`
3. Check logs: `docker-compose logs postgres`

### Container Won't Start
```bash
# Rebuild from scratch
docker-compose down -v
docker-compose build --no-cache
docker-compose up -d
```

## 📊 What's Ready to Run

✅ **Immediately after setup:**
- Docker containers for PostgreSQL, Neo4j, and API
- Database schema initialized
- Environment configured
- FastAPI server running on port 8000
- All endpoints functional

✅ **You just need to:**
1. Load your research data into PostgreSQL
2. Configure sync schedules (if needed)
3. Call sync endpoints to populate Neo4j

## 📞 Next Steps

After successful setup:

1. **Verify everything works:**
   ```bash
   curl http://localhost:8000/health
   ```

2. **Load data into PostgreSQL:**
   Import your research data using your preferred tools

3. **Start sync process:**
   ```bash
   curl -X POST http://localhost:8000/sync/full
   ```

4. **Monitor progress:**
   ```bash
   docker-compose logs -f app
   ```

5. **Access Neo4j:**
   Open browser to http://localhost:7474
   Login: neo4j / password from .env

6. **Customize:**
   Edit config/config.json for sync schedules, batch sizes, etc.

---

## 🎯 Summary

Este toolkit proporciona:

| Feature | Status |
|---------|--------|
| Project Structure | ✅ Automated |
| Environment Config | ✅ Templated |
| Docker Setup | ✅ Complete |
| Database Schema | ✅ Included |
| API Framework | ✅ FastAPI Ready |
| Documentation | ✅ Complete |
| Testing Setup | ✅ Configured |
| CI/CD Ready | ✅ Pre-commit hooks |

**El proyecto está 100% listo para ejecutarse después de la configuración inicial.**

---

**Created by:** AI Implementation Guide Graph Sync Tool  
**Version:** 1.0.0  
**Last Updated:** 2024

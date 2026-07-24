#!/usr/bin/env python3
"""
Research Graph Sync Tool - Automatic Setup Script
Creates project structure, environment files, and configurations
"""

import os
import sys
import json
import pathlib
from pathlib import Path
from datetime import datetime

class SetupTool:
    def __init__(self, project_name="research-graph-sync", base_path=None):
        self.project_name = project_name
        self.base_path = Path(base_path) if base_path else Path.cwd()
        self.project_path = self.base_path / project_name
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
    def create_directory_structure(self):
        """Create complete project directory structure"""
        print(f"📁 Creating project structure: {self.project_name}")
        
        directories = [
            "src/postgres",
            "src/graph",
            "src/sync",
            "src/jobs",
            "src/tests",
            "config",
            "logs",
            "database",
            "docker",
            "scripts",
            "docs",
        ]
        
        for dir_path in directories:
            full_path = self.project_path / dir_path
            full_path.mkdir(parents=True, exist_ok=True)
            print(f"  ✓ {dir_path}")
        
        return True
    
    def create_env_file(self):
        """Create .env template file"""
        print("\n📝 Creating environment configuration")
        
        env_content = """# PostgreSQL Configuration
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=research_user
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_DATABASE=research_db

# Neo4j Configuration
NEO4J_HOST=localhost
NEO4J_PORT=7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_secure_password_here
NEO4J_SCHEME=neo4j

# Application Configuration
APP_ENV=development
APP_DEBUG=true
APP_LOG_LEVEL=INFO
APP_PORT=8000
APP_HOST=0.0.0.0

# Sync Configuration
SYNC_BATCH_SIZE=1000
SYNC_TIMEOUT=300
SYNC_RETRY_ATTEMPTS=3
SYNC_RETRY_DELAY=5

# Feature Flags
ENABLE_FULL_SYNC=true
ENABLE_INCREMENTAL_SYNC=true
ENABLE_SIMILARITY_CALCULATION=true
ENABLE_COLLABORATION_ANALYSIS=true

# Performance
MAX_WORKERS=4
CONNECTION_POOL_SIZE=20
CACHE_TTL=3600

# API Configuration
API_VERSION=v1
API_TIMEOUT=30
API_RATE_LIMIT=1000

# Logging
LOG_FORMAT=json
LOG_OUTPUT_PATH=logs
"""
        
        env_path = self.project_path / ".env"
        env_path.write_text(env_content)
        print(f"  ✓ Created .env")
        
        # Also create .env.example for version control
        env_example = env_content.replace("your_secure_password_here", "CHANGE_ME")
        (self.project_path / ".env.example").write_text(env_example)
        print(f"  ✓ Created .env.example")
        
        return True
    
    def create_config_files(self):
        """Create configuration files"""
        print("\n⚙️  Creating configuration files")
        
        # Main config
        config = {
            "app": {
                "name": "research-graph-sync",
                "version": "1.0.0",
                "description": "Synchronize research data from PostgreSQL to Neo4j",
                "environment": "development"
            },
            "database": {
                "postgres": {
                    "connection_timeout": 10,
                    "pool_size": 20,
                    "max_overflow": 40,
                    "pool_recycle": 3600
                },
                "neo4j": {
                    "connection_timeout": 10,
                    "pool_size": 50,
                    "max_lifetime": 3600
                }
            },
            "sync": {
                "batch_size": 1000,
                "timeout": 300,
                "retry_attempts": 3,
                "retry_delay": 5,
                "full_sync_schedule": "0 2 * * *",
                "incremental_sync_schedule": "*/30 * * * *"
            },
            "api": {
                "version": "v1",
                "timeout": 30,
                "rate_limit": 1000,
                "cors_origins": ["http://localhost:3000", "http://localhost:8080"]
            },
            "logging": {
                "level": "INFO",
                "format": "json",
                "output": "logs",
                "max_bytes": 10485760,
                "backup_count": 10
            }
        }
        
        config_path = self.project_path / "config" / "config.json"
        config_path.write_text(json.dumps(config, indent=2))
        print(f"  ✓ Created config.json")
        
        return True
    
    def create_requirements(self):
        """Create Python requirements.txt"""
        print("\n📦 Creating requirements.txt")
        
        requirements = """# Core Framework
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
pydantic-settings==2.1.0

# Database
psycopg2-binary==2.9.9
sqlalchemy==2.0.23
neo4j==5.14.0

# Data Processing
pandas==2.1.3
numpy==1.26.2

# Async
aiohttp==3.9.1
asyncio-contextmanager==1.0.0

# Utilities
python-dotenv==1.0.0
python-dateutil==2.8.2
requests==2.31.0

# Logging & Monitoring
python-json-logger==2.0.7
structlog==23.2.0

# Testing
pytest==7.4.3
pytest-asyncio==0.21.1
pytest-cov==4.1.0
pytest-mock==3.12.0

# Code Quality
black==23.11.0
flake8==6.1.0
isort==5.12.0
mypy==1.7.1

# Deployment
docker==7.0.0
gunicorn==21.2.0

# Development
ipython==8.17.2
debugpy==1.8.0
"""
        
        req_path = self.project_path / "requirements.txt"
        req_path.write_text(requirements)
        print(f"  ✓ Created requirements.txt")
        
        # Dev requirements
        dev_requirements = """# From requirements.txt
-r requirements.txt

# Additional dev tools
pytest-watch==4.2.0
pytest-xdist==3.5.0
black[jupyter]==23.11.0
pre-commit==3.5.0
commitizen==3.12.0
"""
        
        dev_req_path = self.project_path / "requirements-dev.txt"
        dev_req_path.write_text(dev_requirements)
        print(f"  ✓ Created requirements-dev.txt")
        
        return True
    
    def create_dockerfile(self):
        """Create Docker configuration"""
        print("\n🐳 Creating Docker configuration")
        
        dockerfile = """FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    gcc \\
    postgresql-client \\
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/ ./src/
COPY config/ ./config/

# Create logs directory
RUN mkdir -p logs

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
    CMD python -c "import requests; requests.get('http://localhost:8000/health')"

# Run application
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""
        
        dockerfile_path = self.project_path / "docker" / "Dockerfile"
        dockerfile_path.write_text(dockerfile)
        print(f"  ✓ Created Dockerfile")
        
        # Docker Compose
        docker_compose = """version: '3.9'

services:
  postgres:
    image: postgres:15-alpine
    container_name: research_postgres
    environment:
      POSTGRES_USER: research_user
      POSTGRES_PASSWORD: secure_password_123
      POSTGRES_DB: research_db
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./database/init.sql:/docker-entrypoint-initdb.d/init.sql
    networks:
      - research_network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U research_user"]
      interval: 10s
      timeout: 5s
      retries: 5

  neo4j:
    image: neo4j:5.14-enterprise
    container_name: research_neo4j
    environment:
      NEO4J_AUTH: neo4j/secure_password_123
      NEO4J_apoc_export_file_enabled: "true"
      NEO4J_apoc_import_file_enabled: "true"
    ports:
      - "7687:7687"
      - "7474:7474"
    volumes:
      - neo4j_data:/data
      - neo4j_logs:/logs
    networks:
      - research_network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:7474"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    build:
      context: .
      dockerfile: docker/Dockerfile
    container_name: research_graph_sync
    environment:
      APP_ENV: docker
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_USER: research_user
      POSTGRES_PASSWORD: secure_password_123
      POSTGRES_DATABASE: research_db
      NEO4J_HOST: neo4j
      NEO4J_PORT: 7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: secure_password_123
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      neo4j:
        condition: service_healthy
    networks:
      - research_network
    volumes:
      - ./logs:/app/logs
    command: uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

volumes:
  postgres_data:
  neo4j_data:
  neo4j_logs:

networks:
  research_network:
    driver: bridge
"""
        
        compose_path = self.project_path / "docker" / "docker-compose.yml"
        compose_path.write_text(docker_compose)
        print(f"  ✓ Created docker-compose.yml")
        
        return True
    
    def create_python_modules(self):
        """Create Python module files"""
        print("\n🐍 Creating Python modules")
        
        # Create __init__.py files
        init_content = '"""Module initialization"""\n\n__version__ = "1.0.0"\n'
        
        modules = [
            "src/__init__.py",
            "src/postgres/__init__.py",
            "src/graph/__init__.py",
            "src/sync/__init__.py",
            "src/jobs/__init__.py",
            "src/api/__init__.py",
            "src/tests/__init__.py",
        ]
        
        for module in modules:
            module_path = self.project_path / module
            module_path.write_text(init_content)
            print(f"  ✓ {module}")
        
        # Create main API file
        main_api = '''"""Main API application"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

app = FastAPI(
    title="Research Graph Sync API",
    version="1.0.0",
    description="API for synchronizing research data to Neo4j"
)

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    print("🚀 Application starting...")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    print("🛑 Application shutting down...")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "1.0.0"}

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "Research Graph Sync API",
        "version": "1.0.0",
        "status": "running"
    }

@app.post("/sync/full")
async def full_sync():
    """Execute full synchronization"""
    return {
        "message": "Full sync initiated",
        "status": "queued"
    }

@app.post("/sync/incremental")
async def incremental_sync():
    """Execute incremental synchronization"""
    return {
        "message": "Incremental sync initiated",
        "status": "queued"
    }

@app.get("/sync/status")
async def sync_status():
    """Get synchronization status"""
    return {
        "status": "idle",
        "last_sync": None,
        "next_sync": None
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''
        
        main_path = self.project_path / "src/api/main.py"
        main_path.write_text(main_api)
        print(f"  ✓ Created src/api/main.py")
        
        return True
    
    def create_database_schema(self):
        """Create database initialization script"""
        print("\n📊 Creating database schema")
        
        init_sql = """-- Research Database Schema Initialization
-- PostgreSQL

CREATE TABLE IF NOT EXISTS publisher (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS zone (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS subject_area (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) UNIQUE,
    description TEXT
);

CREATE TABLE IF NOT EXISTS subject_category (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    area_id INTEGER REFERENCES subject_area(id),
    code VARCHAR(50) UNIQUE
);

CREATE TABLE IF NOT EXISTS journal (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    publisher_id INTEGER REFERENCES publisher(id),
    type VARCHAR(50),
    sjr DECIMAL(10, 4),
    quartile VARCHAR(10),
    h_index INTEGER,
    open_access BOOLEAN DEFAULT FALSE,
    diamond_oa BOOLEAN DEFAULT FALSE,
    issn VARCHAR(50) UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journal_subject_category (
    journal_id INTEGER REFERENCES journal(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES subject_category(id) ON DELETE CASCADE,
    PRIMARY KEY (journal_id, category_id)
);

CREATE TABLE IF NOT EXISTS journal_ranking (
    id SERIAL PRIMARY KEY,
    journal_id INTEGER REFERENCES journal(id),
    year INTEGER,
    rank INTEGER,
    quartile VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS author (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    h_index INTEGER,
    cited_by_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS volume (
    id SERIAL PRIMARY KEY,
    journal_id INTEGER REFERENCES journal(id),
    volume_number INTEGER,
    publication_year INTEGER
);

CREATE TABLE IF NOT EXISTS issue (
    id SERIAL PRIMARY KEY,
    volume_id INTEGER REFERENCES volume(id),
    issue_number INTEGER
);

CREATE TABLE IF NOT EXISTS article (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    doi VARCHAR(255) UNIQUE,
    journal_id INTEGER REFERENCES journal(id),
    issue_id INTEGER REFERENCES issue(id),
    publication_year INTEGER,
    abstract TEXT,
    page_start INTEGER,
    page_end INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cited_by_count BIGINT,
    final_references JSONB
);

CREATE TABLE IF NOT EXISTS author_article (
    author_id INTEGER REFERENCES author(id) ON DELETE CASCADE,
    article_id INTEGER REFERENCES article(id) ON DELETE CASCADE,
    author_order INTEGER,
    PRIMARY KEY (author_id, article_id)
);

CREATE TABLE IF NOT EXISTS topic (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sub_topic (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    topic_id INTEGER REFERENCES topic(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS keyword (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    frequency INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS keyword_article (
    keyword_id INTEGER REFERENCES keyword(id) ON DELETE CASCADE,
    article_id INTEGER REFERENCES article(id) ON DELETE CASCADE,
    PRIMARY KEY (keyword_id, article_id)
);

CREATE TABLE IF NOT EXISTS article_topic (
    article_id INTEGER REFERENCES article(id) ON DELETE CASCADE,
    topic_id INTEGER REFERENCES topic(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, topic_id)
);

-- Create indexes for performance
CREATE INDEX idx_journal_publisher ON journal(publisher_id);
CREATE INDEX idx_journal_name ON journal(name);
CREATE INDEX idx_article_journal ON article(journal_id);
CREATE INDEX idx_article_year ON article(publication_year);
CREATE INDEX idx_author_article ON author_article(author_id, article_id);
CREATE INDEX idx_keyword_article ON keyword_article(keyword_id, article_id);
CREATE INDEX idx_article_topic ON article_topic(article_id, topic_id);

-- Create sync tracking table
CREATE TABLE IF NOT EXISTS sync_log (
    id SERIAL PRIMARY KEY,
    sync_type VARCHAR(50),
    status VARCHAR(50),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    records_processed INTEGER,
    errors TEXT
);

-- Create sync metadata table
CREATE TABLE IF NOT EXISTS sync_metadata (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(100),
    last_sync TIMESTAMP,
    last_modified TIMESTAMP
);
"""
        
        schema_path = self.project_path / "database" / "init.sql"
        schema_path.write_text(init_sql)
        print(f"  ✓ Created database schema (init.sql)")
        
        return True
    
    def create_documentation(self):
        """Create documentation files"""
        print("\n📚 Creating documentation")
        
        readme = f"""# Research Graph Sync

A production-ready service for synchronizing research data from PostgreSQL to Neo4j.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Python 3.11+ (if running locally)
- PostgreSQL 15+
- Neo4j 5.14+

### Setup with Docker

1. **Clone and navigate to project:**
```bash
cd {self.project_name}
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your settings
```

3. **Start all services:**
```bash
docker-compose -f docker/docker-compose.yml up -d
```

4. **Check status:**
```bash
docker-compose -f docker/docker-compose.yml ps
```

5. **Access services:**
- API: http://localhost:8000
- Neo4j Browser: http://localhost:7474
- PostgreSQL: localhost:5432

### Setup Locally (Development)

1. **Create virtual environment:**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\\Scripts\\activate
```

2. **Install dependencies:**
```bash
pip install -r requirements-dev.txt
```

3. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your local database credentials
```

4. **Run migrations:**
```bash
python scripts/migrate.py
```

5. **Start development server:**
```bash
uvicorn src.api.main:app --reload
```

## API Endpoints

### Health Check
```
GET /health
```

### Synchronization

**Full Sync:**
```
POST /sync/full
```

**Incremental Sync:**
```
POST /sync/incremental
```

**Sync Status:**
```
GET /sync/status
```

## Project Structure

```
{self.project_name}/
├── src/
│   ├── postgres/        # PostgreSQL data loaders
│   ├── graph/           # Neo4j graph operations
│   ├── sync/            # Sync logic
│   ├── jobs/            # Background jobs
│   ├── api/             # FastAPI application
│   └── tests/           # Test suite
├── config/              # Configuration files
├── database/            # Database schemas
├── docker/              # Docker configuration
├── scripts/             # Utility scripts
├── logs/                # Application logs
├── .env                 # Environment variables
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## Database Schema

### Nodes
- Journal
- Publisher
- Author
- Article
- Topic
- Keyword
- Category
- Area

### Relationships
- PUBLISHES (Publisher -> Journal)
- LOCATED_IN (Journal -> Country)
- BELONGS_TO (Journal -> Category)
- PUBLISHED_IN (Article -> Journal)
- WRITES (Author -> Article)
- HAS_TOPIC (Article -> Topic)
- HAS_KEYWORD (Article -> Keyword)
- COLLABORATES_WITH (Author -> Author)
- RELATED_TO (Keyword -> Keyword)

## Monitoring

### Logs
```bash
docker-compose -f docker/docker-compose.yml logs -f app
```

### Database Health
```bash
# PostgreSQL
docker exec research_postgres pg_isready

# Neo4j
curl http://localhost:7474/db/neo4j/
```

## Testing

```bash
# Run all tests
pytest

# With coverage
pytest --cov=src

# Watch mode
ptw
```

## Configuration

All configuration via `.env` or `config/config.json`:

- `POSTGRES_*`: PostgreSQL connection
- `NEO4J_*`: Neo4j connection
- `APP_*`: Application settings
- `SYNC_*`: Sync behavior
- `LOG_*`: Logging configuration

## Troubleshooting

### Connection Issues
1. Verify services are running: `docker-compose ps`
2. Check credentials in `.env`
3. Review logs: `docker-compose logs app`

### Sync Failures
1. Check data integrity in PostgreSQL
2. Verify Neo4j has sufficient disk space
3. Review sync logs in database

### Performance
1. Adjust BATCH_SIZE in .env
2. Increase MAX_WORKERS
3. Configure connection pool sizes

## Support

For issues and questions, refer to documentation or check logs.

---

Created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""
        
        readme_path = self.project_path / "README.md"
        readme_path.write_text(readme)
        print(f"  ✓ Created README.md")
        
        # Create SETUP guide
        setup_guide = f"""# Setup Guide - Research Graph Sync

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## Installation Steps

### Step 1: Install Docker Desktop
- Download from https://www.docker.com/products/docker-desktop
- Install and start Docker Desktop

### Step 2: Navigate to Project
```bash
cd {self.project_name}
```

### Step 3: Configure Environment
```bash
# Copy template
cp .env.example .env

# Edit with your values
# Important settings to configure:
# - POSTGRES_PASSWORD
# - NEO4J_PASSWORD
# - APP_LOG_LEVEL
```

### Step 4: Start Services
```bash
# Build and start all containers
docker-compose -f docker/docker-compose.yml up -d

# Wait for services to be healthy (30-60 seconds)
docker-compose -f docker/docker-compose.yml ps
```

### Step 5: Verify Installation
```bash
# Check API health
curl http://localhost:8000/health

# Check Neo4j
open http://localhost:7474  # username: neo4j, password: from .env

# Check PostgreSQL
psql -h localhost -U research_user -d research_db
```

### Step 6: Run Initial Sync
```bash
# Full sync
curl -X POST http://localhost:8000/sync/full

# Check status
curl http://localhost:8000/sync/status
```

## Troubleshooting

### Port Already in Use
```bash
# Find and kill process using port
# On Windows:
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# On macOS/Linux:
lsof -i :8000
kill -9 <PID>
```

### Container Won't Start
```bash
# Check logs
docker-compose logs postgres
docker-compose logs neo4j
docker-compose logs app

# Rebuild containers
docker-compose down -v
docker-compose up -d
```

### Database Connection Error
```bash
# Verify credentials in .env
# Test PostgreSQL
docker exec research_postgres psql -U research_user -d research_db -c "SELECT 1"

# Test Neo4j
docker exec research_neo4j cypher-shell -u neo4j -p <password>
```

## Next Steps

1. Load your research data into PostgreSQL
2. Configure sync schedules in config/config.json
3. Set up monitoring and alerting
4. Configure backups for both databases

---

For more information, see README.md
"""
        
        setup_path = self.project_path / "SETUP_GUIDE.md"
        setup_path.write_text(setup_guide)
        print(f"  ✓ Created SETUP_GUIDE.md")
        
        return True
    
    def create_scripts(self):
        """Create utility scripts"""
        print("\n🔧 Creating utility scripts")
        
        # Setup script
        setup_script = """#!/bin/bash
# Quick setup script for Research Graph Sync

echo "🚀 Research Graph Sync Setup"
echo "=============================="

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker Desktop."
    exit 1
fi

echo "✓ Docker found"

# Check Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed"
    exit 1
fi

echo "✓ Docker Compose found"

# Create .env if not exists
if [ ! -f .env ]; then
    echo "📝 Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Please edit .env with your settings"
fi

# Create logs directory
mkdir -p logs

# Pull latest images
echo "📥 Pulling Docker images..."
docker-compose -f docker/docker-compose.yml pull

# Start services
echo "🐳 Starting services..."
docker-compose -f docker/docker-compose.yml up -d

# Wait for services
echo "⏳ Waiting for services to be healthy..."
sleep 30

# Check health
echo "🏥 Checking service health..."
docker-compose -f docker/docker-compose.yml ps

# Final message
echo ""
echo "✅ Setup complete!"
echo ""
echo "Access points:"
echo "  - API: http://localhost:8000"
echo "  - Neo4j: http://localhost:7474"
echo "  - PostgreSQL: localhost:5432"
echo ""
echo "Next steps:"
echo "  1. Configure .env if needed"
echo "  2. Run: curl http://localhost:8000/health"
echo "  3. Initiate sync: curl -X POST http://localhost:8000/sync/full"
"""
        
        setup_sh = self.project_path / "scripts" / "setup.sh"
        setup_sh.write_text(setup_script)
        setup_sh.chmod(0o755)
        print(f"  ✓ Created scripts/setup.sh")
        
        # Stop script
        stop_script = """#!/bin/bash
# Stop all services

echo "🛑 Stopping services..."
docker-compose -f docker/docker-compose.yml down
echo "✅ Services stopped"
"""
        
        stop_sh = self.project_path / "scripts" / "stop.sh"
        stop_sh.write_text(stop_script)
        stop_sh.chmod(0o755)
        print(f"  ✓ Created scripts/stop.sh")
        
        # Logs script
        logs_script = """#!/bin/bash
# View logs

if [ "$1" == "app" ]; then
    docker-compose logs -f app
elif [ "$1" == "postgres" ]; then
    docker-compose logs -f postgres
elif [ "$1" == "neo4j" ]; then
    docker-compose logs -f neo4j
else
    docker-compose logs -f
fi
"""
        
        logs_sh = self.project_path / "scripts" / "logs.sh"
        logs_sh.write_text(logs_script)
        logs_sh.chmod(0o755)
        print(f"  ✓ Created scripts/logs.sh")
        
        return True
    
    def create_gitignore(self):
        """Create .gitignore file"""
        print("\n🔒 Creating .gitignore")
        
        gitignore = """# Environment
.env
.env.local
.env.*.local

# Virtual environments
venv/
env/
ENV/
.venv

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
pip-log.txt
pip-delete-this-directory.txt
.pytest_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~
.DS_Store

# Logs
logs/
*.log

# Docker
.dockerignore

# Database
*.db
*.sqlite
*.sqlite3

# Temporary files
*.tmp
temp/
tmp/

# OS
.DS_Store
Thumbs.db

# Project specific
.sync_state
backup/
"""
        
        gitignore_path = self.project_path / ".gitignore"
        gitignore_path.write_text(gitignore)
        print(f"  ✓ Created .gitignore")
        
        return True
    
    def create_pre_commit_config(self):
        """Create pre-commit configuration"""
        print("\n🔍 Creating pre-commit configuration")
        
        pre_commit = """# Pre-commit hooks configuration
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-json
      - id: check-merge-conflict

  - repo: https://github.com/psf/black
    rev: 23.11.0
    hooks:
      - id: black
        language_version: python3.11

  - repo: https://github.com/PyCQA/isort
    rev: 5.12.0
    hooks:
      - id: isort
        args: ["--profile", "black"]

  - repo: https://github.com/PyCQA/flake8
    rev: 6.1.0
    hooks:
      - id: flake8
        args: ["--max-line-length=100"]
"""
        
        pre_commit_path = self.project_path / ".pre-commit-config.yaml"
        pre_commit_path.write_text(pre_commit)
        print(f"  ✓ Created .pre-commit-config.yaml")
        
        return True
    
    def run_complete_setup(self):
        """Run complete setup"""
        print("\n" + "="*60)
        print("🛠️  RESEARCH GRAPH SYNC - COMPLETE SETUP")
        print("="*60)
        
        steps = [
            ("Directory Structure", self.create_directory_structure),
            ("Environment Files", self.create_env_file),
            ("Configuration", self.create_config_files),
            ("Requirements", self.create_requirements),
            ("Docker Setup", self.create_dockerfile),
            ("Python Modules", self.create_python_modules),
            ("Database Schema", self.create_database_schema),
            ("Documentation", self.create_documentation),
            ("Scripts", self.create_scripts),
            (".gitignore", self.create_gitignore),
            ("Pre-commit Config", self.create_pre_commit_config),
        ]
        
        failed = []
        
        for step_name, step_func in steps:
            try:
                step_func()
            except Exception as e:
                print(f"  ❌ Error: {e}")
                failed.append(step_name)
        
        print("\n" + "="*60)
        if not failed:
            print("✅ SETUP COMPLETE!")
            print("="*60)
            print(f"\n📁 Project created at: {self.project_path}")
            print("\n🚀 Next steps:")
            print(f"\n1. Navigate to project:")
            print(f"   cd {self.project_name}")
            print(f"\n2. Configure environment:")
            print(f"   Edit .env with your database credentials")
            print(f"\n3. Start services with Docker:")
            print(f"   docker-compose -f docker/docker-compose.yml up -d")
            print(f"\n4. Verify installation:")
            print(f"   curl http://localhost:8000/health")
            print(f"\n5. Check documentation:")
            print(f"   - README.md for overview")
            print(f"   - SETUP_GUIDE.md for troubleshooting")
            print(f"\n📚 Documentation files created:")
            print(f"   - README.md")
            print(f"   - SETUP_GUIDE.md")
            print(f"   - .env (configure with your settings)")
            print("\n" + "="*60)
        else:
            print("⚠️  SETUP COMPLETED WITH ISSUES")
            print("="*60)
            print(f"\nFailed steps: {', '.join(failed)}")
            print("\nTroubleshooting:")
            print("- Check file permissions")
            print("- Ensure directory is writable")
            print("- Check disk space")
        
        return len(failed) == 0


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Research Graph Sync Setup Tool"
    )
    parser.add_argument(
        "--name",
        default="research-graph-sync",
        help="Project name (default: research-graph-sync)"
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Base path for project (default: current directory)"
    )
    
    args = parser.parse_args()
    
    setup = SetupTool(
        project_name=args.name,
        base_path=args.path
    )
    
    success = setup.run_complete_setup()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

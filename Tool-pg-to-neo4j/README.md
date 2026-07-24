# Research Graph Sync

A production-ready service for synchronizing research data from PostgreSQL to Neo4j.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Windows/Mac/Linux

### Start Services

```bash
cd c:\Users\Duyga\Downloads\Tool
docker-compose -f docker/docker-compose.yml up -d
```

Wait 30-60 seconds for services to be healthy.

### Access Services

- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **Neo4j Browser**: http://localhost:7474
- **PostgreSQL**: localhost:5432

### Credentials

- **Neo4j**: Username `neo4j`, Password `secure_password_123`
- **PostgreSQL**: Username `research_user`, Password `secure_password_123`

## API Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Full sync
curl -X POST http://localhost:8000/sync/full

# Incremental sync
curl -X POST http://localhost:8000/sync/incremental

# Sync status
curl http://localhost:8000/sync/status
```

## Stop Services

```bash
docker-compose -f docker/docker-compose.yml stop
```

## Remove Everything

```bash
docker-compose -f docker/docker-compose.yml down -v
```

## Troubleshooting

### Port 8000 already in use
```bash
netstat -ano | findstr :8000
taskkill /PID <PID> /F
```

### Container won't start
```bash
docker-compose -f docker/docker-compose.yml logs app
```

### Reset everything
```bash
docker-compose -f docker/docker-compose.yml down -v
docker-compose -f docker/docker-compose.yml up -d
```

---

**Ready to use! Just run:**
```bash
docker-compose -f docker/docker-compose.yml up -d
```

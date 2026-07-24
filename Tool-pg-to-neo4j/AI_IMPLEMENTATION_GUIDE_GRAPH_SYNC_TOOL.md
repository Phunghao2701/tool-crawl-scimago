# AI_IMPLEMENTATION_GUIDE_GRAPH_SYNC_TOOL.md

# MISSION

Build a standalone service named `research-graph-sync`.

The purpose of this service is to synchronize research data from PostgreSQL into a Graph Database (Neo4j preferred).

This is NOT a dashboard service.
This is NOT an analytics warehouse.

The Graph Database will be used for:

- Knowledge Graph
- Recommendation Engine
- Author Collaboration Analysis
- Topic Relationship Analysis
- Keyword Relationship Analysis
- Similar Journal Discovery
- Similar Author Discovery

---

# ARCHITECTURE

```text
PostgreSQL (Source of Truth)
            │
            ▼
     Graph Sync Tool
            │
            ▼
          Neo4j
            │
            ├── Recommendation
            ├── Knowledge Graph
            ├── Similarity Search
            └── Network Analysis
```

---

# SOURCE DATABASE

Read data from PostgreSQL.

Tables:

- journal
- publisher
- zone
- subject_area
- subject_category
- journal_subject_category
- journal_ranking
- article
- author
- author_article
- topic
- sub_topic
- keyword
- keyword_article
- volume
- issue

Never write back to PostgreSQL.

---

# GRAPH MODEL

## Nodes

Journal
Publisher
Country
Area
Category
Article
Author
Topic
Keyword

### Journal

Properties:

- id
- name
- type
- sjr
- quartile
- h_index
- open_access
- diamond_oa

### Author

Properties:

- id
- name
- h_index
- cited_by_count

### Article

Properties:

- id
- title
- publication_year
- doi

### Topic

Properties:

- id
- name

### Keyword

Properties:

- id
- name

---

# RELATIONSHIPS

Publisher -> Journal

PUBLISHES

Journal -> Country

LOCATED_IN

Journal -> Category

BELONGS_TO

Category -> Area

PART_OF

Article -> Journal

PUBLISHED_IN

Author -> Article

WRITES

Article -> Topic

HAS_TOPIC

Article -> Keyword

HAS_KEYWORD

---

# DERIVED RELATIONSHIPS

Generate automatically.

## Author Collaboration

If multiple authors belong to the same article:

Author
COLLABORATES_WITH
Author

Properties:

- paper_count

---

## Keyword Co-occurrence

If multiple keywords belong to the same article:

Keyword
RELATED_TO
Keyword

Properties:

- frequency

---

## Topic Co-occurrence

Topic
RELATED_TO
Topic

Properties:

- frequency

---

## Journal Similarity

Build similarity using:

- Subject Categories
- Topics
- Keywords

Relationship:

SIMILAR_TO

Property:

- similarity_score

---

# FULL SYNC

Implement:

POST /sync/full

Process:

1. Load all PostgreSQL data
2. Create nodes
3. Create relationships
4. Create derived relationships
5. Rebuild similarity graph

---

# INCREMENTAL SYNC

Implement:

POST /sync/incremental

Process:

1. Detect changed data
2. Detect new records
3. Sync updates only
4. Refresh affected graph relationships

---

# PROJECT STRUCTURE

```text
research-graph-sync/

src/

postgres/
graph/
sync/
jobs/
api/
tests/
config/
logs/
```

---

# REQUIRED MODULES

postgres/

- journal_loader
- article_loader
- author_loader
- keyword_loader
- topic_loader

graph/

- neo4j_client
- node_builder
- relationship_builder

sync/

- sync_journal
- sync_article
- sync_author
- sync_keyword
- sync_topic

jobs/

- full_sync
- incremental_sync

---

# IMPORTANT RULE

Graph Database is NOT used for trend charts.

Trend charts remain in PostgreSQL analytics layer.

Examples:

- Keyword Trend
- Topic Trend
- Country Trend
- Journal Trend
- SJR Trend
- Quartile Trend

These must stay in PostgreSQL.

Graph Database only serves:

- Networks
- Recommendations
- Similarity
- Knowledge Graph Queries

---

# EXPECTED OUTPUT

A production-ready service that:

1. Connects to PostgreSQL.
2. Reads research entities.
3. Creates graph nodes.
4. Creates graph relationships.
5. Builds collaboration networks.
6. Builds keyword/topic networks.
7. Builds journal similarity graph.
8. Syncs data daily.
9. Exposes REST APIs.
10. Supports Neo4j as primary graph database.

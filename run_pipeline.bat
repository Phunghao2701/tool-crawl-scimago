@echo off
rem Change directory to the folder where this batch file is located
cd /d "%~dp0"

rem Auto-create .env file if it does not exist
if not exist .env (
    echo DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/researchpulse > .env
    echo OPENALEX_EMAIL=academic-etl@example.com >> .env
    echo SEMANTIC_SCHOLAR_BASE_URL=https://api.semanticscholar.org/graph/v1 >> .env
    echo SEMANTIC_SCHOLAR_RPS=1 >> .env
    echo [INFO] Da tu dong tao file .env mac dinh vi khong tim thay!
)

chcp 65001 > nul
title Scimago and OpenAlex ETL Pipeline Control Panel
color 0B

rem Non-interactive smoke test for the Neo4j menu option. This only prints
rem the command and never connects to PostgreSQL or Neo4j.
set "PIPELINE_DRY_RUN="
set "PIPELINE_DRY_RUN_EXIT="
if /I "%~1"=="--dry-run-neo4j" (
    set "PIPELINE_DRY_RUN=1"
    set "PIPELINE_DRY_RUN_EXIT=1"
    set "neo4j_args=--type full --limit 100"
    goto run_neo4j
)
set "PIPELINE_EMBED_DRY_RUN_EXIT="
set "PIPELINE_EMBED_MENU_DRY_RUN_EXIT="
if /I "%~1"=="--dry-run-embedding-menu" (
    set "PIPELINE_EMBED_MENU_DRY_RUN_EXIT=1"
    goto embed_db
)
if /I "%~1"=="--dry-run-embedding" (
    set "PIPELINE_EMBED_DRY_RUN_EXIT=1"
    set "embedding_is_dry_run=1"
    set "embedding_args=--dimension 768 --limit 100 --dry-run"
    goto run_embedding
)
set "PIPELINE_MEILI_DRY_RUN_EXIT="
if /I "%~1"=="--dry-run-meilisearch" (
    set "PIPELINE_MEILI_DRY_RUN_EXIT=1"
    goto run_meilisearch
)

:menu
cls

rem Hien thi DB dang active
for /f "tokens=2 delims==" %%A in ('findstr /i "DATABASE_URL" .env 2^>nul') do set CURRENT_DB=%%A
echo ====================================================================
echo             SCIMAGO AND OPENALEX ETL PIPELINE CONTROL PANEL
echo ====================================================================
echo  DB dang dung: %CURRENT_DB%
echo ====================================================================
echo  0. Switch DB: Chuyen giua ResearchPulse (100.121.61.95) va Local Docker (5433)
echo  1. Setup Environment - DB: Cai dat dependencies, Docker, Schema, Seed
echo  2. Import Scimago Data: Import file Scimago tho tu thu muc data
echo  3. Sync OpenAlex Journals: Dong bo thong tin tap chi tu OpenAlex API
echo  4. Sync OpenAlex Works: Dong bo bai bao (Works, Topics, Keywords)
echo  5. Sync OpenAlex Authors: Dong bo chi tiet tac gia (Da luong sieu nhanh)
echo  E. Enrich Semantic Scholar: Lam giau du lieu bai bao tu Semantic Scholar
echo  R. Backfill References: Lay references chi tiet tu OpenAlex / Semantic
echo  6. Export Report: Xuat bao cao Excel va CSV
echo  7. View Database Statistics: Xem so lieu thong ke trong database
echo  8. Run FULL Pipeline: Chay lien tuc tu Import den Sync den Export
echo  M. Migrate Data: Chuyen data tu Local Docker sang ResearchPulse (100.121.61.95)
echo  N. Sync PostgreSQL -^> Neo4j: Dong bo knowledge graph
echo  V. Embed Article Vectors: Tao vector embedding trong PostgreSQL
echo  L. Sync PostgreSQL -^> Meilisearch: Dong bo search indexes
echo  9. Exit: Thoat
echo ====================================================================
choice /c 1234567890ERMNVL /n /m "Vui long chon chuc nang (1-9, 0, E, R, M, N, V, L): "
echo [DEBUG] Lua chon nhan duoc: errorlevel=%errorlevel%
if errorlevel 16 goto sync_meilisearch
if errorlevel 15 goto embed_db
if errorlevel 14 goto sync_neo4j
if errorlevel 13 goto migrate_db
if errorlevel 12 goto backfill_references
if errorlevel 11 goto sync_semantic
if errorlevel 10 goto switch_db
if errorlevel 9 goto exit
if errorlevel 8 goto full
if errorlevel 7 goto stats
if errorlevel 6 goto export
if errorlevel 5 goto sync_authors_cmd
if errorlevel 4 goto sync_works
if errorlevel 3 goto sync_journals
if errorlevel 2 goto import
if errorlevel 1 goto setup

:setup
cls
echo ==========================================
echo  1. SETUP ENVIRONMENT AND DATABASE
echo ==========================================
echo [INFO] Dang cai dat cac thu vien Python can thiet...
pip install -r requirements.txt
pip install openpyxl

echo [INFO] Dang kiem tra ket noi va schema database researchpulse...
python tools\scimago_etl.py stats

echo.
echo [OK] Setup moi truong va co so du lieu hoan tat thanh cong!
echo.
pause
goto menu

:import
cls
call :check_db
echo ==========================================
echo  2. IMPORT SCIMAGO DATA
echo ==========================================
echo [INFO] Danh sach cac file trong thu muc data:
dir /b data\*.csv data\*.xls data\*.xlsx 2>nul
echo.
echo [INFO] Ban co the keo tha truc tiep file tu Windows Explorer vao cua so nay.
set filepath=
set /p filepath="Nhap duong dan file (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set "filepath=%filepath:"=%"
set year=
set /p year="Nhap nam du lieu (Mac dinh: 2025): "
if "%year%"=="" set year=2025

echo [INFO] Bat dau qua trinh Import du lieu...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
echo.
pause
goto menu

:sync_journals
cls
call :check_db
echo ==========================================
echo  3. SYNC OPENALEX JOURNALS
echo ==========================================
set j_limit=
set /p j_limit="Nhap gioi han so tap chi can sync (nhan Enter de sync 50, nhap 0 de sync TOAN BO): "
if "%j_limit%"=="" set j_limit=50

echo.
echo [INFO] Bat dau dong bo Tap chi (Journals)...
python tools/openalex_sync.py sync --limit %j_limit%
echo.
echo [OK] Dong bo tap chi hoan tat!
echo.
pause
goto menu

:sync_works
cls
call :check_db
echo ==========================================
echo  4. SYNC OPENALEX WORKS
echo ==========================================
echo  1. Standard Sync (20 bai/tap chi - nhanh)
echo  2. Target 2M Articles (Tu dong cao den moc 2,000,000 bai bao - Khuyen dung)
echo  3. Custom Limit (Tuy chon so luong theo y muon)
echo.
set w_choice=
set /p w_choice="Lua chon cua ban (1-3, Mac dinh 2): "
if "%w_choice%"=="" set w_choice=2

if "%w_choice%"=="1" (
    echo.
    echo [INFO] Bat dau dong bo 20 bai viet moi tap chi...
    python tools/openalex_sync.py sync-works --limit 20
) else if "%w_choice%"=="2" (
    echo.
    echo [INFO] Bat dau dong bo huong den moc 2,000,000 bai bao (Uu tien tap chi chua co bai)...
    python tools/openalex_sync.py sync-works --limit 70 --target-total 2000000
) else (
    echo.
    set w_limit=
    set /p w_limit="Nhap gioi han bai viet can sync moi tap chi (nhan Enter de sync 70): "
    if "!w_limit!"=="" set w_limit=70
    set w_target=
    set /p w_target="Nhap tong so bai bao can dung (nhan Enter de dung 2,000,000, nhap 0 de khong gioi han): "
    if "!w_target!"=="" set w_target=2000000
    if "!w_target!"=="0" (
        python tools/openalex_sync.py sync-works --limit !w_limit!
    ) else (
        python tools/openalex_sync.py sync-works --limit !w_limit! --target-total !w_target!
    )
)
echo.
echo [OK] Dong bo bai bao hoan tat!
echo.
pause
goto menu

:sync_authors_cmd
cls
call :check_db
echo ==========================================
echo  5. SYNC OPENALEX AUTHORS
echo ==========================================
set a_limit=
set /p a_limit="Nhap gioi han so tac gia can sync (nhan Enter de sync TOAN BO): "

echo.
if "%a_limit%"=="" goto sync_all_authors
echo [INFO] Bat dau dong bo %a_limit% Tac gia (Da luong)...
python tools/openalex_sync.py sync-authors --limit %a_limit%
goto sync_authors_end

:sync_all_authors
echo [INFO] Bat dau dong bo toan bo Tac gia (Da luong)...
python tools/openalex_sync.py sync-authors

:sync_authors_end
echo.
echo [OK] Dong bo tac gia hoan tat!
echo.
pause
goto menu

:export
cls
call :check_db
echo ==========================================
echo  6. EXPORT REPORT (EXCEL AND CSV)
echo ==========================================
echo  1. Export Journal Report (Mac dinh)
echo  2. Export Author Report
echo  3. Export Article / Work Report (including Topics & Keywords)
echo.
set /p exp_choice="Lua chon cua ban (1-3, Mac dinh 1): "
if "%exp_choice%"=="2" goto export_author
if "%exp_choice%"=="3" goto export_works

echo [INFO] Dang ket hop du lieu va xuat bao cao Journal...
python tools/openalex_sync.py export
goto export_end

:export_author
echo [INFO] Dang ket hop du lieu va xuat bao cao Author...
python tools/openalex_sync.py export-authors
goto export_end

:export_works
echo [INFO] Dang ket hop du lieu va xuat bao cao Article / Work...
python tools/openalex_sync.py export-works

:export_end
echo.
pause
goto menu

:stats
cls
call :check_db
echo ==========================================
echo  7. DATABASE STATISTICS
echo ==========================================
python tools/openalex_sync.py stats
python tools/openalex_sync.py stats-authors
python tools/openalex_sync.py stats-works
echo.
pause
goto menu

:full
cls
call :check_db
echo ==========================================
echo  8. RUN FULL PIPELINE (IMPORT - SYNC - EXPORT)
echo ==========================================
echo [INFO] Ban co the keo tha truc tiep file tu Windows Explorer vao cua so nay.
set filepath=
set /p filepath="Nhap duong dan file (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set "filepath=%filepath:"=%"
set year=
set /p year="Nhap nam du lieu (Mac dinh: 2025): "
if "%year%"=="" set year=2025

echo.
echo  Chon che do Pipeline:
echo  1. Quick Test (Dong bo mau 50 Journals, 20 Works moi Journal)
echo  2. Full Overnight (Dong bo TOAN BO tap chi, bai bao, tac gia qua dem)
set /p full_pipeline_mode="Lua chon cua ban (1/2, Mac dinh 1): "
if "%full_pipeline_mode%"=="" set full_pipeline_mode=1

echo.
echo [1/3] Dang tien hanh Import Scimago...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
if errorlevel 1 (
    echo [ERROR] Qua trinh Import gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [2/3] Dang tien hanh dong bo hoa tu OpenAlex API...
if "%full_pipeline_mode%"=="2" (
    echo [INFO] Chay che do FULL qua dem: Khong gioi han so luong...
    python tools/openalex_sync.py sync
    python tools/openalex_sync.py sync-works
    python tools/openalex_sync.py sync-authors
) else (
    echo [INFO] Chay che do Quick Test: 50 Journals, 20 Works...
    python tools/openalex_sync.py sync --limit 50
    python tools/openalex_sync.py sync-works --limit 20
    python tools/openalex_sync.py sync-authors --limit 50
)
if errorlevel 1 (
    echo [ERROR] Qua trinh dong bo hoa OpenAlex gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [2.5/3] Dang tien hanh lam giau du lieu bai bao tu Semantic Scholar API...
if "%full_pipeline_mode%"=="2" (
    python tools/semantic_scholar_sync.py enrich-articles --only-missing
) else (
    python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit 20
)

echo.
echo [3/3] Dang tien hanh ket xuat tat ca cac bao cao Excel va CSV...
python tools/openalex_sync.py export
python tools/openalex_sync.py export-authors
python tools/openalex_sync.py export-works

echo.
echo [OK] Da hoan thanh toan bo Pipeline thanh cong!
echo.
pause
goto menu

:switch_db
cls
echo ==========================================
echo  0. SWITCH DATABASE
echo ==========================================
echo.
for /f "tokens=2 delims==" %%A in ('findstr /i "DATABASE_URL" .env 2^>nul') do set CURRENT_DB_VAL=%%A
echo DB hien tai: %CURRENT_DB_VAL%
echo.
echo  1. Dung ResearchPulse Remote (100.121.61.95:5432) - DB Moi
echo  2. Dung Local Docker DB (localhost:5433) - DB Cu
echo  3. Giu nguyen, quay lai menu
echo.
set /p db_choice="Chon (1/2/3): "

if "%db_choice%"=="1" (
    echo DATABASE_URL=postgresql://postgres:postgres123@100.121.61.95:5432/researchpulse> .env
    echo OLD_DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db>> .env
    echo NEW_DATABASE_URL=postgresql+psycopg2://postgres:postgres123@100.121.61.95:5432/researchpulse>> .env
    echo OPENALEX_EMAIL=phunghao2701@gmail.com>> .env
    echo OPENALEX_API_KEY=VNljwpuEXO9SBtrvOAiU1X>> .env
    echo OPENALEX_RPS=8>> .env
    echo.
    echo [OK] Da chuyen sang ResearchPulse Remote (100.121.61.95)!
    pause
    goto menu
)
if "%db_choice%"=="2" (
    echo DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db> .env
    echo OLD_DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db>> .env
    echo NEW_DATABASE_URL=postgresql+psycopg2://postgres:postgres123@100.121.61.95:5432/researchpulse>> .env
    echo OPENALEX_EMAIL=phunghao2701@gmail.com>> .env
    echo OPENALEX_API_KEY=VNljwpuEXO9SBtrvOAiU1X>> .env
    echo OPENALEX_RPS=8>> .env
    echo.
    echo [OK] Da chuyen sang Local Docker DB (localhost:5433)!
    pause
    goto menu
)
goto menu

:migrate_db
cls
echo ==========================================
echo  M. MIGRATE LOCAL -> RESEARCHPULSE
echo ==========================================
echo [INFO] Cong cu se chuyen toan bo du lieu hoc thuat tu Local Docker sang
echo        CSDL ResearchPulse moi (100.121.61.95:5432).
echo [INFO] Ho tro khop schema tu dong va reset sequence day du.
echo.
echo  1. Bat dau chay Migrate sang ResearchPulse
echo  2. Quay lai menu chinh
echo.
set /p migrate_mode="Chon (1/2): "
if not "%migrate_mode%"=="1" goto menu

echo.
echo [INFO] Dang chay tool migrate sang ResearchPulse...
python tools/migrate_to_researchpulse.py
echo.
pause
goto menu

:sync_semantic
cls
call :check_db
echo ==========================================
echo  E. ENRICH SEMANTIC SCHOLAR
echo ==========================================
echo [INFO] Ban co the tuy chon so luong bai bao hoac chay toan bo.
echo  1. Lam giau tat ca bai bao (Quet lai tu dau)
echo  2. Chi lam giau bai bao con thieu (Khuyen dung - Bo qua bai da quet)
echo.
set /p semantic_mode="Chon (1/2): "
if "%semantic_mode%"=="" set semantic_mode=2

echo.
set /p s_limit="Nhap gioi han bai viet can quet (Nhan Enter de chay 100, Nhap 0 de quet TOAN BO): "
if "%s_limit%"=="" set s_limit=100

echo.
echo [INFO] Bat dau lam giau du lieu voi gioi han: %s_limit%
if "%semantic_mode%"=="1" (
    python tools/semantic_scholar_sync.py enrich-articles --limit %s_limit%
) else (
    python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit %s_limit%
)
echo.
pause
goto menu

:backfill_references
cls
call :check_db
echo ==========================================
echo  R. BACKFILL REFERENCES
echo ==========================================
echo  1. OpenAlex only  - Cap nhat references + reference_count tu OpenAlex
echo  2. Semantic only  - Bo sung references/reference_count neu OpenAlex chua co
echo  3. Both          - Chay OpenAlex truoc, roi Semantic bo sung
echo                   - Chuan hoa DOI trong references
echo  4. Quay lai menu chinh
echo.
set /p ref_mode="Chon (1/2/3/4): "
if "%ref_mode%"=="" set ref_mode=1
if "%ref_mode%"=="4" goto menu

echo.
set /p ref_limit="Nhap gioi han bai viet can quet (Nhan Enter de chay 100, Nhap 0 de quet TOAN BO): "
if "%ref_limit%"=="" set ref_limit=100

echo.
set /p ref_min_year="Nhap nam toi thieu de uu tien bai moi (Nhan Enter de bo qua): "

echo.
if "%ref_mode%"=="1" goto backfill_openalex_only
if "%ref_mode%"=="2" goto backfill_semantic_only
if "%ref_mode%"=="3" goto backfill_both
goto backfill_references

:backfill_openalex_only
echo [INFO] Bat dau backfill references tu OpenAlex...
if "%ref_min_year%"=="" (
    python tools/openalex_reference_backfill.py --limit %ref_limit%
) else (
    python tools/openalex_reference_backfill.py --limit %ref_limit% --min-year %ref_min_year%
)
goto backfill_refs_end

:backfill_semantic_only
echo [INFO] Bat dau bo sung references tu Semantic Scholar...
python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit %ref_limit%
goto backfill_refs_end

:backfill_both
echo [INFO] Buoc 1/2: Backfill references tu OpenAlex...
if "%ref_min_year%"=="" (
    python tools/openalex_reference_backfill.py --limit %ref_limit%
) else (
    python tools/openalex_reference_backfill.py --limit %ref_limit% --min-year %ref_min_year%
)
echo.
echo [INFO] Buoc 2/2: Bo sung references tu Semantic Scholar...
python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit %ref_limit%

:backfill_refs_end
echo.
echo [INFO] Buoc cuoi: Chuan hoa DOI trong references...
python tools/merge_reference_dois.py --limit %ref_limit%
echo.
echo [OK] Backfill references hoan tat!
echo.
pause
goto menu

:sync_neo4j
cls
echo ==========================================
echo  N. SYNC POSTGRESQL -^> NEO4J
echo ==========================================
echo [INFO] Tool dung cau hinh rieng tai Tool-pg-to-neo4j\.env
echo [INFO] PostgreSQL chi duoc doc; Neo4j se duoc tao/cap nhat graph.
echo.
echo  1. Sync mau co gioi han (khuyen dung de kiem tra)
echo  2. Full sync tat ca du lieu
echo  3. Dry-run 100 ban ghi (khong ket noi database)
echo  4. Quay lai menu chinh
echo.
set "neo4j_mode="
set /p neo4j_mode="Chon (1/2/3/4): "
if "%neo4j_mode%"=="1" goto neo4j_limited
if "%neo4j_mode%"=="2" goto neo4j_full
if "%neo4j_mode%"=="3" goto neo4j_dry_run
if "%neo4j_mode%"=="4" goto menu
goto sync_neo4j

:neo4j_limited
set "neo4j_limit="
set /p neo4j_limit="Nhap gioi han moi entity (Mac dinh 100): "
if "%neo4j_limit%"=="" set "neo4j_limit=100"
set "neo4j_args=--type full --limit %neo4j_limit%"
goto run_neo4j

:neo4j_full
echo.
echo [CANH BAO] Full sync se ghi toan bo graph va dung lai cac mang quan he phai sinh.
set "neo4j_confirm="
set /p neo4j_confirm="Nhap FULL de xac nhan: "
if /I not "%neo4j_confirm%"=="FULL" (
    echo [INFO] Da huy full sync Neo4j.
    pause
    goto menu
)
set "neo4j_args=--type full --all"
goto run_neo4j

:neo4j_dry_run
set "PIPELINE_DRY_RUN=1"
set "neo4j_args=--type full --limit 100"
goto run_neo4j

:run_neo4j
if not exist "Tool-pg-to-neo4j\src\main.py" (
    echo [ERROR] Khong tim thay Tool-pg-to-neo4j\src\main.py
    if /I "%PIPELINE_DRY_RUN%"=="1" exit /b 1
    pause
    goto menu
)
if not exist "Tool-pg-to-neo4j\.env" (
    echo [ERROR] Khong tim thay Tool-pg-to-neo4j\.env
    if /I "%PIPELINE_DRY_RUN%"=="1" exit /b 1
    pause
    goto menu
)

echo.
echo [INFO] Lenh Neo4j: python src\main.py %neo4j_args%
if /I "%PIPELINE_DRY_RUN%"=="1" (
    echo [DRY-RUN] Hop le. Khong ket noi PostgreSQL/Neo4j, khong ghi du lieu.
    if /I "%PIPELINE_DRY_RUN_EXIT%"=="1" exit /b 0
    set "PIPELINE_DRY_RUN="
    pause
    goto menu
)

python -c "import neo4j, psycopg2" >nul 2>&1
if errorlevel 1 goto neo4j_dependency_missing
goto neo4j_dependencies_ok

:neo4j_dependency_missing
echo [ERROR] Thieu dependency cho tool Neo4j trong Python interpreter:
python -c "import sys; print(sys.executable)"
echo.
choice /c YN /n /m "Cai dependency vao dung interpreter nay? (Y/N): "
if errorlevel 2 (
    echo [INFO] Da huy. Co the cai thu cong bang: python -m pip install neo4j
    pause
    goto menu
)

python -m pip install "neo4j>=5.14.0,<6.0.0" "psycopg2-binary>=2.9.0" "python-dotenv>=1.0.0"
if errorlevel 1 (
    echo [ERROR] Cai dependency Neo4j that bai.
    pause
    goto menu
)

python -c "import neo4j, psycopg2" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Dependency da cai nhung van khong import duoc.
    pause
    goto menu
)

:neo4j_dependencies_ok

pushd "Tool-pg-to-neo4j"
python src\main.py %neo4j_args%
set "neo4j_exit=%errorlevel%"
popd

if not "%neo4j_exit%"=="0" (
    echo.
    echo [ERROR] Sync Neo4j that bai voi exit code %neo4j_exit%.
    pause
    goto menu
)

echo.
echo [OK] Sync PostgreSQL -^> Neo4j hoan tat.
pause
goto menu

:sync_meilisearch
cls
echo ==========================================
echo  L. SYNC POSTGRESQL -^> MEILISEARCH
echo ==========================================
echo [INFO] Tool dung cau hinh rieng tai pg-to-melisearch\.env
echo [INFO] PostgreSQL chi duoc doc; Meilisearch se duoc tao/cap nhat index.
echo [CANH BAO] Tool hien tai se huy cac task Meilisearch dang pending khi khoi dong.
echo [INFO] Sau khi chay, tool se cho chon ALL hoac LIMIT moi bang.
echo.
set "meili_confirm="
set /p meili_confirm="Nhap MEILI de tiep tuc, hoac Enter de quay lai: "
if /I not "%meili_confirm%"=="MEILI" goto menu
goto run_meilisearch

:run_meilisearch
if not exist "pg-to-melisearch\main.py" (
    echo [ERROR] Khong tim thay pg-to-melisearch\main.py
    if /I "%PIPELINE_MEILI_DRY_RUN_EXIT%"=="1" exit /b 1
    pause
    goto menu
)
if not exist "pg-to-melisearch\.env" (
    echo [ERROR] Khong tim thay pg-to-melisearch\.env
    if /I "%PIPELINE_MEILI_DRY_RUN_EXIT%"=="1" exit /b 1
    pause
    goto menu
)
if not exist "pg-to-melisearch\requirements.txt" (
    echo [ERROR] Khong tim thay pg-to-melisearch\requirements.txt
    if /I "%PIPELINE_MEILI_DRY_RUN_EXIT%"=="1" exit /b 1
    pause
    goto menu
)

echo.
echo [INFO] Lenh Meilisearch: python main.py
if /I "%PIPELINE_MEILI_DRY_RUN_EXIT%"=="1" (
    echo [DRY-RUN] Hop le. Khong ket noi PostgreSQL/Meilisearch, khong ghi du lieu.
    exit /b 0
)

python -c "import meilisearch, psycopg2, dotenv" >nul 2>&1
if errorlevel 1 goto meilisearch_dependency_missing
goto meilisearch_dependencies_ok

:meilisearch_dependency_missing
echo [ERROR] Thieu dependency cho tool Meilisearch trong Python interpreter:
python -c "import sys; print(sys.executable)"
echo.
choice /c YN /n /m "Cai dependency vao dung interpreter nay? (Y/N): "
if errorlevel 2 (
    echo [INFO] Da huy cai dependency.
    pause
    goto menu
)

python -m pip install -r "pg-to-melisearch\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Cai dependency Meilisearch that bai.
    pause
    goto menu
)

python -c "import meilisearch, psycopg2, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Dependency da cai nhung van khong import duoc.
    pause
    goto menu
)

:meilisearch_dependencies_ok
pushd "pg-to-melisearch"
python main.py
set "meilisearch_exit=%errorlevel%"
popd

if not "%meilisearch_exit%"=="0" (
    echo.
    echo [ERROR] Sync Meilisearch that bai voi exit code %meilisearch_exit%.
    pause
    goto menu
)

echo.
echo [OK] Sync PostgreSQL -^> Meilisearch hoan tat.
pause
goto menu

:embed_db
if /I "%PIPELINE_EMBED_MENU_DRY_RUN_EXIT%"=="1" (
    echo [DRY-RUN] Label embed_db hop le. Khong ket noi database.
    exit /b 0
)
cls
echo ==========================================
echo  V. EMBED ARTICLE VECTORS
echo ==========================================
echo [INFO] Tool dung cau hinh rieng tai embedding-tool\.env
echo [INFO] Chi Article co embedding IS NULL moi duoc xu ly.
echo [CANH BAO] Tool se tu choi neu DB da co vector khac so chieu.
echo.
echo  Chon dimensions:
echo  1. 768 dimensions - all-mpnet-base-v2 (Local)
echo  2. 3072 dimensions - qwen3-embedding:8b (Ollama Local)
echo  3. Quay lai menu chinh
echo.
set "embedding_dimension="
set /p embedding_dimension_choice="Chon (1/2/3): "
if "%embedding_dimension_choice%"=="1" set "embedding_dimension=768"
if "%embedding_dimension_choice%"=="2" set "embedding_dimension=3072"
if "%embedding_dimension_choice%"=="3" goto menu
if not defined embedding_dimension goto embed_db

echo.
echo  Chon so bai can xu ly:
echo  1. 100 bai (khuyen dung de test)
echo  2. 1,000 bai
echo  3. Tat ca bai con thieu embedding
echo  4. Quay lai menu chinh
echo.
set "embedding_limit="
set /p embedding_limit_choice="Chon (1/2/3/4): "
if "%embedding_limit_choice%"=="1" set "embedding_limit=100"
if "%embedding_limit_choice%"=="2" set "embedding_limit=1000"
if "%embedding_limit_choice%"=="3" set "embedding_limit=0"
if "%embedding_limit_choice%"=="4" goto menu
if not defined embedding_limit goto embed_db

echo.
echo  1. Dry-run - chi kiem tra config, khong tai model/ket noi DB
echo  2. Chay embedding that
echo  3. Quay lai menu chinh
echo.
set "embedding_run_mode="
set /p embedding_run_mode="Chon (1/2/3): "
if "%embedding_run_mode%"=="1" goto embedding_dry_run
if "%embedding_run_mode%"=="2" goto embedding_confirm
if "%embedding_run_mode%"=="3" goto menu
goto embed_db

:embedding_dry_run
set "embedding_is_dry_run=1"
set "embedding_args=--dimension %embedding_dimension% --limit %embedding_limit% --dry-run"
goto run_embedding

:embedding_confirm
echo.
echo [CANH BAO] Embedding se ghi vector vao PostgreSQL theo embedding-tool\.env.
set "embedding_confirm="
set /p embedding_confirm="Nhap EMBED de xac nhan: "
if /I not "%embedding_confirm%"=="EMBED" (
    echo [INFO] Da huy embedding.
    pause
    goto menu
)
set "embedding_is_dry_run=0"
set "embedding_args=--dimension %embedding_dimension% --limit %embedding_limit%"
goto run_embedding

:run_embedding
if not exist "embedding-tool\embed_database.py" (
    echo [ERROR] Khong tim thay embedding-tool\embed_database.py
    if /I "%PIPELINE_EMBED_DRY_RUN_EXIT%"=="1" exit /b 1
    pause
    goto menu
)
if not exist "embedding-tool\.env" (
    echo [ERROR] Khong tim thay embedding-tool\.env
    if /I "%PIPELINE_EMBED_DRY_RUN_EXIT%"=="1" exit /b 1
    pause
    goto menu
)

echo.
echo [INFO] Lenh embedding: python embed_database.py %embedding_args%
if not "%embedding_is_dry_run%"=="1" goto embedding_dependency_check
goto embedding_dependencies_ok

:embedding_dependency_check
if "%embedding_dimension%"=="3072" goto embedding_dependency_check_ollama

:embedding_dependency_check_local
python -c "import sentence_transformers, psycopg2, dotenv" >nul 2>&1
if errorlevel 1 goto embedding_dependency_missing_local
goto embedding_dependencies_ok

:embedding_dependency_check_ollama
python -c "import ollama, psycopg2, dotenv" >nul 2>&1
if errorlevel 1 goto embedding_dependency_missing_ollama
goto embedding_dependencies_ok

:embedding_dependency_missing_local
set "embedding_requirements=embedding-tool\requirements-local.txt"
goto embedding_dependency_install

:embedding_dependency_missing_ollama
set "embedding_requirements=embedding-tool\requirements-ollama.txt"
goto embedding_dependency_install

:embedding_dependency_install
echo [ERROR] Thieu dependency cho embedding %embedding_dimension% dimensions trong Python interpreter:
python -c "import sys; print(sys.executable)"
echo.
choice /c YN /n /m "Cai dependency vao dung interpreter nay? (Y/N): "
if errorlevel 2 (
    echo [INFO] Da huy cai dependency.
    pause
    goto menu
)

python -m pip install --timeout 120 --retries 10 -r "%embedding_requirements%"
if errorlevel 1 (
    echo [ERROR] Cai dependency embedding that bai.
    pause
    goto menu
)

if "%embedding_dimension%"=="3072" goto embedding_dependency_recheck_ollama

:embedding_dependency_recheck_local
python -c "import sentence_transformers, psycopg2, dotenv" >nul 2>&1
if not errorlevel 1 goto embedding_dependencies_ok
goto embedding_dependency_still_missing

:embedding_dependency_recheck_ollama
python -c "import ollama, psycopg2, dotenv" >nul 2>&1
if not errorlevel 1 goto embedding_dependencies_ok

:embedding_dependency_still_missing
echo [ERROR] Dependency da cai nhung van khong import duoc.
pause
goto menu

:embedding_dependencies_ok
pushd "embedding-tool"
python embed_database.py %embedding_args%
set "embedding_exit=%errorlevel%"
popd

if not "%embedding_exit%"=="0" (
    echo.
    echo [ERROR] Embedding that bai voi exit code %embedding_exit%.
    if /I "%PIPELINE_EMBED_DRY_RUN_EXIT%"=="1" exit /b %embedding_exit%
    pause
    goto menu
)

echo.
if "%embedding_is_dry_run%"=="1" (
    echo [OK] Dry-run embedding hop le. Khong ghi database.
) else (
    echo [OK] Embedding Article hoan tat.
)
if /I "%PIPELINE_EMBED_DRY_RUN_EXIT%"=="1" exit /b 0
pause
goto menu

:exit
echo Tam biet!
exit /b 0

:check_db
python -c "import os; from sqlalchemy import create_engine; from dotenv import load_dotenv; import pathlib; load_dotenv(pathlib.Path('.env'), override=True); url=os.getenv('DATABASE_URL',''); engine=create_engine(url, connect_args={'connect_timeout': 5}); conn=engine.connect(); conn.close()" 2>nul
if errorlevel 1 (
    cls
    echo ====================================================================
    echo [LOI] KHONG THE KET NOI DEN DATABASE POSTGRESQL!
    echo ====================================================================
    echo DB hien tai trong .env:
    findstr /i "DATABASE_URL" .env
    echo.
    echo Neu dung ResearchPulse: Kiem tra mang WireGuard hoac IP 100.121.61.95.
    echo Neu dung Local Docker: Kiem tra Docker container dang chay (Option 1).
    echo Co the dung Option 0 de doi giua cac database.
    echo ====================================================================
    pause
    goto menu
)
exit /b 0

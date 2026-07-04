@echo off
rem Change directory to the folder where this batch file is located
cd /d "%~dp0"

rem Auto-create .env file if it does not exist
if not exist .env (
    echo DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db > .env
    echo OPENALEX_EMAIL=academic-etl@example.com >> .env
    echo SEMANTIC_SCHOLAR_BASE_URL=https://api.semanticscholar.org/graph/v1 >> .env
    echo SEMANTIC_SCHOLAR_RPS=1 >> .env
    echo [INFO] Da tu dong tao file .env mac dinh vi khong tim thay!
)

chcp 65001 > nul
title Scimago and OpenAlex ETL Pipeline Control Panel
color 0B

:menu
cls

rem Hien thi DB dang active
for /f "tokens=2 delims==" %%A in ('findstr /i "DATABASE_URL" .env 2^>nul') do set CURRENT_DB=%%A
echo ====================================================================
echo             SCIMAGO AND OPENALEX ETL PIPELINE CONTROL PANEL
echo ====================================================================
echo  DB dang dung: %CURRENT_DB%
echo ====================================================================
echo  0. Switch DB: Chuyen giua Local Docker va Vercel
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
echo  M. Migrate Local -^> Vercel: Chuyen toan bo data tu Local len Vercel
echo  9. Exit: Thoat
echo ====================================================================
choice /c 1234567890ERM /n /m "Vui long chon chuc nang (1-9, 0, E, R, M): "
echo [DEBUG] Lua chon nhan duoc: errorlevel=%errorlevel%
if errorlevel 13 goto migrate_to_vercel
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

echo [INFO] Dang khoi dong PostgreSQL qua Docker Compose...
docker compose up -d --build

echo [INFO] Doi 5 giay de co so du lieu khoi dong hoan tat...
timeout /t 5 > nul

echo [INFO] Dang khoi tao cau truc bang schema.sql...
type database\schema.sql | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db

echo [INFO] Dang nap du lieu ranking metric mac dinh...
type database\seed_ranking_metric.sql | docker exec -i scientific_journal_postgres psql -U postgres -d scientific_journal_db

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
set w_limit=
set /p w_limit="Nhap gioi han bai viet can sync moi tap chi (nhan Enter de sync 20, nhap 0 de sync TOAN BO): "
if "%w_limit%"=="" set w_limit=20

echo.
echo [INFO] Bat dau dong bo Bai bao (Works, Topics, Keywords)...
python tools/openalex_sync.py sync-works --limit %w_limit%
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
echo [1/3] Dang tien hanh Import Scimago...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
if errorlevel 1 (
    echo [ERROR] Qua trinh Import gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [2/3] Dang tien hanh dong bo hoa tu OpenAlex API (Tap chi, Tac gia, Bai bao, Topic, Tu khoa)...
python tools/openalex_sync.py sync --limit 50
python tools/openalex_sync.py sync-works --limit 20
python tools/openalex_sync.py sync-authors
if errorlevel 1 (
    echo [ERROR] Qua trinh dong bo hoa OpenAlex gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [2.5/3] Dang tien hanh lam giau du lieu bai bao tu Semantic Scholar API...
python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit 20

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
echo  1. Dung LOCAL Docker (postgresql://localhost:5433)
echo  2. Dung VERCEL Prisma (db.prisma.io)
echo  3. Giu nguyen, quay lai menu
echo.
set /p db_choice="Chon (1/2/3): "

if "%db_choice%"=="1" (
    echo DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db> .env
    echo OPENALEX_EMAIL=phunghao2701@gmail.com>> .env
    echo OPENALEX_API_KEY=QMpnNu39KD8pRteBiQzGqe>> .env
    echo.
    echo [OK] Da chuyen sang LOCAL Docker DB!
    pause
    goto menu
)
if "%db_choice%"=="2" (
    echo DATABASE_URL=postgresql+psycopg2://8b22d4c854c9d742f0eaa0da80bd1208bcd3c18cfe5d2667f7c06d7f38905f81:sk__9UjMxIRIGy7X2dRACsdc@db.prisma.io:5432/postgres?sslmode=require> .env
    echo OPENALEX_EMAIL=phunghao2701@gmail.com>> .env
    echo OPENALEX_API_KEY=QMpnNu39KD8pRteBiQzGqe>> .env
    echo.
    echo [OK] Da chuyen sang VERCEL DB!
    pause
    goto menu
)
goto menu

:migrate_to_vercel
cls
echo ==========================================
echo  M. MIGRATE LOCAL -> VERCEL
echo ==========================================
echo.
echo  Chon che do dong bo:
echo.
echo  1. INCREMENTAL (Khuyen dung): Chi copy nhung gi chua co tren Vercel.
echo     - Du lieu da co tren Vercel duoc giu nguyen.
echo     - Dong bo theo schema moi tren Vercel/Supabase.
echo     - An toan, khong mat data.
echo.
echo  2. FULL RESET: Xoa TOAN BO Vercel roi copy lai tu dau.
echo     - NGUY HIEM: Toan bo data tren Vercel se bi mat!
echo     - Dung khi muon dong bo lai hoan toan tu Local.
echo.
echo  3. Quay lai menu chinh.
echo.
set /p migrate_mode="Chon (1/2/3): "
echo.

if "%migrate_mode%"=="1" (
    echo [INFO] Bat dau INCREMENTAL sync...
    echo Luu y: .env hien tai van dung LOCAL DB sau khi migrate xong.
    echo.
    python tools/migrate_local_to_vercel.py
)
if "%migrate_mode%"=="2" (
    echo [CANH BAO] FULL RESET se XOA TOAN BO du lieu tren Vercel!
    echo.
    python tools/migrate_local_to_vercel.py --reset
)
if "%migrate_mode%"=="3" goto menu
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

:exit
echo Tam biet!
exit /b 0

:check_db
python -c "import os; from sqlalchemy import create_engine; from dotenv import load_dotenv; import pathlib; load_dotenv(pathlib.Path('.env'), override=True); url=os.getenv('DATABASE_URL',''); engine=create_engine(url); conn=engine.connect(); conn.close()" 2>nul
if errorlevel 1 (
    cls
    echo ====================================================================
    echo [LOI] KHONG THE KET NOI DEN DATABASE POSTGRESQL!
    echo ====================================================================
    echo DB hien tai trong .env:
    findstr /i "DATABASE_URL" .env
    echo.
    echo Neu dung LOCAL: Kiem tra Docker Desktop da chay chua? Chay Option 1.
    echo Neu dung VERCEL: Kiem tra ket noi Internet va URL trong .env.
    echo Co the dung Option 0 de doi sang DB khac.
    echo ====================================================================
    pause
    goto menu
)
exit /b 0

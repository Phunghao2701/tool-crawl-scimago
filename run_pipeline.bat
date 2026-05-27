@echo off
rem Change directory to the folder where this batch file is located
cd /d "%~dp0"

rem Auto-create .env file if it does not exist
if not exist .env (
    echo DATABASE_URL=postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db > .env
    echo OPENALEX_EMAIL=academic-etl@example.com >> .env
    echo [INFO] Da tu dong tao file .env mac dinh vi khong tim thay!
)

chcp 65001 > nul
title Scimago and OpenAlex ETL Pipeline Control Panel
color 0B

:menu
cls
echo ====================================================================
echo             SCIMAGO AND OPENALEX ETL PIPELINE CONTROL PANEL
echo ====================================================================
echo 1. Setup Environment - DB: Cai dat dependencies, Docker, Schema, Seed
echo 2. Import Scimago Data: Import file Scimago tho tu thu muc data
echo 3. Sync OpenAlex Data: Dong bo thong tin chi tiet tu OpenAlex API
echo 4. Export Report: Xuat bao cao Excel va CSV co chua cot ISSN
echo 5. View Database Statistics: Xem so lieu thong ke trong database
echo 6. Run FULL Pipeline: Chay lien tuc tu Import den Sync den Export
echo 7. Exit: Thoat
echo ====================================================================
set /p choice="Vui long chon chuc nang (1-7): "

if "%choice%"=="1" goto setup
if "%choice%"=="2" goto import
if "%choice%"=="3" goto sync
if "%choice%"=="4" goto export
if "%choice%"=="5" goto stats
if "%choice%"=="6" goto full
if "%choice%"=="7" goto exit
goto menu

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
set /p filepath="Nhap duong dan file (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set "filepath=%filepath:"=%"
set /p year="Nhap nam du lieu (Mac dinh: 2025): "
if "%year%"=="" set year=2025

echo [INFO] Bat dau qua trinh Import du lieu...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
echo.
pause
goto menu

:sync
cls
call :check_db
echo ==========================================
echo  3. SYNC OPENALEX DATA (ALL-IN-ONE)
echo ==========================================
set /p j_limit="1. Nhap gioi han so tap chi can sync (nhan Enter de sync 50, nhap 0 de sync TOAN BO): "
if "%j_limit%"=="" set j_limit=50

set /p w_limit="2. Nhap gioi han bai viet can sync moi tap chi (nhan Enter de sync 20, nhap 0 de sync TOAN BO): "
if "%w_limit%"=="" set w_limit=20

echo.
echo [INFO] Bat dau dong bo Tap chi (Journals)...
python tools/openalex_sync.py sync --limit %j_limit%

echo.
echo [INFO] Bat dau dong bo Bai bao (Works, Topics, Keywords)...
python tools/openalex_sync.py sync-works --limit %w_limit%

echo.
echo [INFO] Bat dau dong bo Tac gia (Authors)...
python tools/openalex_sync.py sync-authors

echo.
echo [OK] Hoan thanh dong bo toan bo du lieu OpenAlex!
echo.
pause
goto menu

:export
cls
call :check_db
echo ==========================================
echo  4. EXPORT REPORT (EXCEL AND CSV)
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
echo  5. DATABASE STATISTICS
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
echo  6. RUN FULL PIPELINE (IMPORT - SYNC - EXPORT)
echo ==========================================
echo [INFO] Ban co the keo tha truc tiep file tu Windows Explorer vao cua so nay.
set /p filepath="Nhap duong dan file (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set "filepath=%filepath:"=%"
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
echo [3/3] Dang tien hanh ket xuat tat ca cac bao cao Excel va CSV...
python tools/openalex_sync.py export
python tools/openalex_sync.py export-authors
python tools/openalex_sync.py export-works

echo.
echo [OK] Da hoan thanh toan bo Pipeline thanh cong!
echo.
pause
goto menu

:exit
echo Tam biet!
exit /b 0

:check_db
python -c "import os; from sqlalchemy import create_engine; from dotenv import load_dotenv; load_dotenv(); engine=create_engine(os.getenv('DATABASE_URL')); conn=engine.connect(); conn.close()" 2>nul
if errorlevel 1 (
    cls
    echo ====================================================================
    echo [LOI] KHONG THE KET NOI DEN DATABASE POSTGRESQL!
    echo ====================================================================
    echo Vui long kiem tra cac buoc sau:
    echo  1. Ban da mo Docker Desktop hoac dich vu Docker len chua?
    echo  2. Ban da chay lua chon "1. Setup Environment - DB" de tao container chua?
    echo  3. Neu file .env cua ban bi thay doi, hay dam bao port la 5433 va password la 1234.
    echo.
    echo Vui long mo Docker Desktop va chay Option 1 truoc de thiet lap tu dong.
    echo ====================================================================
    pause
    goto menu
)
exit /b 0

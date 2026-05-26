@echo off
rem Change directory to the folder where this batch file is located
cd /d "%~dp0"

chcp 65001 > nul
title Scimago and OpenAlex ETL Pipeline Control Panel
color 0B

:menu
cls
echo ====================================================================
echo             SCIMAGO AND OPENALEX ETL PIPELINE CONTROL PANEL
echo ====================================================================
echo  1. Setup Environment - DB: Cai dat dependencies, Docker, Schema, Seed
echo  2. Import Scimago Data: Import file Scimago tho tu thu muc data
echo  3. Sync OpenAlex Data: Dong bo thong tin chi tiet tu OpenAlex API
echo  4. Export Report: Xuat bao cao Excel va CSV co chua cot ISSN
echo  5. View Database Statistics: Xem so lieu thong ke trong database
echo  6. Run FULL Pipeline: Chay lien tuc tu Import den Sync den Export
echo  7. Exit: Thoat
echo ====================================================================
echo.
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
echo ==========================================
echo  2. IMPORT SCIMAGO DATA
echo ==========================================
echo [INFO] Danh sach cac file trong thu muc data:
dir /b data\*.csv data\*.xls data\*.xlsx 2>nul
echo.
set /p filepath="Nhap duong dan file import (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set /p year="Nhap nam du lieu (Mac dinh: 2025): "
if "%year%"=="" set year=2025

echo [INFO] Bat dau qua trinh Import du lieu...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
echo.
pause
goto menu

:sync
cls
echo ==========================================
echo  3. SYNC OPENALEX DATA
echo ==========================================
set /p limit="Nhap gioi han so luong tap chi can sync (nhan Enter de sync toan bo): "
if "%limit%"=="" (
    python tools/openalex_sync.py sync
) else (
    python tools/openalex_sync.py sync --limit %limit%
)
echo.
pause
goto menu

:export
cls
echo ==========================================
echo  4. EXPORT REPORT (EXCEL AND CSV)
echo ==========================================
echo [INFO] Dang ket hop du lieu va xuat bao cao...
python tools/openalex_sync.py export
echo.
pause
goto menu

:stats
cls
echo ==========================================
echo  5. DATABASE STATISTICS
echo ==========================================
python tools/openalex_sync.py stats
echo.
pause
goto menu

:full
cls
echo ==========================================
echo  6. RUN FULL PIPELINE (IMPORT - SYNC - EXPORT)
echo ==========================================
set /p filepath="Nhap duong dan file import (Mac dinh: data/scimagojr 2025.csv): "
if "%filepath%"=="" set filepath=data/scimagojr 2025.csv
set /p year="Nhap nam du lieu (Mac dinh: 2025): "
if "%year%"=="" set year=2025

echo.
echo [1/3] Dang tien hanh Import Scimago...
python tools/scimago_etl.py import --file "%filepath%" --year %year%
if %errorlevel% neq 0 (
    echo [ERROR] Qua trinh Import gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [2/3] Dang tien hanh dong bo hoa tu OpenAlex API...
python tools/openalex_sync.py sync
if %errorlevel% neq 0 (
    echo [ERROR] Qua trinh dong bo hoa OpenAlex gap loi. Dung pipeline.
    pause
    goto menu
)

echo.
echo [3/3] Dang tien hanh ket xuat bao cao Excel va CSV...
python tools/openalex_sync.py export

echo.
echo [OK] Da hoan thanh toan bo Pipeline thanh cong!
echo.
pause
goto menu

:exit
echo Tam biet!
exit

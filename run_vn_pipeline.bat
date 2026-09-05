@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Kiem tra Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ====================================================================
    echo [LOI] Khong tim thay lenh 'python' trong PATH he thong!
    echo ====================================================================
    pause
    exit /b 1
)

chcp 65001 >nul 2>&1
title Vietnam Journals ETL Pipeline Control Panel

set "REGISTRY_JSON=data\vietnam_journals\vn_journals_registry.json"
set "OFFICIAL_DELAY=1.5"
set "OPENALEX_DELAY=0.2"
set "OPENALEX_CITING_LIMIT=50"
set "OPENALEX_REFERENCES_LIMIT=200"

rem Option 10 defaults
set "CRAWL_MAX_DEPTH=2"
set "CRAWL_MAX_WORKS=0"
set "CRAWL_CITING_LIMIT=0"
set "CRAWL_REFERENCE_LIMIT=0"
set "CRAWL_BATCH_SIZE=20"
set "CRAWL_DELAY=1.0"
set "CRAWL_MAX_RETRIES=3"
set "CRAWL_SEED_LIMIT=0"

if not exist "%REGISTRY_JSON%" (
    echo [LOI] Khong tim thay registry tap chi VN tai:
    echo %REGISTRY_JSON%
    echo.
    echo Vui long kiem tra lai thu muc data\vietnam_journals.
    pause
    exit /b 1
)

:menu
cls
set "CURRENT_DB="
for /f "tokens=1,* delims==" %%A in ('findstr /r /i "^DATABASE_URL=" .env 2^>nul') do set "CURRENT_DB=%%B"
if not defined CURRENT_DB for /f "tokens=1,* delims==" %%A in ('findstr /r /i "^LOCAL_DATABASE_URL=" .env.local 2^>nul') do set "CURRENT_DB=%%B"
if not defined CURRENT_DB for /f "tokens=1,* delims==" %%A in ('findstr /i "DATABASE_URL" .env 2^>nul') do set "CURRENT_DB=%%B"

echo ====================================================================
echo             VIETNAM JOURNALS ETL PIPELINE CONTROL PANEL
echo ====================================================================
echo  DB dang dung: %CURRENT_DB%
echo  Registry:     %REGISTRY_JSON%
echo ====================================================================
echo  1. Import Lens CSV/XLSX -^> Preview JSON (Registry matching)
echo  2. Enrich Metadata/PDF tu trang chinh thuc -^> Final JSON
echo  3. Enrich OpenAlex DOI, Tac gia, Don vi -^> OpenAlex Final JSON
echo  4. Import Full Journal vao Database (Ho tro luu lich su don vi)
echo  5. Sync thong tin Tac gia VN tu OpenAlex API (Da luong)
echo  6. Enrich Semantic Scholar Metadata cho bai bao VN
echo  7. Merge References (Ket hop OpenAlex + Semantic Scholar)
echo  8. Backfill Related DOI Articles tu bang References / Citations
echo  9. Backfill Institution cho tac gia cu (Dry-run truoc, confirm sau)
echo  10. Crawl Recursive Research Graph (Dry-run truoc, confirm sau)
echo --------------------------------------------------------------------
echo  A. Chay FULL Pipeline co ban: 1 -^> 2 -^> 3 -^> 4 -^> 5 -^> 6 -^> 7
echo  M. Migrate Data: Chuyen data sang Remote DB (Auto-resume, bo qua data cu)
echo  0. Quay lai Menu chinh
echo ====================================================================
set "vn_choice="
set /p vn_choice="Vui long chon chuc nang (1-10, A, M, 0): "

rem Trim whitespace
for /f "tokens=1" %%i in ("%vn_choice%") do set "vn_choice=%%i"

if "%vn_choice%"=="" goto check_empty
set "empty_input_count=0"

if "%vn_choice%"=="0" goto exit_vn
if /i "%vn_choice%"=="A" goto run_full_vn
if /i "%vn_choice%"=="M" goto run_migrate_vn
if "%vn_choice%"=="1" goto run_step_1
if "%vn_choice%"=="2" goto run_step_2
if "%vn_choice%"=="3" goto run_step_3
if "%vn_choice%"=="4" goto run_step_4
if "%vn_choice%"=="5" goto run_step_5
if "%vn_choice%"=="6" goto run_step_6
if "%vn_choice%"=="7" goto run_step_7
if "%vn_choice%"=="8" goto run_step_8
if "%vn_choice%"=="9" goto run_step_9
if "%vn_choice%"=="10" goto run_step_10

echo [THONG BAO] Lua chon khong hop le.
pause
goto menu

:check_empty
set /a empty_input_count+=1
if %empty_input_count% geq 5 (
    echo [THONG BAO] Khong nhan duoc lua chon, thoat.
    goto exit_vn
)
goto menu

rem =======================================================
rem SUBROUTINE: NHAN FILE LENS VA JOURNAL CODE
rem =======================================================
:get_input_file
echo.
echo [INFO] Keo tha file Lens CSV hoac XLSX vao cua so nay:
set "INPUT_FILE="
set /p INPUT_FILE="Nhap duong dan file: "
if "%INPUT_FILE%"=="" (
    echo [LOI] Duong dan file khong duoc de trong.
    pause
    goto menu
)
set "INPUT_FILE=%INPUT_FILE:"=%"
if not exist "%INPUT_FILE%" (
    echo [LOI] File khong ton tai: %INPUT_FILE%
    pause
    goto menu
)
for %%F in ("%INPUT_FILE%") do set "DEFAULT_CODE=%%~nF"
goto get_journal_code

:get_journal_code
echo.
echo Cac Journal Code mau trong registry:
echo  - Acta_Mathematica_Vietnamica
echo  - journal_of_science_and_technology_on_information_security
echo  - journal_on_information_technologies_communications
echo  - science_and_technology_development_journal
echo.
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code theo registry [Mac dinh: %DEFAULT_CODE%]: "
if "%JOURNAL_CODE%"=="" set "JOURNAL_CODE=%DEFAULT_CODE%"
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"
exit /b 0

rem =======================================================
rem BƯỚC 1: IMPORT LENS
rem =======================================================
:run_step_1
cls
echo =======================================================
echo  1. IMPORT LENS CSV/XLSX -^> PREVIEW JSON
echo =======================================================
call :get_input_file
set "PREVIEW_JSON=data\vietnam_journals\%JOURNAL_CODE%_preview.json"

echo.
echo [INFO] Bat dau import file Lens...
python tools\vn_journals\lens_excel_importer.py "%INPUT_FILE%" --journal-code "%JOURNAL_CODE%" --output "%PREVIEW_JSON%" --journal-registry "%REGISTRY_JSON%"
if errorlevel 1 (
    echo.
    echo [ERROR] Import Lens that bai.
    pause
    goto menu
)
echo.
echo [OK] Import Lens thanh cong! File luu tai: %PREVIEW_JSON%
pause
goto menu

rem =======================================================
rem BƯỚC 2: ENRICH OFFICIAL METADATA
rem =======================================================
:run_step_2
cls
echo =======================================================
echo  2. ENRICH OFFICIAL METADATA VA PDF
echo =======================================================
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code can enrich: "
if "%JOURNAL_CODE%"=="" goto menu
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"

set "PREVIEW_JSON=data\vietnam_journals\%JOURNAL_CODE%_preview.json"
set "OFFICIAL_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_final.json"

if not exist "%PREVIEW_JSON%" (
    echo [LOI] Khong tim thay file preview: %PREVIEW_JSON%
    echo Vui long chay Buoc 1 truoc.
    pause
    goto menu
)

if not exist "data\vietnam_journals\final" mkdir "data\vietnam_journals\final"

echo.
echo [INFO] Bat dau enrich tu website chinh thuc cua tap chi...
python tools\vn_journals\enrich_lens_preview.py "%PREVIEW_JSON%" "%OFFICIAL_FINAL_JSON%" --delay %OFFICIAL_DELAY%
if errorlevel 1 (
    echo.
    echo [ERROR] Enrich website chinh thuc that bai.
    pause
    goto menu
)
echo.
echo [OK] Enrich metadata hoan tat! File luu tai: %OFFICIAL_FINAL_JSON%
pause
goto menu

rem =======================================================
rem BƯỚC 3: ENRICH OPENALEX DOI
rem =======================================================
:run_step_3
cls
echo =======================================================
echo  3. ENRICH OPENALEX DOI, AUTHORS, INSTITUTIONS
echo =======================================================
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code can enrich OpenAlex: "
if "%JOURNAL_CODE%"=="" goto menu
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"

set "PREVIEW_JSON=data\vietnam_journals\%JOURNAL_CODE%_preview.json"
set "OFFICIAL_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_final.json"
set "OPENALEX_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_openalex_final.json"

set "OPENALEX_INPUT=%OFFICIAL_FINAL_JSON%"
if not exist "%OFFICIAL_FINAL_JSON%" (
    if exist "%PREVIEW_JSON%" (
        echo [INFO] Final JSON khong ton tai. Dung tam Preview JSON.
        set "OPENALEX_INPUT=%PREVIEW_JSON%"
    ) else (
        echo [LOI] Khong tim thay file JSON dau vao cho Buoc 3.
        pause
        goto menu
    )
)

if not exist "data\vietnam_journals\final" mkdir "data\vietnam_journals\final"

echo.
echo [INFO] Bat dau enrich OpenAlex...
python tools\vn_journals\enrich_vn_openalex.py "%OPENALEX_INPUT%" "%OPENALEX_FINAL_JSON%" --delay %OPENALEX_DELAY% --citing-limit %OPENALEX_CITING_LIMIT% --references-limit %OPENALEX_REFERENCES_LIMIT%
if errorlevel 1 (
    echo.
    echo [ERROR] Enrich OpenAlex that bai.
    pause
    goto menu
)
echo.
echo [OK] Enrich OpenAlex hoan tat! File luu tai: %OPENALEX_FINAL_JSON%
pause
goto menu

rem =======================================================
rem BƯỚC 4: IMPORT FULL JOURNAL TO DATABASE
rem =======================================================
:run_step_4
cls
echo =======================================================
echo  4. IMPORT FULL JOURNAL TO DATABASE
echo =======================================================
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code can import vao database: "
if "%JOURNAL_CODE%"=="" goto menu
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"

set "OPENALEX_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_openalex_final.json"
if not exist "%OPENALEX_FINAL_JSON%" (
    echo [LOI] Khong tim thay file: %OPENALEX_FINAL_JSON%
    echo Vui long chay Buoc 3 truoc.
    pause
    goto menu
)

set "SUPABASE_LIMIT="
set /p SUPABASE_LIMIT="Nhap gioi han import (Nhan Enter de import tat ca): "

set "PREVIEW_LIMIT_ARG="
set "IMPORT_LIMIT_ARG="
if not "%SUPABASE_LIMIT%"=="" (
    set "PREVIEW_LIMIT_ARG=--limit %SUPABASE_LIMIT%"
    set "IMPORT_LIMIT_ARG=--limit %SUPABASE_LIMIT%"
)

echo.
echo [INFO] Dang chay preview kiem tra du lieu truoc khi import...
python tools\vn_journals\preview_full_journal_import_supabase.py --json-file "%OPENALEX_FINAL_JSON%" --examples 3 %PREVIEW_LIMIT_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Preview that bai.
    pause
    goto menu
)

echo.
set "CONFIRM_IMPORT="
set /p CONFIRM_IMPORT="Xac nhan thuc hien import that vao Database? (Y/N): "
if /i not "%CONFIRM_IMPORT%"=="Y" (
    echo [INFO] Da huy import vao database.
    pause
    goto menu
)

echo.
echo [INFO] Dang import vao database...
python tools\vn_journals\import_full_journal_supabase.py --json-file "%OPENALEX_FINAL_JSON%" %IMPORT_LIMIT_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Import vao database that bai.
    pause
    goto menu
)
echo.
echo [OK] Import Full Journal vao database hoan tat thanh cong!
pause
goto menu

rem =======================================================
rem BƯỚC 5: SYNC VN AUTHORS
rem =======================================================
:run_step_5
cls
echo =======================================================
echo  5. SYNC THONG TIN TAC GIA VN TU OPENALEX
echo =======================================================
set "AUTHORS_LIMIT="
set /p AUTHORS_LIMIT="Nhap gioi han tac gia can sync (Nhan Enter de sync tat ca): "

set "AUTHORS_ARG="
if not "%AUTHORS_LIMIT%"=="" set "AUTHORS_ARG=--limit %AUTHORS_LIMIT%"

echo.
echo [INFO] Bat dau sync thong tin tac gia VN tu OpenAlex...
python tools\vn_journals\sync_vn_authors.py %AUTHORS_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Sync tac gia VN that bai.
    pause
    goto menu
)
echo.
echo [OK] Sync tac gia VN hoan tat!
pause
goto menu

rem =======================================================
rem BƯỚC 6: ENRICH SEMANTIC SCHOLAR
rem =======================================================
:run_step_6
cls
echo =======================================================
echo  6. ENRICH SEMANTIC SCHOLAR METADATA
echo =======================================================
set "SEMANTIC_LIMIT="
set /p SEMANTIC_LIMIT="Nhap gioi han bai bao can enrich (Nhan Enter de chay 100): "
if "%SEMANTIC_LIMIT%"=="" set "SEMANTIC_LIMIT=100"

echo.
echo [INFO] Bat dau enrich Semantic Scholar...
python tools\semantic_scholar_sync.py enrich-articles --only-missing --limit %SEMANTIC_LIMIT%
if errorlevel 1 (
    echo.
    echo [ERROR] Enrich Semantic Scholar that bai.
    pause
    goto menu
)
echo.
echo [OK] Enrich Semantic Scholar hoan tat!
pause
goto menu

rem =======================================================
rem BƯỚC 7: MERGE REFERENCES
rem =======================================================
:run_step_7
cls
echo =======================================================
echo  7. MERGE REFERENCES (OPENALEX + SEMANTIC SCHOLAR)
echo =======================================================
set "MERGE_LIMIT="
set /p MERGE_LIMIT="Nhap gioi han bai bao can merge references (Nhan Enter de chay 100): "
if "%MERGE_LIMIT%"=="" set "MERGE_LIMIT=100"

echo.
echo [INFO] Bat dau merge va chuan hoa references...
python tools\merge_reference_dois.py --limit %MERGE_LIMIT%
if errorlevel 1 (
    echo.
    echo [ERROR] Merge references that bai.
    pause
    goto menu
)
echo.
echo [OK] Merge references hoan tat!
pause
goto menu

rem =======================================================
rem BƯỚC 8: BACKFILL RELATED DOI
rem =======================================================
:run_step_8
cls
echo =======================================================
echo  8. BACKFILL RELATED DOI ARTICLES
echo =======================================================
set "RELATED_LIMIT="
set /p RELATED_LIMIT="Nhap gioi han related DOI (Nhan Enter de quet tat ca): "

set "RELATED_ARG="
if not "%RELATED_LIMIT%"=="" set "RELATED_ARG=--related-limit %RELATED_LIMIT%"

echo.
echo [INFO] Bat dau backfill related DOIs...
python tools\vn_journals\import_full_journal_supabase.py --backfill-related-dois %RELATED_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Backfill related DOIs that bai.
    pause
    goto menu
)
echo.
echo [OK] Backfill related DOIs hoan tat!
pause
goto menu

rem =======================================================
rem BƯỚC 9: LEGACY AFFILIATION BACKFILL
rem =======================================================
:run_step_9
cls
echo =======================================================
echo  9. LEGACY AFFILIATION BACKFILL
echo =======================================================
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code can backfill don vi: "
if "%JOURNAL_CODE%"=="" goto menu
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"

set "AFF_LIMIT="
set /p AFF_LIMIT="Nhap gioi han (Nhan Enter de quet tat ca): "

set "AFF_ARG="
if not "%AFF_LIMIT%"=="" set "AFF_ARG=--limit %AFF_LIMIT%"

set "AFF_CHECKPOINT=scratch\papervn_affiliation_checkpoint_%JOURNAL_CODE%.json"
set "AFF_REPORT=scratch\papervn_affiliation_report_%JOURNAL_CODE%.jsonl"

echo.
echo [INFO] Dang chay Dry-run kiem tra (khong ghi database)...
python tools\vn_journals\backfill_institutions.py --journal-code "%JOURNAL_CODE%" --dry-run --only-missing --batch-size 100 --checkpoint "%AFF_CHECKPOINT%" --report "%AFF_REPORT%" %AFF_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Dry-run that bai.
    pause
    goto menu
)

echo.
set "CONFIRM_AFF="
set /p CONFIRM_AFF="Xac nhan chay GHI THAT vao Database? (Y/N): "
if /i not "%CONFIRM_AFF%"=="Y" (
    echo [INFO] Da huy backfill thuc te.
    pause
    goto menu
)

echo.
echo [INFO] Dang ghi thuc te vao database...
python tools\vn_journals\backfill_institutions.py --journal-code "%JOURNAL_CODE%" --only-missing --batch-size 100 --checkpoint "%AFF_CHECKPOINT%" --report "%AFF_REPORT%" %AFF_ARG%
if errorlevel 1 (
    echo.
    echo [ERROR] Ghi thuc te that bai.
    pause
    goto menu
)
echo.
echo [OK] Backfill don vi cong tac hoan tat!
pause
goto menu

rem =======================================================
rem BƯỚC 10: RECURSIVE RESEARCH GRAPH CRAWLER
rem =======================================================
:run_step_10
cls
echo =======================================================
echo  10. RECURSIVE RESEARCH GRAPH CRAWLER
echo =======================================================
set "JOURNAL_CODE="
set /p JOURNAL_CODE="Nhap journal_code lam hat giong (seed): "
if "%JOURNAL_CODE%"=="" goto menu
set "JOURNAL_CODE=%JOURNAL_CODE: =_%"

echo.
echo [INFO] Dang khoi dong phan tich seeds o che do Dry-run...
python tools\vn_journals\crawl_recursive_graph.py --journal-code "%JOURNAL_CODE%" --seed-limit %CRAWL_SEED_LIMIT% --max-depth %CRAWL_MAX_DEPTH% --max-works %CRAWL_MAX_WORKS% --citing-limit %CRAWL_CITING_LIMIT% --reference-limit %CRAWL_REFERENCE_LIMIT% --batch-size %CRAWL_BATCH_SIZE% --delay %CRAWL_DELAY% --max-retries %CRAWL_MAX_RETRIES% --dry-run
if errorlevel 1 (
    echo.
    echo [ERROR] Dry-run crawler that bai.
    pause
    goto menu
)

echo.
echo [CANH BAO] Chay crawl graph de quy se tao luong request lon.
set "CONFIRM_CRAWL="
set /p CONFIRM_CRAWL="Nhap 'CRAWL' (viet hoa) de bat dau chay that: "
if not "%CONFIRM_CRAWL%"=="CRAWL" (
    echo [INFO] Da huy chay crawl thuc te.
    pause
    goto menu
)

echo.
echo [INFO] Bat dau crawl thuc te...
python tools\vn_journals\crawl_recursive_graph.py --journal-code "%JOURNAL_CODE%" --seed-limit %CRAWL_SEED_LIMIT% --max-depth %CRAWL_MAX_DEPTH% --max-works %CRAWL_MAX_WORKS% --citing-limit %CRAWL_CITING_LIMIT% --reference-limit %CRAWL_REFERENCE_LIMIT% --batch-size %CRAWL_BATCH_SIZE% --delay %CRAWL_DELAY% --max-retries %CRAWL_MAX_RETRIES%
if errorlevel 1 (
    echo.
    echo [ERROR] Crawl thuc te that bai.
    pause
    goto menu
)
echo.
echo [OK] Crawl recursive graph hoan tat!
pause
goto menu

rem =======================================================
rem FULL PIPELINE (1 -> 7)
rem =======================================================
:run_full_vn
cls
echo =======================================================
echo  A. CHAY FULL VIETNAM JOURNALS PIPELINE (1 -^> 7)
echo =======================================================
call :get_input_file
set "PREVIEW_JSON=data\vietnam_journals\%JOURNAL_CODE%_preview.json"
set "OFFICIAL_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_final.json"
set "OPENALEX_FINAL_JSON=data\vietnam_journals\final\%JOURNAL_CODE%_openalex_final.json"

if not exist "data\vietnam_journals\final" mkdir "data\vietnam_journals\final"

echo.
echo [1/5] Import Lens file...
python tools\vn_journals\lens_excel_importer.py "%INPUT_FILE%" --journal-code "%JOURNAL_CODE%" --output "%PREVIEW_JSON%" --journal-registry "%REGISTRY_JSON%"
if errorlevel 1 goto full_failed

echo.
echo [2/5] Enrich Official Website...
python tools\vn_journals\enrich_lens_preview.py "%PREVIEW_JSON%" "%OFFICIAL_FINAL_JSON%" --delay %OFFICIAL_DELAY%
if errorlevel 1 goto full_failed

echo.
echo [3/5] Enrich OpenAlex DOI...
python tools\vn_journals\enrich_vn_openalex.py "%OFFICIAL_FINAL_JSON%" "%OPENALEX_FINAL_JSON%" --delay %OPENALEX_DELAY%
if errorlevel 1 goto full_failed

echo.
echo [4/5] Import vao Database...
python tools\vn_journals\import_full_journal_supabase.py --json-file "%OPENALEX_FINAL_JSON%"
if errorlevel 1 goto full_failed

echo.
echo [5/5] Sync Authors va Semantic Scholar...
python tools\vn_journals\sync_vn_authors.py --limit 50
python tools\semantic_scholar_sync.py enrich-articles --only-missing --limit 50
python tools\merge_reference_dois.py --limit 50

echo.
echo =======================================================
echo [OK] TOAN BO FULL PIPELINE VN DA HOAN TAT THANH CONG!
echo =======================================================
pause
goto menu

:full_failed
echo.
echo [ERROR] FULL Pipeline bi loi va da dung lai.
pause
goto menu

:run_migrate_vn
cls
echo =======================================================
echo  M. MIGRATE VIETNAM JOURNALS DATA (LOCAL -^> REMOTE)
echo =======================================================
echo [INFO] Su dung Migration Middleware thong nhat.
echo [INFO] Tu dong resume tai diem dung va bo qua data da co.
echo.
echo  1. Migrate nhanh VN (--branch vn)
echo  2. Migrate tat ca cac bang (--branch all)
echo  0. Quay lai menu
echo.
set "mig_choice="
set /p mig_choice="Chon (1/2/0): "
if "%mig_choice%"=="1" (
    python tools\migration_middleware.py --branch vn
    pause
    goto menu
)
if "%mig_choice%"=="2" (
    python tools\migration_middleware.py --branch all
    pause
    goto menu
)
goto menu

:exit_vn
exit /b 0

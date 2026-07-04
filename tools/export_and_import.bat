@echo off
chcp 65001 >nul
echo ==============================================
echo   EXPORT Local DB -^> Import Supabase
echo ==============================================
echo.

:: Config
set CONTAINER=scientific_journal_postgres
set LOCAL_DB=scientific_journal_db
set LOCAL_USER=postgres
set SUPABASE_URL=postgresql://postgres.egyrzaqtmxmcezxchfrl:TeamSWP3912006@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require

:: Tao thu muc data neu chua co
if not exist "%~dp0..\data" mkdir "%~dp0..\data"
set DUMP_FILE=%~dp0..\data\local_dump_data.sql

echo [1/3] Truncating Supabase data (keep schema)...
python "%~dp0..\scratch\truncate_supabase.py"
if errorlevel 1 (
    echo [ERROR] Truncate failed or cancelled.
    goto :end
)

echo.
echo [2/3] Exporting data from Local Docker (data-only, no triggers)...
echo   Output: %DUMP_FILE%

:: Xuat ra file: bo --disable-triggers (can superuser), dung --no-acl --no-owner
docker exec %CONTAINER% pg_dump -U %LOCAL_USER% -d %LOCAL_DB% ^
    --data-only ^
    --no-acl ^
    --no-owner ^
    --no-tablespaces ^
    -f /tmp/dump_data.sql

if errorlevel 1 (
    echo [ERROR] pg_dump failed.
    goto :end
)

docker cp %CONTAINER%:/tmp/dump_data.sql "%DUMP_FILE%"
echo   [OK] Export done: %DUMP_FILE%

echo.
echo [3/3] Importing into Supabase...
echo   (This may take 3-10 minutes depending on data size...)
echo.

:: Chay import: dung session_replication_role de bypass FK checks
docker exec -i %CONTAINER% psql "%SUPABASE_URL%" ^
    -c "SET session_replication_role = replica;" ^
    -f /tmp/dump_data.sql ^
    -c "SET session_replication_role = DEFAULT;"

echo.
echo   [OK] Import done!

echo.
echo [verify] Checking row counts on Supabase...
docker exec -i %CONTAINER% psql "%SUPABASE_URL%" -c ^
    "SELECT table_name, (xpath('/row/cnt/text()', xml_count))[1]::text::int AS rows FROM (SELECT table_name, query_to_xml(format('SELECT count(*) as cnt FROM public.%%I', table_name), false, true, '') AS xml_count FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name) t;"

echo.
echo ==============================================
echo   DONE! 
echo ==============================================

:end
pause

@echo off
rem $queryStats via mongosh (no python). DB user needs queryStatsRead (clusterMonitor has it). mongosh prompts for the password.
rem Hosts file: first column host:port. Skips nodes that already have shapes; set MDIAG_REFRESH=1 to force.
setlocal
if not defined MDIAG_DATE (echo set MDIAG_DATE=YYYY-MM-DD & exit /b 1)
if not defined MDIAG_USER (echo set MDIAG_USER & exit /b 1)
if not defined MDIAG_OUT set "MDIAG_OUT=observability-manual"
if not defined HOSTS_FILE set "HOSTS_FILE=hosts.txt"
if not defined MDIAG_AUTH_DB set "MDIAG_AUTH_DB=admin"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
exit /b 0
:one
set "S=%~dp0query_stats.js"
for /f "tokens=1,2 delims=:" %%h in ("%~1") do (set "H=%%h" & set "P=%%i")
set "D=%~3" & if "%~3"=="" (set "D=%H%" & if "%~2"=="SHARD_MONGOS" set "D=%H%_%P%")
set "DIR=%MDIAG_OUT%\%MDIAG_DATE%\%D%"
set "OUT=%DIR%\queryStats\query-stats.json"
if not defined MDIAG_REFRESH findstr /m /c:"queryShapeHash" "%OUT%" >nul 2>&1 && (echo skip %OUT% & exit /b 0)
mongosh "mongodb://%~1/?directConnection=true" -u "%MDIAG_USER%" --authenticationDatabase "%MDIAG_AUTH_DB%" %MDIAG_MONGOSH_OPTS% --quiet --eval "var MDIAG_HOST='%H%', MDIAG_DIR='%DIR:\=/%'; load('%S:\=/%')" || echo !! %~1 failed
exit /b 0

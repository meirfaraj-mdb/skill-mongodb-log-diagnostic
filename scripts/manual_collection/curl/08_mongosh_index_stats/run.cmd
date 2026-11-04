@echo off
rem $indexStats + index definitions via mongosh (no python), each data-bearing mongod. Password is prompted.
rem DB user needs listDatabases, listCollections, listIndexes, indexStats (clusterMonitor has them).
setlocal
if not defined MDIAG_DATE (echo set MDIAG_DATE=YYYY-MM-DD & exit /b 1)
if not defined MDIAG_USER (echo set MDIAG_USER & exit /b 1)
if not defined MDIAG_OUT set "MDIAG_OUT=observability-manual"
if not defined HOSTS_FILE set "HOSTS_FILE=hosts.txt"
if not defined MDIAG_AUTH_DB set "MDIAG_AUTH_DB=admin"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
exit /b 0
:one
set "S=%~dp0index_stats.js"
if "%~2"=="SHARD_MONGOS" exit /b 0
set "T=x%~2"
if "%T:~0,13%"=="xSHARD_CONFIG" exit /b 0
for /f "tokens=1 delims=:" %%h in ("%~1") do set "H=%%h"
set "D=%~3" & if "%~3"=="" set "D=%H%"
set "DIR=%MDIAG_OUT%\%MDIAG_DATE%\%D%"
mongosh "mongodb://%~1/?directConnection=true&readPreference=secondaryPreferred" -u "%MDIAG_USER%" --authenticationDatabase "%MDIAG_AUTH_DB%" %MDIAG_MONGOSH_OPTS% --quiet --eval "var MDIAG_HOST='%H%', MDIAG_DIR='%DIR:\=/%'; load('%S:\=/%')" || echo !! %~1 failed
exit /b 0

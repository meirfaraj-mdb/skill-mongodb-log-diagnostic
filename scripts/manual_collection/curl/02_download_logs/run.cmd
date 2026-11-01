@echo off
rem Downloads mongod (or mongos) logs per host. Role: Project Data Access Read Only (or Project Cluster Log Viewer).
setlocal
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
set "Q=" & if defined MDIAG_START_S set "Q=?startDate=%MDIAG_START_S%&endDate=%MDIAG_END_S%"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
for /f "tokens=1,2 delims=:" %%h in ("%~1") do (set "H=%%h" & set "P=%%i")
set "D=%~3" & if "%~3"=="" (set "D=%H%" & if "%~2"=="SHARD_MONGOS" set "D=%H%_%P%")
set "LOG=mongodb" & if "%~2"=="SHARD_MONGOS" set "LOG=mongos"
set "OUT=%DAY%\%D%\mongodb\%LOG%.gz"
if exist "%OUT%" (echo skip %OUT% ^(exists^) & exit /b 0)
call "%C%" api "%OUT%" "/groups/%GROUP%/clusters/%H%/logs/%LOG%.gz%Q%" "application/vnd.atlas.2023-02-01+gzip" || move /y "%OUT%" "%OUT%.error.json" >nul
exit /b 0

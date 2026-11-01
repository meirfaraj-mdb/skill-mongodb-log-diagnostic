@echo off
rem Performance Advisor suggested indexes per host. Role: Project Read Only (+ Project Data Access Read Only for unredacted values).
setlocal
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
set "Q="
if defined MDIAG_START_S set /a "DUR=(%MDIAG_END_S%-%MDIAG_START_S%)*1000"
if defined MDIAG_START_S set "Q=?since=%MDIAG_START_S%000&duration=%DUR%"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
if "%~2"=="SHARD_MONGOS" exit /b 0
for /f "tokens=1,2 delims=:" %%h in ("%~1") do (set "H=%%h" & set "P=%%i")
set "D=%~3" & if "%~3"=="" set "D=%H%"
call "%C%" api "%TMPF%" "/groups/%GROUP%/processes/%~1/performanceAdvisor/suggestedIndexes%Q%" || exit /b 0
> "%TMPF%.data" (echo [& type "%TMPF%"& echo ])
set EXTRA="processId":"%~1",
call "%C%" envelope "%DAY%\%D%\advisor\advisor.json" atlas_api_advisor %H% window "%TMPF%.data"
exit /b 0

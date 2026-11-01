@echo off
rem Ops Manager Performance Advisor suggested indexes. Hosts lines: "hostId hostname:port [host_dir]".
rem Role: Project Read Only (+ Project Data Access Read Only for field values). No query-stats API in Ops Manager.
setlocal
set "MDIAG_TARGET=opsmanager"
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
set "Q="
if defined MDIAG_START_S set /a "DUR=(%MDIAG_END_S%-%MDIAG_START_S%)*1000"
if defined MDIAG_START_S set "Q=?since=%MDIAG_START_S%000&duration=%DUR%"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
for /f "tokens=1 delims=:" %%h in ("%~2") do set "H=%%h"
set "D=%~3" & if "%~3"=="" set "D=%H%"
call "%C%" api "%TMPF%" "/groups/%GROUP%/hosts/%~1/performanceAdvisor/suggestedIndexes%Q%" || exit /b 0
> "%TMPF%.data" (echo [& type "%TMPF%"& echo ])
set EXTRA="hostId":"%~1",
call "%C%" envelope "%DAY%\%D%\advisor\advisor.json" ops_manager_advisor %H% window "%TMPF%.data"
exit /b 0

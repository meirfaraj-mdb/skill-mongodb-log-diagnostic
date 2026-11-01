@echo off
rem Lists the project's processes. Role: Project Read Only. Copy each "id" (hostname:port) and "typeName" into hosts.txt.
setlocal
call "%~dp0..\_common.cmd" || exit /b 1
set "OUT=%DAY%\cluster\manifests\processes.json"
call "%~dp0..\_common.cmd" api "%OUT%" "/groups/%GROUP%/processes?itemsPerPage=500"
set RC=%ERRORLEVEL%
call "%~dp0..\_common.cmd" cleanup
echo Open %OUT%: for each process copy "id" and "typeName" into %HOSTS_FILE%  (see hosts.example.txt)
exit /b %RC%

@echo off
rem Ops Manager host measurements + one disk partition. Hosts lines: "hostId hostname:port [host_dir]". Role: Project Read Only.
rem "run.cmd --list" saves GET /groups/{G}/hosts to cluster\manifests\om-hosts.json to find the hostIds.
setlocal
set "MDIAG_TARGET=opsmanager"
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
if not defined MDIAG_GRANULARITY set "MDIAG_GRANULARITY=PT5M"
if not defined MDIAG_PARTITION set "MDIAG_PARTITION=data"
if "%~1"=="--list" (call "%C%" api "%DAY%\cluster\manifests\om-hosts.json" "/groups/%GROUP%/hosts?itemsPerPage=500" & call "%C%" cleanup & exit /b 0)
set "W=granularity=%MDIAG_GRANULARITY%&period=P1D"
if defined MDIAG_START_ISO set "W=granularity=%MDIAG_GRANULARITY%&start=%MDIAG_START_ISO%&end=%MDIAG_END_ISO%"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
for /f "tokens=1 delims=:" %%h in ("%~2") do set "H=%%h"
set "D=%~3" & if "%~3"=="" set "D=%H%"
call "%C%" api "%TMPF%.p" "/groups/%GROUP%/hosts/%~1/measurements?%W%" || exit /b 0
call "%C%" api "%TMPF%.d" "/groups/%GROUP%/hosts/%~1/disks/%MDIAG_PARTITION%/measurements?%W%" || (> "%TMPF%.d" echo {})
set "OUT=%DAY%\%D%\metrics\metrics.json"
if not exist "%DAY%\%D%\metrics" mkdir "%DAY%\%D%\metrics"
> "%OUT%" (
  echo {"schema_version":2,"source":"ops_manager_metrics","host":"%H%","temporal_basis":"window","collector":"curl-sample","hostId":"%~1","disk_measurements":[{"partitionName":"%MDIAG_PARTITION%","data":[
  type "%TMPF%.d"
  echo ]}],"data":[
  type "%TMPF%.p"
  echo ]}
)
echo   wrote %OUT%
exit /b 0

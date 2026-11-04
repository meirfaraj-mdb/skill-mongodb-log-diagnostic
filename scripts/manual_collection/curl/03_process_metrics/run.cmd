@echo off
rem Process measurements + one disk partition per host -> <host_dir>\metrics\metrics.json. Role: Project Read Only.
setlocal
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
if not defined MDIAG_GRANULARITY set "MDIAG_GRANULARITY=PT5M"
if not defined MDIAG_PARTITION set "MDIAG_PARTITION=data"
set "W=granularity=%MDIAG_GRANULARITY%&period=P1D"
if defined MDIAG_START_ISO set "W=granularity=%MDIAG_GRANULARITY%&start=%MDIAG_START_ISO%&end=%MDIAG_END_ISO%"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
for /f "tokens=1,2 delims=:" %%h in ("%~1") do (set "H=%%h" & set "P=%%i")
set "D=%~3" & if "%~3"=="" (set "D=%H%" & if "%~2"=="SHARD_MONGOS" set "D=%H%_%P%")
call "%C%" api "%TMPF%.p" "/groups/%GROUP%/processes/%~1/measurements?%W%" || exit /b 0
call "%C%" api "%TMPF%.d" "/groups/%GROUP%/processes/%~1/disks/%MDIAG_PARTITION%/measurements?%W%" || (> "%TMPF%.d" echo {})
set "OUT=%DAY%\%D%\metrics\metrics.json"
if not exist "%DAY%\%D%\metrics" mkdir "%DAY%\%D%\metrics"
> "%OUT%" (
  echo {"schema_version":2,"source":"atlas_api_metrics","host":"%H%","temporal_basis":"window","collector":"curl-sample","processId":"%~1","disk_measurements":[{"partitionName":"%MDIAG_PARTITION%","data":[
  type "%TMPF%.d"
  echo ]}],"data":[
  type "%TMPF%.p"
  echo ]}
)
echo   wrote %OUT%
exit /b 0

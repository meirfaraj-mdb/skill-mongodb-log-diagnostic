@echo off
rem Query Shape Insights: per host, then one cluster-wide call copied to each empty/failed node (scope=cluster).
rem Skips nodes whose query-stats.json already has shapes (set MDIAG_REFRESH=1 to force). Needs ATLAS_CLUSTER.
rem Role: Project Observability Viewer or Project Data Access Read Only (or higher). M10+, MongoDB 8.0+.
setlocal
set "NOTE=Cluster-wide Query Shape Insights copied to this node because the per-host request returned no data. Summaries do not identify the process that ran them: do not attribute these shapes to this node."
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
if not defined ATLAS_CLUSTER (echo set ATLAS_CLUSTER & exit /b 1)
set "B=/groups/%GROUP%/clusters/%ATLAS_CLUSTER%/queryShapeInsights/summaries?nSummaries=100"
if defined MDIAG_START_S set "B=%B%&since=%MDIAG_START_S%000&until=%MDIAG_END_S%000"
for /f "usebackq eol=# tokens=1-3" %%a in ("%HOSTS_FILE%") do call :one %%a %%b %%c
call "%C%" cleanup & exit /b 0
:one
set "T=x%~2"
if "%T:~0,13%"=="xSHARD_CONFIG" exit /b 0
for /f "tokens=1,2 delims=:" %%h in ("%~1") do (set "H=%%h" & set "P=%%i")
set "D=%~3" & if "%~3"=="" (set "D=%H%" & if "%~2"=="SHARD_MONGOS" set "D=%H%_%P%")
set "OUT=%DAY%\%D%\queryStats\query-stats.json"
if not defined MDIAG_REFRESH findstr /m /c:"queryShapeHash" "%OUT%" >nul 2>&1 && (echo skip %OUT% & exit /b 0)
rem 1) per host
call "%C%" api "%TMPF%" "%B%&processIds=%~1" && findstr /m /c:"queryShapeHash" "%TMPF%" >nul && goto :own
set "R=no_reads_on_this_process"
findstr /m /c:"summaries" "%TMPF%" >nul 2>&1 || set "R=per_process_request_failed"
if "%R%"=="no_reads_on_this_process" if "%T:~0,7%"=="xSHARD_" if not "%~2"=="SHARD_MONGOS" set "R=collected_on_mongos_only"
rem 2) one cluster-wide call per run, stored once under cluster\queryStats
if exist "%TMPF%.cluster" goto :copy
call "%C%" api "%TMPF%.cluster" "%B%" || (del /q "%TMPF%.cluster" 2>nul & exit /b 0)
set EXTRA="scope":"cluster",
call "%C%" envelope "%DAY%\cluster\queryStats\query-stats.json" atlas_query_shape_insights "" log_day_window "%TMPF%.cluster"
:copy
findstr /m /c:"queryShapeHash" "%TMPF%.cluster" >nul || set "R=no_shapes_in_cluster"
if not "%R%"=="no_shapes_in_cluster" goto :split
set EXTRA="processId":"%~1","scope":"process","diagnostics":{"reason":"%R%"},
goto :write
:split
set EXTRA="processId":null,"requestedProcessId":"%~1","scope":"cluster","attribution_note":"%NOTE%","diagnostics":{"reason":"%R%","fallback":"cluster_wide_copy"},
rem 3) copy the cluster-wide list into this node file, marked as not node-attributed
:write
call "%C%" envelope "%OUT%" atlas_query_shape_insights %H% log_day_window "%TMPF%.cluster"
exit /b 0
:own
set EXTRA="processId":"%~1","scope":"process",
call "%C%" envelope "%OUT%" atlas_query_shape_insights %H% log_day_window "%TMPF%"
exit /b 0

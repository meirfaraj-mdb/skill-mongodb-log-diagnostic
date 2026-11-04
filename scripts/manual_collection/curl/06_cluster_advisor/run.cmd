@echo off
rem Cluster-level Performance Advisor: drop-index suggestions + schema advice. Role: Project Read Only. Needs ATLAS_CLUSTER.
setlocal
set "C=%~dp0..\_common.cmd"
call "%C%" || exit /b 1
if not defined ATLAS_CLUSTER (echo set ATLAS_CLUSTER & exit /b 1)
set "A=/groups/%GROUP%/clusters/%ATLAS_CLUSTER%/performanceAdvisor"
call "%C%" api "%TMPF%.d" "%A%/dropIndexSuggestions" || (> "%TMPF%.d" echo null)
call "%C%" api "%TMPF%.s" "%A%/schemaAdvice" || (> "%TMPF%.s" echo null)
> "%TMPF%.data" (echo {"drop_index_suggestions":& type "%TMPF%.d"& echo ,"schema_advice":& type "%TMPF%.s"& echo })
set EXTRA="cluster":"%ATLAS_CLUSTER%",
call "%C%" envelope "%DAY%\cluster\advisor\cluster-advisor.json" atlas_api_cluster_advisor "" current_snapshot "%TMPF%.data"
call "%C%" cleanup

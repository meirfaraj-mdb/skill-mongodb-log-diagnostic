@echo off
rem Called by every run.cmd:  call "%~dp0..\_common.cmd"  (setup)  |  api OUT PATH [ACCEPT]  |  envelope OUT SRC HOST BASIS DATA  |  cleanup
rem Needs only cmd.exe + curl.exe (built into Windows 10 1803+ / 11). Same variables as _common.sh;
rem MDIAG_DATE is required here (YYYY-MM-DD). Optional window: MDIAG_START_ISO/MDIAG_END_ISO (2026-10-29T22:00:00Z)
rem and MDIAG_START_S/MDIAG_END_S (epoch seconds) - set all four, or none for the last 24 h.
if "%~1"=="api" goto :api
if "%~1"=="envelope" goto :envelope
if not "%~1"=="cleanup" goto :setup
if defined CURLCFG del /q "%CURLCFG%" 2>nul
if defined TMPF del /q "%TMPF%*" 2>nul
exit /b 0
:setup
if not defined MDIAG_DATE (echo set MDIAG_DATE=YYYY-MM-DD first & exit /b 1)
if not defined MDIAG_OUT set "MDIAG_OUT=observability-manual"
if not defined HOSTS_FILE set "HOSTS_FILE=hosts.txt"
set "DAY=%MDIAG_OUT%\%MDIAG_DATE%"
set "CURLCFG=%TEMP%\mdiag-%RANDOM%%RANDOM%.cfg"
set "TMPF=%TEMP%\mdiag-%RANDOM%%RANDOM%.tmp"
rem Secrets go to a private curl config file (-K) in your profile TEMP, deleted by "cleanup".
if /i "%MDIAG_TARGET%"=="opsmanager" (
  set "BASE=%OM_BASE_URL%/api/public/v1.0" & set "GROUP=%OM_GROUP_ID%" & set "ACCEPT=application/json"
  > "%CURLCFG%" (echo digest& echo user = "%OM_PUBLIC_KEY%:%OM_PRIVATE_KEY%")
  if defined OM_CA_FILE (>> "%CURLCFG%" echo cacert = "%OM_CA_FILE%")
  goto :ca
)
if not defined ATLAS_BASE set "ATLAS_BASE=https://cloud.mongodb.com/api/atlas/v2"
if not defined ATLAS_API_VERSION set "ATLAS_API_VERSION=2025-03-12"
if not defined ATLAS_TOKEN_URL set "ATLAS_TOKEN_URL=https://cloud.mongodb.com/api/oauth/token"
set "BASE=%ATLAS_BASE%" & set "GROUP=%ATLAS_GROUP_ID%" & set "ACCEPT=application/vnd.atlas.%ATLAS_API_VERSION%+json"
if not defined ATLAS_CLIENT_ID (
  > "%CURLCFG%" (echo digest& echo user = "%ATLAS_PUBLIC_KEY%:%ATLAS_PRIVATE_KEY%")
  goto :ca
)
> "%CURLCFG%" echo user = "%ATLAS_CLIENT_ID%:%ATLAS_CLIENT_SECRET%"
set "TOKEN="
for /f "tokens=2 delims=:," %%t in ('curl -sS -K "%CURLCFG%" -X POST -H "Accept: application/json" -d "grant_type=client_credentials" "%ATLAS_TOKEN_URL%"') do if not defined TOKEN set "TOKEN=%%~t"
if not defined TOKEN (echo service-account token request failed & exit /b 1)
> "%CURLCFG%" echo header = "Authorization: Bearer %TOKEN%"
set "TOKEN="
:ca
if defined CURL_CA_FILE (>> "%CURLCFG%" echo cacert = "%CURL_CA_FILE%")
if not defined GROUP (echo set ATLAS_GROUP_ID / OM_GROUP_ID & exit /b 1)
exit /b 0

:api
if not exist "%~dp2" mkdir "%~dp2"
set "ACC=%~4" & if "%~4"=="" set "ACC=%ACCEPT%"
set "HTTP=000"
for /f %%c in ('curl -sS -K "%CURLCFG%" -H "Accept: %ACC%" -o "%~2" -w "%%{http_code}" "%BASE%%~3"') do set "HTTP=%%c"
echo   HTTP %HTTP% %~3
if "%HTTP%"=="200" exit /b 0
exit /b 1

:envelope
rem extra JSON fields (each followed by a comma) come from the EXTRA variable
if not exist "%~dp2" mkdir "%~dp2"
> "%~2" (
  echo {"schema_version":2,"source":"%~3","host":"%~4","temporal_basis":"%~5","collector":"curl-sample",%EXTRA%"data":
  type "%~6"
  echo }
)
echo   wrote %~2
exit /b 0

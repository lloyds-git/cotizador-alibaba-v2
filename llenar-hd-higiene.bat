@echo off
REM Variante del wrapper para archivos con mapeo C=8,S=11,Y=16,Z=17 (HIGIENE)
REM Uso: llenar-hd-higiene HIGIENE.xlsx

setlocal
set "MAPEO=C=8,S=11,Y=16,Z=17"
call "%~dp0llenar-hd.bat" %*
exit /b %ERRORLEVEL%

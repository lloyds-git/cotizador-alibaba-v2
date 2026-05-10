@echo off
REM Wrapper para llenar el formato HD-Mascotas con un archivo de cotizacion.
REM Uso:
REM   llenar-hd ArchivoXXX.xlsx
REM   llenar-hd ArchivoXXX.xlsx --mapeo "C=8,S=11,Y=16,Z=17"
REM     - El archivo origen puede ser ruta relativa o absoluta.
REM     - El formato HD-Mascotas.xlsb se toma de la raiz del proyecto.
REM     - --mapeo opcional, default: C=8,O=11,U=16,V=17 (formato rejas)
REM       Tambien se puede via variable de entorno: set MAPEO=C=8,S=11,Y=16,Z=17

setlocal

set "PROYECTO=C:\Users\salomon.DC0\Documents\Mascotas-9Mayo"
set "SCRIPT=%PROYECTO%\llenar_formato_hd.py"
set "FORMATO=%PROYECTO%\Formato HD-Mascotas.xlsb"

if "%~1"=="" (
    echo Uso: llenar-hd ^<archivo_origen.xlsx^> [--mapeo "C=8,O=11,..."]
    echo.
    echo Ejemplos:
    echo   llenar-hd rejas.xlsx
    echo   llenar-hd HIGIENE.xlsx --mapeo "C=8,S=11,Y=16,Z=17"
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo ERROR: No se encontro el script: %SCRIPT%
    exit /b 1
)

if not exist "%FORMATO%" (
    echo ERROR: No se encontro el formato: %FORMATO%
    exit /b 1
)

REM Pasamos el primer arg + formato + el resto de args (para --mapeo "...")
set "ORIGEN=%~1"
shift
REM Usamos %* despues de un shift para preservar argumentos con =
REM (cmd no expande %* tras shift, asi que reconstruimos manualmente)
set "RESTO="
:loop
if "%~1"=="" goto fin
set RESTO=%RESTO% "%~1"
shift
goto loop
:fin

python "%SCRIPT%" "%ORIGEN%" "%FORMATO%" %RESTO%
exit /b %ERRORLEVEL%

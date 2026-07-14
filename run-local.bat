@echo off
REM ============================================================
REM  Levanta el Cotizador Mascotas localmente en Windows (host).
REM  NO usa Docker: asi el "Exportar HD" puede usar las librerias
REM  de Windows (pywin32 / Excel COM) para el .xlsb.
REM
REM  El puerto sale de APP_PORT en .env (default 8071), el mismo que
REM  publica docker-compose. Docker y este bat usan EL MISMO puerto:
REM  no corras los dos a la vez (o chocaran en el puerto y en data/).
REM  Si antes exponias 8082 por un tunel Cloudflare, apunta el tunel
REM  al nuevo puerto (APP_PORT) o cambia APP_PORT en .env.
REM
REM  Para detener la app: cierra esta ventana o Ctrl+C.
REM ============================================================
cd /d "%~dp0"
echo Iniciando Cotizador (puerto = APP_PORT de .env)  (Ctrl+C para detener)
echo.
"C:\Python314\python.exe" -m app.main
echo.
echo *** El servidor se detuvo. Presiona una tecla para cerrar esta ventana. ***
pause >nul

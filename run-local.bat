@echo off
REM ============================================================
REM  Levanta el Cotizador Mascotas localmente en Windows (host).
REM  NO usa Docker: asi el "Exportar HD" puede usar las librerias
REM  de Windows (pywin32 / Excel COM) para el .xlsb.
REM
REM  Escucha en 0.0.0.0:8082 para que el tunel Cloudflare
REM  (dev-pet.lloyds.com.mx -> IP local:8082) lo alcance.
REM
REM  Requisito: que Docker NO este publicando el 8082.
REM  Si lo esta, primero corre:  docker compose down
REM
REM  Para detener la app: cierra esta ventana o Ctrl+C.
REM ============================================================
cd /d "%~dp0"
echo Iniciando Cotizador en http://0.0.0.0:8082  (Ctrl+C para detener)
echo.
"C:\Python314\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload
echo.
echo *** El servidor se detuvo. Presiona una tecla para cerrar esta ventana. ***
pause >nul

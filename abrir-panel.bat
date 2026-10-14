@echo off
rem LoL Analyzer - abre el panel. Cualquier opcion extra se pasa al programa (por ejemplo --no-abrir).
rem Solo ASCII en este archivo: cmd se confunde con acentos en algunos Windows.
setlocal EnableExtensions
chcp 65001 >nul
title LoL Analyzer
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :sin_instalar
if not "%~1"=="" goto :abrir

rem Si el panel ya esta abierto en otra ventana, solo lo mostramos.
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul 2>&1
if not errorlevel 1 goto :ya_abierto

:abrir
echo ==================================================
echo   LoL Analyzer
echo ==================================================
echo.
echo El panel se abrira en tu navegador en unos segundos.
echo.
echo   IMPORTANTE: no cierres esta ventana mientras uses el panel.
echo   Para apagarlo, cierra esta ventana.
echo.
echo (Los mensajes en ingles que salen abajo son normales.)
echo.
".venv\Scripts\python.exe" -m lolan web %*
set "SALIDA=%errorlevel%"
echo.
echo El panel se cerro. Para abrirlo otra vez usa el acceso directo
echo "LoL Analyzer" del escritorio o este mismo archivo.
if not defined LOLAN_SIN_PAUSA pause
exit /b %SALIDA%

:ya_abierto
echo.
echo El panel ya parece estar abierto en otra ventana.
echo Lo abro en tu navegador. Si no aparece, escribe esto en el navegador:
echo   http://127.0.0.1:8765
start "" "http://127.0.0.1:8765/"
if not defined LOLAN_SIN_PAUSA pause
exit /b 0

:sin_instalar
echo.
echo Todavia no esta instalado. Haz doble clic en instalar.bat primero.
echo Cuando termine, el panel se abre solo.
if not defined LOLAN_SIN_PAUSA pause
exit /b 1

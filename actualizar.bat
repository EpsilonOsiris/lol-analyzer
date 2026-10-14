@echo off
rem LoL Analyzer - actualizar a la ultima version.
rem
rem cmd lee los .bat poco a poco y esta actualizacion reemplaza este mismo archivo, asi que
rem la primera etapa solo se copia a TEMP y sigue desde alli: el original deja de leerse.
rem (la etapa 1 borra la copia al terminar la etapa 2).
rem Variables solo para pruebas: LOLAN_ZIP_URL (direccion o ruta local del zip), LOLAN_PUERTO.
rem Solo ASCII en este archivo: cmd se confunde con acentos en algunos Windows.
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="/desde-copia" goto :etapa2

set "APP=%~dp0"
if "%APP:~-1%"=="\" set "APP=%APP:~0,-1%"
set "COPIA=%TEMP%\lolan-actualizar-%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%COPIA%" >nul
if errorlevel 1 goto :sin_copia
set "LOLAN_APP_DIR=%APP%"
rem Todo en una sola linea: cmd no vuelve a leer este archivo despues de la llamada.
setlocal EnableDelayedExpansion
call "%COPIA%" /desde-copia & set "RC=!errorlevel!" & del "%COPIA%" >nul 2>&1 & exit /b !RC!

:sin_copia
echo No se pudo preparar la actualizacion (no se pudo escribir en la carpeta temporal).
pause
exit /b 1

rem ================================================================== etapa 2
:etapa2
chcp 65001 >nul
title LoL Analyzer - Actualizar
set "APP=%LOLAN_APP_DIR%"
set "PUERTO=8765"
if defined LOLAN_PUERTO set "PUERTO=%LOLAN_PUERTO%"
set "ZIP_URL=https://github.com/EpsilonOsiris/lol-analyzer/archive/refs/heads/main.zip"
if defined LOLAN_ZIP_URL set "ZIP_URL=%LOLAN_ZIP_URL%"
set "WORK=%TEMP%\lolan-update-%RANDOM%%RANDOM%"

echo ==================================================
echo   LoL Analyzer - Actualizar
echo ==================================================
echo.

if not exist "%APP%\lolan\__main__.py" goto :no_es_la_carpeta
if exist "%APP%\.git" goto :es_repositorio

netstat -ano | findstr /R /C:":%PUERTO% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto :panel_abierto

echo Tus datos no se tocan: se conservan el archivo .env y las carpetas data y reports.
echo.

rem -------------------------------------------------------------- 1. descargar
echo [1/3] Descargando la ultima version...
mkdir "%WORK%\x" >nul 2>&1
set "ZIP=%WORK%\lol-analyzer.zip"
if exist "%ZIP_URL%" goto :zip_local
"%SystemRoot%\System32\curl.exe" -L --fail --silent --show-error -o "%ZIP%" "%ZIP_URL%"
if errorlevel 1 goto :fallo_descarga
goto :zip_listo
:zip_local
copy /y "%ZIP_URL%" "%ZIP%" >nul
if errorlevel 1 goto :fallo_descarga
:zip_listo

"%SystemRoot%\System32\tar.exe" -xf "%ZIP%" -C "%WORK%\x"
if errorlevel 1 goto :fallo_zip
set "SRC="
for /d %%D in ("%WORK%\x\*") do if not defined SRC set "SRC=%%~fD"
if not exist "%SRC%\lolan\__main__.py" goto :fallo_zip

rem ------------------------------------------------------------------ 2. copiar
echo [2/3] Copiando los archivos nuevos...
rem Sin /MIR ni /PURGE: no se borra nada. Se excluyen .env, data, reports y .venv.
robocopy "%SRC%" "%APP%" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP /XD "%SRC%\data" "%SRC%\reports" "%SRC%\.venv" "%SRC%\.git" "%APP%\data" "%APP%\reports" "%APP%\.venv" __pycache__ /XF .env >nul
if errorlevel 8 goto :fallo_copia

rem ------------------------------------------------------------- 3. librerias
echo [3/3] Revisando Python y las librerias...
echo.
call "%APP%\instalar.bat" /sin-acceso-directo /sin-abrir
if errorlevel 1 goto :fallo_librerias

rmdir /s /q "%WORK%" >nul 2>&1
echo.
echo ==================================================
echo   Listo: LoL Analyzer esta actualizado.
echo ==================================================
echo Abre el panel con el acceso directo "LoL Analyzer" o con abrir-panel.bat.
echo.
if not defined LOLAN_SIN_PAUSA pause
exit /b 0

rem ---------------------------------------------------------------- errores
:no_es_la_carpeta
echo ERROR: esta carpeta no parece ser la de LoL Analyzer.
goto :error_fin

:es_repositorio
echo Esta carpeta es un repositorio de git, no una instalacion normal.
echo Para actualizar usa: git pull
goto :error_fin

:panel_abierto
echo El panel esta abierto. Cierra primero la ventana negra del panel
echo (la que dice "no cierres esta ventana") y vuelve a hacer doble clic
echo en actualizar.bat.
goto :error_fin

:fallo_descarga
echo.
echo ERROR: no se pudo descargar la actualizacion. Revisa tu conexion a internet.
echo Si usas un antivirus o una VPN, prueba desactivarlos un momento.
goto :error_fin

:fallo_zip
echo.
echo ERROR: el archivo descargado no es valido. Vuelve a intentarlo en un rato.
goto :error_fin

:fallo_copia
echo.
echo ERROR: no se pudieron copiar los archivos. Cierra el panel y cualquier
echo programa que tenga abierta esta carpeta, y vuelve a intentarlo.
goto :error_fin

:fallo_librerias
echo.
echo Los archivos se actualizaron, pero fallo la revision de las librerias.
echo Haz doble clic en instalar.bat para terminarla.
goto :error_fin

:error_fin
echo.
echo La actualizacion no se completo.
if exist "%WORK%" rmdir /s /q "%WORK%" >nul 2>&1
if not defined LOLAN_SIN_PAUSA pause
exit /b 1

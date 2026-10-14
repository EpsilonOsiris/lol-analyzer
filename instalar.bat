@echo off
rem LoL Analyzer - instalacion (doble clic). Se puede ejecutar varias veces sin problema.
rem Opciones para pruebas: /sin-acceso-directo  /sin-abrir
rem Solo ASCII en este archivo: cmd se confunde con acentos en algunos Windows.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title LoL Analyzer - Instalacion
cd /d "%~dp0"

set "SIN_ACCESO=0"
set "SIN_ABRIR=0"
:leer_opciones
if "%~1"=="" goto :opciones_listas
if /i "%~1"=="/sin-acceso-directo" set "SIN_ACCESO=1"
if /i "%~1"=="/sin-abrir" set "SIN_ABRIR=1"
shift
goto :leer_opciones
:opciones_listas

echo ==================================================
echo   LoL Analyzer - Instalacion
echo ==================================================
echo.
echo Esto puede tardar unos minutos la primera vez.
echo No cierres esta ventana hasta que termine.
echo.

if not exist "requirements.txt" goto :carpeta_incorrecta
if not exist "lolan\__main__.py" goto :carpeta_incorrecta

rem ---------------------------------------------------------------- 1. Python
echo [1/4] Buscando Python...
call :buscar_python
if defined PYCMD goto :python_listo
echo       No se encontro Python 3.10 o mas nuevo en este equipo.
where winget >nul 2>&1
if errorlevel 1 goto :sin_winget
echo       Instalando Python con winget. Puede tardar unos minutos
echo       y Windows puede pedirte permiso: acepta.
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
call :buscar_python
if defined PYCMD goto :python_listo
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" call :probar_python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PYCMD goto :python_listo
goto :python_no_aparece

:python_listo
echo       Python listo.

rem ---------------------------------------------------------------- 2. Entorno
echo [2/4] Preparando el entorno del programa...
if not exist ".venv\Scripts\python.exe" goto :crear_entorno
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 goto :recrear_entorno
echo       El entorno ya existia; se reutiliza.
goto :entorno_listo

:crear_entorno
%PYCMD% -m venv .venv
if errorlevel 1 goto :entorno_fallo
goto :entorno_listo

:recrear_entorno
echo       El entorno anterior estaba danado; se vuelve a crear. Tus datos no se tocan.
%PYCMD% -m venv --clear .venv
if errorlevel 1 goto :entorno_fallo

:entorno_listo

rem ------------------------------------------------------------- 3. Librerias
echo [3/4] Instalando las librerias del programa. Necesita internet y puede
echo       tardar un par de minutos...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if not errorlevel 1 goto :librerias_listas
echo       Reintentando con los certificados de Windows, por si un antivirus
echo       esta bloqueando la descarga...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --use-feature=truststore -r requirements.txt
if errorlevel 1 goto :librerias_fallo

:librerias_listas
echo       Librerias listas.

rem --------------------------------------------------------------- 4. ffmpeg
echo [4/4] Buscando ffmpeg. Se usara en el analisis de video, que llegara pronto.
where ffmpeg >nul 2>&1
if not errorlevel 1 goto :ffmpeg_listo
where winget >nul 2>&1
if errorlevel 1 goto :ffmpeg_a_mano
echo       Instalando ffmpeg con winget...
winget install -e --id Gyan.FFmpeg.Essentials --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :ffmpeg_a_mano
echo       ffmpeg instalado.
goto :ffmpeg_fin
:ffmpeg_a_mano
echo       No se pudo instalar ffmpeg. No pasa nada: hoy el programa funciona sin el.
echo       Mas adelante, para el analisis de video, se puede instalar desde ffmpeg.org.
goto :ffmpeg_fin
:ffmpeg_listo
echo       ffmpeg ya estaba instalado.
:ffmpeg_fin

rem ------------------------------------------------------------ Acceso directo
if "%SIN_ACCESO%"=="1" goto :acceso_fin
echo.
echo Creando el acceso directo "LoL Analyzer" en el escritorio...
set "LOLAN_APP=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:LOLAN_ACCESO_DIR; if(-not $d){$d=[Environment]::GetFolderPath('Desktop')}; $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'LoL Analyzer.lnk')); $s.TargetPath=(Join-Path $env:LOLAN_APP 'abrir-panel.bat'); $s.WorkingDirectory=$env:LOLAN_APP; $s.Description='Abre el panel de LoL Analyzer'; $s.Save()"
if errorlevel 1 goto :acceso_fallo
echo       Acceso directo creado.
goto :acceso_fin
:acceso_fallo
echo       No se pudo crear el acceso directo. Puedes abrir el panel con el archivo
echo       abrir-panel.bat de esta carpeta.
:acceso_fin

echo.
echo ==================================================
echo   Instalacion terminada.
echo ==================================================
if "%SIN_ABRIR%"=="1" exit /b 0
echo.
echo Abriendo el panel. La primera vez te pedira tu Riot ID y tu key de Riot.
echo Despues, para abrirlo usa el acceso directo "LoL Analyzer" del escritorio.
echo.
call "%~dp0abrir-panel.bat"
exit /b 0

rem ------------------------------------------------------------- Subrutinas
:buscar_python
set "PYCMD="
call :probar_python py -3
if defined PYCMD exit /b 0
call :probar_python python
exit /b 0

:probar_python
rem Prueba el comando %1 %2; si es Python 3.10 o mas nuevo lo deja en PYCMD.
rem Ojo: el "python" de la Tienda de Microsoft falla aqui y se descarta.
"%~1" %~2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set PYCMD="%~1" %~2
exit /b 0

rem ---------------------------------------------------------------- Errores
:carpeta_incorrecta
echo.
echo ERROR: no encuentro los archivos del programa junto a instalar.bat.
echo Parece que lo abriste desde dentro del archivo zip. Haz esto:
echo   1. Cierra esta ventana.
echo   2. Clic derecho sobre el archivo zip y elige "Extraer todo...".
echo   3. Abre la carpeta nueva y haz doble clic en instalar.bat desde ahi.
goto :error_fin

:sin_winget
echo.
echo       Este equipo no tiene "winget", asi que no puedo instalar Python solo.
echo       Instalalo a mano, es rapido:
echo         1. Entra a https://www.python.org/downloads/
echo         2. Descarga Python 3.13 con el boton amarillo.
echo         3. Abre el instalador y MARCA la casilla "Add python.exe to PATH"
echo            que esta abajo de todo.
echo         4. Pulsa "Install Now" y espera a que termine.
echo         5. Vuelve a hacer doble clic en instalar.bat.
goto :error_fin

:python_no_aparece
echo.
echo ERROR: se intento instalar Python pero no lo encuentro todavia.
echo Cierra esta ventana y vuelve a hacer doble clic en instalar.bat.
echo Si sigue igual, instala Python a mano desde https://www.python.org/downloads/
echo marcando la casilla "Add python.exe to PATH".
goto :error_fin

:entorno_fallo
echo.
echo ERROR: no se pudo crear el entorno del programa (carpeta .venv).
echo Revisa que haya espacio en el disco y que la carpeta no sea de solo lectura.
goto :error_fin

:librerias_fallo
echo.
echo ERROR: no se pudieron instalar las librerias.
echo Revisa tu conexion a internet. Si usas un antivirus o una VPN, prueba
echo desactivarlos un momento y vuelve a hacer doble clic en instalar.bat.
goto :error_fin

:error_fin
echo.
echo La instalacion no se pudo completar.
if not defined LOLAN_SIN_PAUSA pause
exit /b 1

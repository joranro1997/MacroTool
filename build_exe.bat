@echo off
rem Genera dist\MacroTool.exe (un solo fichero, sin consola) con PyInstaller y MacroTool.spec.
rem Requisitos: python -m pip install -r requirements.txt pyinstaller
setlocal
cd /d "%~dp0"

rem Doble clic desde el Explorador: esperar una tecla al final para poder leer el resultado.
set "PAUSE_AT_END="
echo "%cmdcmdline%" | find /i "%~nx0" >nul && set "PAUSE_AT_END=1"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No se encuentra python en el PATH.
    goto :fail
)
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PyInstaller no esta instalado. Instalalo con:
    echo         python -m pip install pyinstaller
    goto :fail
)

if not exist "assets\icon.ico" (
    echo Generando el icono...
    python tools\make_icon.py
    if errorlevel 1 goto :fail
)

echo Limpiando build\ y dist\ ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist build (
    echo [ERROR] No se pudo borrar build\
    goto :fail
)
if exist dist (
    echo [ERROR] No se pudo borrar dist\ ^(cierra MacroTool.exe si esta abierto^)
    goto :fail
)

python -m PyInstaller --noconfirm --clean MacroTool.spec
if errorlevel 1 goto :fail
if not exist "dist\MacroTool.exe" goto :fail

echo.
echo ============================================================
echo  Ejecutable generado:
echo  %~dp0dist\MacroTool.exe
echo ============================================================
if defined PAUSE_AT_END pause
endlocal
exit /b 0

:fail
echo.
echo [ERROR] No se pudo generar el ejecutable.
if defined PAUSE_AT_END pause
endlocal
exit /b 1

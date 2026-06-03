@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [Companion] Creando entorno virtual de Windows...
    python -m venv venv
    echo [Companion] Instalando dependencias...
    venv\Scripts\pip install -r requirements.txt
)

:: Abrir puerto 7891 en el firewall para WSL2
netsh advfirewall firewall delete rule name="Iris Companion" >nul 2>&1
netsh advfirewall firewall add rule name="Iris Companion" dir=in action=allow protocol=TCP localport=7891 >nul 2>&1

:: Matar cualquier proceso que ocupe el puerto 7891
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":7891 "') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo [Companion] Iniciando Iris Desktop Companion...
venv\Scripts\python server.py
pause

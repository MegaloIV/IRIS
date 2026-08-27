@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [Companion] Creando entorno virtual de Windows...
    python -m venv venv
    echo [Companion] Instalando dependencias...
    venv\Scripts\pip install -r requirements.txt
)

:: Abrir el puerto 7891 SOLO para el rango de WSL2, no para toda la red.
:: Aun asi el companion exige token en cada peticion (ver auth.py) — el
:: firewall es la segunda capa, no la unica.
netsh advfirewall firewall delete rule name="Iris Companion" >nul 2>&1
netsh advfirewall firewall add rule name="Iris Companion" dir=in action=allow protocol=TCP localport=7891 remoteip=172.16.0.0/12,127.0.0.1 >nul 2>&1

:: Matar cualquier proceso que ocupe el puerto 7891
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":7891 "') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo [Companion] Iniciando Iris Desktop Companion...
venv\Scripts\python server.py
pause

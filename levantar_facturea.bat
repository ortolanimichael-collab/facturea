@echo off
REM ============================================================
REM  Levanta Facturea en esta PC con un doble clic.
REM  Tiene que estar guardado DENTRO de la carpeta "proyecto"
REM  (al lado de app.py) para que las rutas relativas funcionen.
REM ============================================================

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo No encontre el entorno virtual "venv" en esta carpeta.
    echo Antes de usar este .bat una sola vez hay que crearlo:
    echo.
    echo     python -m venv venv
    echo     venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo     playwright install chromium
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

if not exist ".env" (
    echo No encontre el archivo .env en esta carpeta.
    echo Copia .env.example a .env y completa SECRET_KEY y ENCRYPTION_KEY antes de seguir.
    pause
    exit /b 1
)

set FLASK_APP=app.py

echo Aplicando migraciones de base de datos si hay alguna pendiente...
flask db upgrade
if errorlevel 1 (
    echo.
    echo La migracion fallo -- revisa el error de arriba antes de seguir.
    pause
    exit /b 1
)

echo.
echo Levantando Facturea en http://localhost:5000 ...
echo (Dejar esta ventana abierta mientras uses el sistema. Cerrarla apaga el servidor.)
echo.

start "" http://localhost:5000
python app.py

pause

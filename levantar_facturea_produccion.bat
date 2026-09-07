@echo off
REM ============================================================
REM Levanta Facturea LOCAL apuntando a la base de datos REAL de
REM produccion en Render, y abre el navegador solo.
REM
REM OJO: esto conecta tu PC a la base de datos REAL. Cualquier
REM cambio que hagas (facturar, editar, borrar) impacta en
REM produccion de verdad, no en una copia de prueba.
REM ============================================================

cd /d C:\src\facturea

echo.
echo ============================================================
echo  Levantando Facturea, conectado a la base de PRODUCCION
echo ============================================================
echo.

start "Facturea - Servidor local" cmd /k _servidor_facturea.bat

timeout /t 5 /nobreak >nul

start "" http://localhost:5000

echo Listo. El servidor sigue corriendo en la otra ventana -- no la cierres.

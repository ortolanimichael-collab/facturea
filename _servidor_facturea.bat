@echo off
cd /d C:\src\facturea
call venv\Scripts\activate
set DATABASE_URL=postgresql://facturea_db_user:pZIXKgHSiudjNIZDffoQ3XUKR2wFXtmM@dpg-da5e1qajobas73edva30-a.oregon-postgres.render.com/facturea_db
set FLASK_APP=app.py
python app.py

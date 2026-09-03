#!/bin/sh
set -e

echo "Aplicando migraciones de la base de datos..."
flask db upgrade

echo "Arrancando el servidor..."
exec gunicorn app:app \
    --bind 0.0.0.0:10000 \
    --timeout 300 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}"

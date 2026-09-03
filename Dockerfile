FROM python:3.11-slim

# Tesseract (OCR) y las librerías del sistema que Playwright/Chromium
# necesitan para correr -- no son paquetes de Python, van aparte.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala el navegador Chromium que usa Playwright para automatizar ARCA,
# junto con todas sus dependencias de sistema (--with-deps se encarga de
# eso automáticamente, para no tener que listarlas una por una acá).
RUN playwright install --with-deps chromium

COPY . .

ENV FLASK_APP=app.py
RUN chmod +x /app/entrypoint.sh

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]

# Facturea

Facturación electrónica automática para monotributistas: lee comprobantes de pago (Mercado Pago, Ualá, transferencias bancarias) y factura ante ARCA sin tipear nada a mano.

## Estado actual

Landing page servida con Flask. Todavía no incluye backend de lectura de comprobantes ni facturación (próximos pasos del proyecto).

## Correr en tu PC

```bash
pip install -r requirements.txt
python app.py
```

Después abrí `http://localhost:5000` en el navegador.

## Deploy en Render

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app`

No requiere variables de entorno por ahora.

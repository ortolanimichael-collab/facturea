"""
Uso: python revisar_tablas.py
Muestra qué tablas tiene realmente instance/facturea.db (o la ruta que
tengas configurada en DATABASE_URL) -- para diagnosticar si la migración
se aplicó de verdad o el server está mirando un archivo distinto.
"""
import os
import sqlite3

RUTA = os.path.join("instance", "facturea.db")

print(f"Revisando: {os.path.abspath(RUTA)}")

if not os.path.exists(RUTA):
    print("El archivo NO existe en esa ruta.")
else:
    tamano = os.path.getsize(RUTA)
    print(f"El archivo existe -- tamaño: {tamano} bytes")
    con = sqlite3.connect(RUTA)
    tablas = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    con.close()
    print("Tablas encontradas:", tablas)

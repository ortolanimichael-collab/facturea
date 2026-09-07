"""
Conexión de Mercado Pago POR EMPRESA (por cliente) -- cada empresa conecta
SU PROPIA cuenta de Mercado Pago, y lo que se lee (pagos recibidos por
Point, QR, link de pago, o transferencias dentro de Mercado Pago) es
siempre de ESA cuenta, nunca de otra. Mismo patrón que google_drive_cliente.py:
token guardado cifrado por empresa, nada de credenciales compartidas.

Requiere 2 variables de entorno (se consiguen creando una "Aplicación" en
https://www.mercadopago.com.ar/developers, una sola para todo Facturea, no
una por cliente):
    MERCADOPAGO_CLIENT_ID
    MERCADOPAGO_CLIENT_SECRET
Y opcionalmente:
    MERCADOPAGO_REDIRECT_URI  (si no se define, usa la de abajo -- tiene
    que coincidir EXACTO con la cargada en el panel de desarrolladores)
"""
import os
import secrets

import requests

API_BASE = "https://api.mercadopago.com"
AUTH_BASE = "https://auth.mercadopago.com"


def _client_id():
    return os.environ.get("MERCADOPAGO_CLIENT_ID", "")


def _client_secret():
    return os.environ.get("MERCADOPAGO_CLIENT_SECRET", "")


def _redirect_uri():
    return os.environ.get("MERCADOPAGO_REDIRECT_URI", "http://localhost:5000/mercadopago/callback")


def esta_configurado():
    """Si no se cargaron las 2 variables de entorno, esta función no está disponible todavía."""
    return bool(_client_id() and _client_secret())


def generar_url_autorizacion(empresa_id):
    """
    Arma el link al que hay que mandar al dueño de la empresa para que
    autorice el acceso a su cuenta de Mercado Pago. "state" lleva el id de
    la empresa, para saber a cuál conectar cuando Mercado Pago nos
    devuelva al callback -- y de paso sirve como protección estándar de
    OAuth contra pedidos de callback falsos (se valida que coincida con lo
    que se generó acá).
    """
    state = f"{empresa_id}:{secrets.token_urlsafe(16)}"
    params = (
        f"client_id={_client_id()}"
        f"&response_type=code"
        f"&platform_id=mp"
        f"&state={state}"
        f"&redirect_uri={_redirect_uri()}"
    )
    return f"{AUTH_BASE}/authorization?{params}", state


def procesar_callback(code):
    """
    Se llama desde la ruta /mercadopago/callback, con el "code" que mandó
    Mercado Pago. Devuelve (refresh_token, user_id, email).
    """
    respuesta = requests.post(
        f"{API_BASE}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        timeout=15,
    )
    respuesta.raise_for_status()
    datos = respuesta.json()

    access_token = datos["access_token"]
    refresh_token = datos["refresh_token"]
    user_id = str(datos.get("user_id", ""))

    # El token de acceso no trae el email -- hace falta un pedido aparte,
    # solo para mostrar "Conectado: fulano@gmail.com" en el panel.
    email = ""
    try:
        perfil = requests.get(
            f"{API_BASE}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        perfil.raise_for_status()
        email = perfil.json().get("email", "")
    except requests.RequestException:
        pass  # no es crítico -- si falla, queda conectado igual, solo sin mostrar el email

    return refresh_token, user_id, email


def _access_token_de(empresa):
    """
    El access_token de Mercado Pago dura poco -- a diferencia de Drive, acá
    no hay una librería que lo renueve sola, así que se pide uno nuevo con
    el refresh_token cada vez que hace falta hacer una consulta. El
    refresh_token en sí no cambia (Mercado Pago no lo rota en cada uso).
    """
    refresh_token = empresa.get_mercadopago_token()
    if not refresh_token:
        raise ValueError(f"La empresa '{empresa.nombre_interno}' no tiene Mercado Pago conectado.")

    respuesta = requests.post(
        f"{API_BASE}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    respuesta.raise_for_status()
    return respuesta.json()["access_token"]


def buscar_pagos(empresa, fecha_desde_iso, fecha_hasta_iso):
    """
    Trae todos los pagos APROBADOS de esta empresa entre dos fechas
    (formato ISO completo, ej. "2026-09-01T00:00:00.000-03:00"). Devuelve
    una lista de pagos (diccionarios), ya recorriendo todas las páginas --
    Mercado Pago los entrega de a bloques de 50 como máximo por pedido.
    """
    access_token = _access_token_de(empresa)
    headers = {"Authorization": f"Bearer {access_token}"}

    pagos = []
    offset = 0
    limite = 50
    while True:
        respuesta = requests.get(
            f"{API_BASE}/v1/payments/search",
            headers=headers,
            params={
                "status": "approved",
                "range": "date_approved",
                "begin_date": fecha_desde_iso,
                "end_date": fecha_hasta_iso,
                "sort": "date_approved",
                "criteria": "asc",
                "offset": offset,
                "limit": limite,
            },
            timeout=20,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
        resultados = datos.get("results", [])
        pagos.extend(resultados)

        total = datos.get("paging", {}).get("total", 0)
        offset += limite
        if offset >= total or not resultados:
            break

    return pagos


def desconectar(empresa):
    empresa.mercadopago_token_cifrado = None
    empresa.mercadopago_email = None
    empresa.mercadopago_user_id = None

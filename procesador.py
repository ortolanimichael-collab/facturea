import os
import random
from datetime import datetime, timedelta

from automatizacion.arca_bot import concepto_efectivo
from lector import lector_core
from models import db, Comprobante, Empresa
from almacenamiento import guardar_archivo_persistente

EXTENSIONES_VALIDAS = {"png", "jpg", "jpeg", "pdf"}

# ---------- Mercado Pago: mapa de payment_method_id -> ARCA ----------
# Cuando no encuentra un código exacto acá, cae a "Otra..." con el nombre
# que mandó Mercado Pago como detalle -- mismo criterio que ya se usa para
# las tarjetas que el lector de imágenes no reconoce (ver lector_core.py).
MAPA_TARJETAS_MERCADOPAGO = {
    # Débito
    "debvisa": ("Otra...", "VISA Débito"),  # "Visa" a secas en Débito no es una opción real de ARCA, ver lector_core.py
    "debmaster": ("Mastercard Débito", None),
    "maestro": ("Maestro", None),
    "debcabal": ("Cabal 24 hs", None),
    # Crédito
    "visa": ("Visa", None),
    "master": ("Mastercard", None),
    "amex": ("American Express", None),
    "cabal": ("Cabal", None),
    "naranja": ("Tarjeta Naranja", None),
    "cencosud": ("Tarjeta Shopping", None),
    "cordial": ("Credencial", None),
}


def _mapear_medio_pago_mercadopago(pago):
    """
    Traduce el payment_method_id/payment_type_id de un pago de Mercado Pago
    a (medio_pago_detectado, tipo_pago, tipo_pago_detalle) -- el mismo
    formato que ya usa el lector de imágenes, así el resto del sistema
    (Revisión Manual, arca_bot.py) no necesita saber de dónde salió el dato.
    """
    payment_type_id = pago.get("payment_type_id") or ""
    payment_method_id = pago.get("payment_method_id") or ""

    if payment_type_id not in ("credit_card", "debit_card", "prepaid_card"):
        # account_money (transferencia dentro de Mercado Pago), bank_transfer,
        # ticket, etc. -- para ARCA todos van como "Transferencia Bancaria".
        return "Transferencia", None, None

    medio_pago = "Débito" if payment_type_id in ("debit_card", "prepaid_card") else "Crédito"
    tipo_pago, detalle = MAPA_TARJETAS_MERCADOPAGO.get(payment_method_id, (None, None))
    if tipo_pago is None:
        # No está en el mapa -- se carga como "Otra..." con el nombre que
        # mandó Mercado Pago, para no inventar una marca que no es.
        detalle = (payment_method_id or "Tarjeta").upper()
        tipo_pago = "Otra..."
    return medio_pago, tipo_pago, detalle


def crear_comprobante_desde_pago_mercadopago(pago, usuario_id, empresa):
    """
    Arma un Comprobante "pendiente" a partir de un pago ya traído de la API
    de Mercado Pago (ver mercadopago_cliente.buscar_pagos) -- mismos valores
    por defecto que procesar_archivo(), pero sin imagen: el monto, la fecha
    y la tarjeta ya vienen exactos de Mercado Pago, sin que el lector tenga
    que adivinar nada. Devuelve el Comprobante nuevo, o None si ese pago ya
    se había traído antes (para no duplicarlo).
    """
    id_transaccion = f"MP-{pago['id']}"
    if Comprobante.query.filter_by(id_transaccion=id_transaccion, empresa_id=empresa.id).first():
        return None

    importe_total = pago.get("transaction_amount") or 0.0
    cantidad = 1.0
    medio_pago_detectado, tipo_pago, tipo_pago_detalle = _mapear_medio_pago_mercadopago(pago)

    if medio_pago_detectado == "Débito":
        condicion_venta_default = "Tarjeta de Débito"
    elif medio_pago_detectado == "Crédito":
        condicion_venta_default = "Tarjeta de Crédito"
    else:
        condicion_venta_default = (empresa.config_condicion_venta or "").split(",")[0]

    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_aprobado = pago.get("date_approved") or pago.get("date_created")
    try:
        fecha_comprobante = datetime.fromisoformat(fecha_aprobado).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        fecha_comprobante = (datetime.now() - timedelta(days=dias_atras)).strftime("%d/%m/%Y")

    if empresa.config_descripcion_aleatoria:
        opciones_descripcion = [d.strip() for d in (empresa.descripciones_disponibles or "").split(",") if d.strip()]
    else:
        opciones_descripcion = []
    descripcion_elegida = random.choice(opciones_descripcion) if opciones_descripcion else empresa.config_producto_servicio

    alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
    if alicuota_elegida is None:
        alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    payer = pago.get("payer") or {}
    nombre_pagador = (
        f"{payer.get('first_name', '')} {payer.get('last_name', '')}".strip()
        or payer.get("email") or None
    )

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,

        punto_venta=empresa.config_punto_venta,
        tipo_comprobante=(empresa.config_tipo_comprobante or "").split(",")[0],
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=importe_total / cantidad,

        nombre_remitente=nombre_pagador,
        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=tipo_pago,
        tipo_pago_detalle=tipo_pago_detalle,
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=condicion_venta_default,
        importe_total=importe_total,
        cantidad=cantidad,
        archivo_origen=f"Mercado Pago #{pago['id']}",
    )
    db.session.add(fila)
    return fila


def procesar_archivo(ruta_local, nombre_original, usuario_id, empresa_id, fecha_interfaz, cuit_propio_cliente="", drive_file_id=None):
    """
    Corre un archivo ya descargado/subido a disco por el lector, y si los datos
    son válidos y no están duplicados, lo guarda en la base para esa empresa.
    Devuelve una tupla (resultado, comprobante_relacionado, info_archivo_intento):
      - resultado: "nuevo" | "duplicado" | "error" | "ignorado"
      - comprobante_relacionado: si es "nuevo", el comprobante recién creado;
        si es "duplicado", el comprobante ORIGINAL ya existente al que corresponde
        (para poder comparar las dos imágenes); si no, None.
      - info_archivo_intento: para "duplicado" y "error" -- una tupla
        (archivo_ruta, archivo_drive_id) de DÓNDE quedó guardada la imagen de
        ESTE intento (distinta de la del original, para "duplicado"), para
        poder mostrarla o cargar el comprobante a mano después. (None, None)
        en los demás casos ("nuevo" ya tiene su propio archivo_ruta en el
        comprobante creado; "ignorado" nunca se guarda).

    Los campos de facturación (tipo de comprobante, condición de IVA, etc.)
    arrancan con el valor configurado en la Empresa, pero quedan grabados en
    el propio comprobante -- de ahí en más son editables sin afectar a las
    demás facturas de esa empresa.
    """
    ext = nombre_original.lower().split(".")[-1]
    if ext not in EXTENSIONES_VALIDAS:
        return "ignorado", None, (None, None)

    empresa = Empresa.query.get(empresa_id)

    if ext == "pdf":
        datos = lector_core.extraer_datos_de_pdf(ruta_local, fecha_interfaz, cuit_propio_cliente)
    else:
        datos = lector_core.extraer_datos_de_imagen(ruta_local, fecha_interfaz, cuit_propio_cliente)

    if not datos:
        # Antes esto no guardaba la imagen en ningún lado -- el archivo se
        # perdía apenas terminaba la subida y no había forma de verlo ni de
        # cargar el comprobante a mano después. Se guarda igual que un
        # duplicado, para poder mostrarlo en "Archivos con error de lectura".
        archivo_ruta_intento, archivo_drive_id_intento = guardar_archivo_persistente(ruta_local, nombre_original, empresa)
        return "error", None, (archivo_ruta_intento, archivo_drive_id_intento)

    duplicado = Comprobante.query.filter_by(
        id_transaccion=datos.get("ID_Transaccion"), empresa_id=empresa_id
    ).first()
    if duplicado:
        archivo_ruta_intento, archivo_drive_id_intento = guardar_archivo_persistente(ruta_local, nombre_original, empresa)
        return "duplicado", duplicado, (archivo_ruta_intento, archivo_drive_id_intento)

    importe_total = datos.get("Importe Total") or 0.0
    cantidad = 1.0
    medio_pago_detectado = datos.get("Medio Pago") or "Transferencia"

    # La condición de venta por defecto tiene que coincidir con lo que el
    # lector detectó: si es un pago con tarjeta, el checkbox correcto en ARCA
    # es "Tarjeta de Débito"/"Tarjeta de Crédito", no lo que tenga configurado
    # la empresa como default general (pensado para transferencias).
    if medio_pago_detectado == "Débito":
        condicion_venta_default = "Tarjeta de Débito"
    elif medio_pago_detectado == "Crédito":
        condicion_venta_default = "Tarjeta de Crédito"
    else:
        # La primera condición de venta configurada en la empresa -- el
        # checkbox real de ARCA solo permite una por factura.
        condicion_venta_default = (empresa.config_condicion_venta or "").split(",")[0] if empresa else ""

    archivo_ruta, archivo_drive_id = guardar_archivo_persistente(ruta_local, nombre_original, empresa)

    # Si el lector no pudo leer la fecha del comprobante en la imagen, se
    # completa con un default razonable: hoy menos los días configurados en
    # la empresa (el mismo criterio que ya se usa al momento de facturar, ver
    # calcular_fecha_facturacion en arca_bot.py -- 10 días si la empresa no
    # configuró nada distinto). Se calcula UNA vez, con la fecha de HOY en el
    # momento de subir el archivo, y queda grabado así en el comprobante -- si
    # el usuario lo edita después a mano, se queda con lo que él puso, no se
    # vuelve a recalcular.
    dias_atras = (empresa.config_dias_atras_fecha_emision if empresa and empresa.config_dias_atras_fecha_emision else 10)
    fecha_comprobante_detectada = datos.get("Fecha del Comprobante") or (datetime.now() - timedelta(days=dias_atras)).strftime("%d/%m/%Y")

    # Descripción del ítem: si la empresa cargó varias (para no repetir siempre
    # la misma en todas las facturas) y activó "elegir al azar", se sortea una;
    # si no, se usa la que tiene configurada como default.
    if empresa and empresa.config_descripcion_aleatoria:
        opciones_descripcion = [d.strip() for d in (empresa.descripciones_disponibles or "").split(",") if d.strip()]
    else:
        opciones_descripcion = []
    descripcion_elegida = random.choice(opciones_descripcion) if opciones_descripcion else (empresa.config_producto_servicio if empresa else None)

    # Si esa descripción tiene una alícuota propia cargada (ej. "Embutidos"
    # -> 10.5%, para un Responsable Inscripto que vende cosas con distinta
    # alícuota), se usa esa -- si no, se cae al default general de la
    # empresa (la primera de la lista que haya marcado).
    alicuota_elegida = None
    if empresa:
        alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
        if alicuota_elegida is None:
            alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa_id,
        drive_file_id=drive_file_id,
        id_transaccion=datos.get("ID_Transaccion"),

        punto_venta=empresa.config_punto_venta if empresa else None,
        tipo_comprobante=(empresa.config_tipo_comprobante or "").split(",")[0] if empresa else None,
        concepto=(
            concepto_efectivo(fecha_comprobante_detectada, empresa.config_concepto, dias_atras)
            if empresa else None
        ),
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida if empresa else None,
        precio_unitario=importe_total / cantidad,

        tipo_documento=datos.get("Tipo Documento"),
        cuit_receptor=str(datos.get("CUIT Receptor") or ""),
        cuit_alternativo=str(datos.get("CUIT Alternativo") or "") or None,
        alicuota_iva=alicuota_elegida,
        nombre_razon_social=datos.get("Nombre / Razón Social"),
        nombre_remitente=datos.get("Nombre Remitente"),
        fecha_comprobante=fecha_comprobante_detectada,
        fecha_no_detectada=bool(datos.get("Fecha No Detectada")),
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=datos.get("Tipo Pago"),
        tipo_pago_detalle=datos.get("Tipo Pago Detalle"),
        numero_pago=datos.get("Numero Pago"),
        condicion_iva=datos.get("Condicion IVA") or (empresa.config_condicion_iva if empresa else None),
        condicion_venta=datos.get("Condicion Venta") or condicion_venta_default,
        fecha_desde=datos.get("Fecha Desde"),
        fecha_hasta=datos.get("Fecha Hasta"),
        importe_total=importe_total,
        cantidad=cantidad,
        archivo_origen=datos.get("Archivo Origen") or nombre_original,
        archivo_ruta=archivo_ruta,
        archivo_drive_id=archivo_drive_id,
    )
    db.session.add(fila)
    return "nuevo", fila, (None, None)

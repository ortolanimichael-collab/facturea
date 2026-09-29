import os
import random
from datetime import datetime, timedelta

from automatizacion.arca_bot import concepto_efectivo
from lector import lector_core
from models import db, Comprobante, Empresa, Usuario, IdTransaccionFacturada
from almacenamiento import guardar_archivo_persistente

EXTENSIONES_VALIDAS = {"png", "jpg", "jpeg", "pdf"}

# Primeros bytes ("magic numbers") reales de cada formato soportado -- se
# usan para chequear que el CONTENIDO del archivo sea lo que dice ser su
# extensión, y no algo renombrado a mano (ej. un .html o un ejecutable
# subido como "foto.jpg") para intentar colarlo por acá.
_FIRMAS_VALIDAS = {
    "pdf": (b"%PDF",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
}


def _contenido_coincide_con_extension(ruta_local, ext):
    firmas = _FIRMAS_VALIDAS.get(ext)
    if not firmas:
        return False
    try:
        with open(ruta_local, "rb") as f:
            cabecera = f.read(16)
    except OSError:
        return False
    return any(cabecera.startswith(firma) for firma in firmas)


def _estado_inicial_para_transaccion(id_transaccion, empresa_id):
    """
    Decide qué hacer con una transacción (Mercado Pago, Payway, Banco
    Galicia o NAVE) antes de armar su Comprobante. Devuelve una tupla
    (crear, estado_inicial, facturado_en_previo):

    - Si YA existe un Comprobante con este id_transaccion en la tabla (en
      cualquier estado: pendiente, error o facturado), no hay que crear
      otro -- (False, None, None).
    - Si no existe un Comprobante pero SÍ hay un registro permanente en
      IdTransaccionFacturada (ver models.py) de que esta transacción ya se
      facturó antes -- el comprobante original se borró después, ej. con
      "Eliminar todos los archivos" -- se crea igual el Comprobante para
      que no desaparezca del historial, pero directamente en estado
      "facturado" (con la fecha real en la que se había facturado), NO
      "pendiente" -- si quedara "pendiente" volvería a aparecer en
      "Facturar todo" y se duplicaría la factura real ya emitida en ARCA.
      -- (True, "facturado", esa fecha).
    - Si no hay ningún rastro de esta transacción, es realmente nueva --
      (True, "pendiente", None).
    """
    if Comprobante.query.filter_by(id_transaccion=id_transaccion, empresa_id=empresa_id).first():
        return False, None, None
    registro_previo = IdTransaccionFacturada.query.filter_by(
        id_transaccion=id_transaccion, empresa_id=empresa_id,
    ).first()
    if registro_previo:
        return True, "facturado", registro_previo.facturado_en
    return True, "pendiente", None


def _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida):
    """
    Punto de venta y tipo de comprobante con los que se arma un Comprobante
    nuevo para `descripcion_elegida`: si esa descripción tiene su propio
    punto de venta y/o tipo de comprobante asignado en "Editar empresa"
    (solo disponible para Responsable Inscripto -- ver
    Empresa.punto_venta_para_descripcion / tipo_comprobante_para_descripcion
    en models.py), se usa ese; si no, se cae al default de siempre de la
    empresa (config_punto_venta / el primero de config_tipo_comprobante).
    """
    punto_venta = empresa.punto_venta_para_descripcion(descripcion_elegida) or empresa.config_punto_venta
    tipo_comprobante = (
        empresa.tipo_comprobante_para_descripcion(descripcion_elegida)
        or (empresa.config_tipo_comprobante or "").split(",")[0]
    )
    return punto_venta, tipo_comprobante


def _elegir_descripcion(empresa):
    """
    Elige la descripción para un comprobante nuevo -- reemplaza el bloque
    "if config_descripcion_aleatoria: ... random.choice(...)" que antes
    estaba repetido en cada función de creación de más abajo. Sigue el modo
    configurado:

    - Si "elegir al azar" (config_descripcion_aleatoria) está desactivado,
      o no hay ninguna descripción cargada: siempre la default de la
      empresa (config_producto_servicio -- la primera que se cargó).
    - Si está activado y hay un reparto por % cargado (pestaña
      Configuraciones, ver descripciones_porcentajes en models.py): sorteo
      PESADO por esos porcentajes -- una descripción con 70% sale
      aproximadamente 7 de cada 10 veces.
    - Si está activado pero no hay porcentajes cargados (o suman 0): sorteo
      parejo entre todas las cargadas, como funcionaba antes de que
      existiera el reparto por %.

    Si la regla de "monto bajo" está activa (config_umbral_precio_bajo +
    config_descripcion_precio_bajo cargados, ver _aplicar_regla_monto_bajo),
    la descripción especial de esa regla se saca del sorteo -- es una
    descripción RESERVADA para los montos bajos, así que no debe poder
    tocarle por azar a un comprobante de cualquier monto (antes sí podía,
    porque seguía siendo una opción más de la lista normal). Si sacarla deja
    la lista vacía, se la deja igual -- mejor que sortear entre nada.
    """
    descripciones = [d.strip() for d in (empresa.descripciones_disponibles or "").split(",") if d.strip()]
    if not empresa.config_descripcion_aleatoria or not descripciones:
        return empresa.config_producto_servicio

    porcentajes_crudos = (empresa.descripciones_porcentajes or "").split(",")
    pesos = []
    for i in range(len(descripciones)):
        try:
            peso = float(porcentajes_crudos[i]) if i < len(porcentajes_crudos) and porcentajes_crudos[i].strip() else 0.0
        except ValueError:
            peso = 0.0
        pesos.append(max(peso, 0.0))

    descripcion_especial = (empresa.config_descripcion_precio_bajo or "").strip()
    if empresa.config_umbral_precio_bajo is not None and descripcion_especial in descripciones:
        indice_especial = descripciones.index(descripcion_especial)
        descripciones_sin_especial = descripciones[:indice_especial] + descripciones[indice_especial + 1:]
        pesos_sin_especial = pesos[:indice_especial] + pesos[indice_especial + 1:]
        if descripciones_sin_especial:  # si era la única cargada, se la deja -- no hay entre qué elegir
            descripciones, pesos = descripciones_sin_especial, pesos_sin_especial

    if sum(pesos) > 0:
        return random.choices(descripciones, weights=pesos, k=1)[0]
    return random.choice(descripciones)


def _aplicar_regla_monto_bajo(empresa, descripcion_elegida, monto):
    """
    Regla de "monto bajo" (pestaña Configuraciones): si la empresa tiene
    cargado un umbral (config_umbral_precio_bajo) y una descripción
    especial para eso (config_descripcion_precio_bajo), y este comprobante
    no llega a ese umbral, se IGNORA la descripción que le hubiera tocado
    por _elegir_descripcion (azar/reparto/default) y se usa siempre esa
    descripción especial -- con su propia alícuota/tipo de
    comprobante/punto de venta si los tiene asignados (ver
    _punto_venta_y_tipo_comprobante_para, que se llama DESPUÉS de esto en
    cada función de creación, ya con la descripción final).

    Devuelve la descripción sin cambios si la regla no está cargada, o si
    el monto no calificó.
    """
    umbral = empresa.config_umbral_precio_bajo
    descripcion_especial = (empresa.config_descripcion_precio_bajo or "").strip()
    if umbral is None or not descripcion_especial:
        return descripcion_elegida
    if monto is not None and monto < umbral:
        return descripcion_especial
    return descripcion_elegida


def aplicar_regla_monto_bajo_retroactiva(empresa):
    """
    _aplicar_regla_monto_bajo (arriba) solo se aplica a los comprobantes que
    se crean DE ACÁ EN ADELANTE -- guardar la regla en la pestaña
    Configuraciones no tocaba los comprobantes que ya estaban cargados en la
    tabla desde antes. Esta función corrige eso de una, en los dos sentidos,
    para todos los comprobantes PENDIENTES (nunca los ya facturados):

    1. Los que califican por monto (precio_unitario menor al umbral) pasan a
       tener la descripción especial, con su propia alícuota/tipo de
       comprobante/punto de venta si los tiene -- mismo criterio que un
       comprobante nuevo.
    2. Los que YA tenían cargada la descripción especial pero su monto está
       en el umbral o por arriba (quedó de una carga vieja, de antes de que
       existiera esta regla, o de un sorteo al azar de cuando esa
       descripción todavía no estaba reservada -- ver _elegir_descripcion)
       se les asigna otra descripción, porque ahora esa es exclusiva de los
       montos bajos y no debe repetirse en montos que no calificaron.

    Nunca toca un comprobante con precio_unitario en 0 -- ese es el
    placeholder que deja "cargar a mano" antes de completar el importe real
    (ver registro_agregar_manual en app.py), así que ni se le fuerza la
    descripción especial ni se le saca si por algún motivo ya la tenía.

    Devuelve la cantidad de comprobantes que cambiaron.
    """
    umbral = empresa.config_umbral_precio_bajo
    descripcion_especial = (empresa.config_descripcion_precio_bajo or "").strip()
    if umbral is None or not descripcion_especial:
        return 0

    punto_venta_especial, tipo_comprobante_especial = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_especial)
    alicuota_especial = empresa.alicuota_para_descripcion(descripcion_especial)
    if alicuota_especial is None:
        alicuota_especial = (empresa.config_alicuota_iva or "").split(",")[0] or None

    cantidad_cambiada = 0

    # 1. Monto bajo -> descripción especial.
    pendientes_bajos = Comprobante.query.filter(
        Comprobante.empresa_id == empresa.id,
        Comprobante.estado.in_(("pendiente", "error")),
        Comprobante.precio_unitario > 0,
        Comprobante.precio_unitario < umbral,
    ).all()
    for c in pendientes_bajos:
        if (
            c.descripcion == descripcion_especial
            and c.punto_venta == punto_venta_especial
            and c.tipo_comprobante == tipo_comprobante_especial
            and c.alicuota_iva == alicuota_especial
        ):
            continue  # ya estaba así -- no cuenta como cambio
        c.descripcion = descripcion_especial
        c.punto_venta = punto_venta_especial
        c.tipo_comprobante = tipo_comprobante_especial
        c.alicuota_iva = alicuota_especial
        cantidad_cambiada += 1

    # 2. Descripción especial pero monto que YA NO califica -> se le sortea
    # otra descripción normal (_elegir_descripcion ya excluye la especial
    # sola mientras esta regla esté activa, ver arriba).
    pendientes_altos_con_especial = Comprobante.query.filter(
        Comprobante.empresa_id == empresa.id,
        Comprobante.estado.in_(("pendiente", "error")),
        Comprobante.descripcion == descripcion_especial,
        Comprobante.precio_unitario >= umbral,
    ).all()
    for c in pendientes_altos_con_especial:
        nueva_descripcion = _elegir_descripcion(empresa)
        if nueva_descripcion == descripcion_especial:
            # No quedó ninguna otra descripción cargada para sortear (ver
            # _elegir_descripcion) -- se la deja como está en vez de dejarla
            # sin ninguna descripción.
            continue
        nuevo_punto_venta, nuevo_tipo_comprobante = _punto_venta_y_tipo_comprobante_para(empresa, nueva_descripcion)
        nueva_alicuota = empresa.alicuota_para_descripcion(nueva_descripcion)
        if nueva_alicuota is None:
            nueva_alicuota = (empresa.config_alicuota_iva or "").split(",")[0] or None
        c.descripcion = nueva_descripcion
        c.punto_venta = nuevo_punto_venta
        c.tipo_comprobante = nuevo_tipo_comprobante
        c.alicuota_iva = nueva_alicuota
        cantidad_cambiada += 1

    return cantidad_cambiada


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
    crear, estado_inicial, facturado_en_previo = _estado_inicial_para_transaccion(id_transaccion, empresa.id)
    if not crear:
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

    descripcion_elegida = _elegir_descripcion(empresa)
    descripcion_elegida = _aplicar_regla_monto_bajo(empresa, descripcion_elegida, importe_total)

    alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
    if alicuota_elegida is None:
        alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None
    punto_venta_elegido, tipo_comprobante_elegido = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida)

    payer = pago.get("payer") or {}
    nombre_pagador = (
        f"{payer.get('first_name', '')} {payer.get('last_name', '')}".strip()
        or payer.get("email") or None
    )

    # Tipo de documento del receptor: si Mercado Pago mandó la identificación
    # del pagador (payer.identification.type/number -- lo manda cuando el
    # comprador la cargó al pagar, ej. con QR o link de pago) y es CUIT o
    # CUIL, se usa ese dato tal cual. Si no, se factura como "DNI" sin
    # número -- el valor por defecto para un monotributista facturando a
    # Consumidor Final, que es el caso normal de un pago de Mercado Pago
    # (ARCA solo exige completar el número si el Tipo de documento elegido
    # es CUIT/CUIL, ver facturar_comprobante en arca_bot.py -- con "DNI"
    # puede quedar vacío sin problema).
    identificacion = payer.get("identification") or {}
    tipo_doc_mp = (identificacion.get("type") or "").strip().upper()
    numero_doc_mp = (identificacion.get("number") or "").strip()
    if tipo_doc_mp in ("CUIT", "CUIL") and numero_doc_mp:
        tipo_documento = tipo_doc_mp
        cuit_receptor = numero_doc_mp
    else:
        tipo_documento = "DNI"
        cuit_receptor = None

    # Últimos dígitos de la tarjeta (débito o crédito) que mandó Mercado
    # Pago -- igual que con el lector de imágenes, ARCA solo pide estos
    # últimos dígitos, nunca el número completo de la tarjeta (ver
    # zfill(20) en _completar_tarjeta, arca_bot.py).
    numero_pago_mp = None
    if medio_pago_detectado in ("Débito", "Crédito"):
        numero_pago_mp = ((pago.get("card") or {}).get("last_four_digits") or "").strip() or None

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,

        punto_venta=punto_venta_elegido,
        tipo_comprobante=tipo_comprobante_elegido,
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        alicuota_iva=alicuota_elegida,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=importe_total / cantidad,

        nombre_remitente=nombre_pagador,
        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=tipo_pago,
        tipo_pago_detalle=tipo_pago_detalle,
        numero_pago=numero_pago_mp,
        tipo_documento=tipo_documento,
        cuit_receptor=cuit_receptor,
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=condicion_venta_default,
        # Mismo criterio que la carga manual (ver comprobante_cargar_a_mano en
        # app.py): el período facturado "Desde/Hasta" que le pide ARCA para
        # servicios se carga igual a la fecha del comprobante -- antes acá
        # quedaban vacíos, lo que rompía la automatización para cualquier
        # empresa con concepto "Servicios" o "Productos y Servicios".
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        importe_total=importe_total,
        cantidad=cantidad,
        archivo_origen=f"Mercado Pago #{pago['id']}" + (" -- ya facturado anteriormente (registro recuperado)" if estado_inicial == "facturado" else ""),
        estado=estado_inicial,
        facturado_en=facturado_en_previo,
    )
    db.session.add(fila)
    return fila


# ---------- Payway (CSV de "Historial" / "Movimientos") ----------
# La MARCA que trae el CSV de Payway ya viene separada en Débito/Crédito
# (ej. "VISA DEBITO" vs "VISA" a secas para crédito), a diferencia de
# Mercado Pago que la manda por separado en payment_type_id -- por eso este
# mapa es propio, con las mismas reglas ya definidas para el resto del
# sistema: "Visa" a secas en Débito NO es "Visa Electrón" (un producto
# distinto), se carga como "Otra..." con detalle "VISA Débito".
MAPA_TARJETAS_PAYWAY = {
    "VISA DEBITO": ("Débito", "Otra...", "VISA Débito"),
    "MASTERCARD DEBITO": ("Débito", "Mastercard Débito", None),
    "MAESTRO": ("Débito", "Maestro", None),
    "CABAL DEBITO": ("Débito", "Cabal 24 hs", None),
    "VISA": ("Crédito", "Visa", None),
    "MASTERCARD": ("Crédito", "Mastercard", None),
    "AMEX": ("Crédito", "American Express", None),
    "AMERICAN EXPRESS": ("Crédito", "American Express", None),
    "CABAL": ("Crédito", "Cabal", None),
    "NARANJA": ("Crédito", "Tarjeta Naranja", None),
    "CENCOSUD": ("Crédito", "Tarjeta Shopping", None),
    "CORDIAL": ("Crédito", "Credencial", None),
}


def _mapear_medio_pago_payway(marca):
    """
    Traduce la columna MARCA del CSV de Payway (ej. "VISA DEBITO",
    "MASTERCARD") a (medio_pago_detectado, tipo_pago, tipo_pago_detalle) --
    mismo formato que ya usan el lector de imágenes y Mercado Pago. Si la
    marca no está en el mapa, se carga como "Otra..." con el nombre tal
    cual lo mandó Payway, para no inventar una marca que no es ni perder la
    fila.
    """
    marca_norm = (marca or "").strip().upper()
    if marca_norm in MAPA_TARJETAS_PAYWAY:
        medio_pago, tipo_pago, detalle = MAPA_TARJETAS_PAYWAY[marca_norm]
        return medio_pago, tipo_pago, detalle
    # Cualquier variante "<MARCA> DEBITO" que no esté mapeada explícitamente
    # se toma igual como Débito -- Payway es consistente con ese sufijo.
    if marca_norm.endswith(" DEBITO"):
        return "Débito", "Otra...", marca_norm
    return "Crédito", "Otra...", marca_norm or "Tarjeta"


def parsear_csv_payway(contenido_texto):
    """
    Parsea el contenido del CSV de "Historial"/"Movimientos" de Payway y
    devuelve una lista de diccionarios, uno por fila de venta. El archivo
    que exporta Payway NO es un CSV estándar desde la primera línea: la
    línea 1 es un título libre ("Detalle de Transacciones en pesos de
    Payway Desde: ... Hasta: ..."), y recién la línea 2 trae el encabezado
    real de columnas -- por eso se busca a mano la línea que empieza con
    "COMPRA," en vez de asumir que el encabezado está en la fila 0 (lo que
    haría fallar a csv.DictReader con las columnas corridas).

    Solo se quedan las filas con TIPO="Venta" -- Payway también puede listar
    anulaciones/contracargos ahí mismo, y facturarlos igual que una venta
    normal duplicaría el importe en la contabilidad del cliente.
    """
    import csv
    import io

    lineas = contenido_texto.splitlines()
    idx_encabezado = next(
        (i for i, linea in enumerate(lineas) if linea.strip().upper().startswith("COMPRA,")),
        None,
    )
    if idx_encabezado is None:
        raise ValueError(
            "No se encontró la fila de encabezados (\"COMPRA,PRESENTACION,...\") en el archivo -- "
            "¿es realmente un CSV de Historial/Movimientos de Payway?"
        )

    lector_csv = csv.DictReader(io.StringIO("\n".join(lineas[idx_encabezado:])))
    filas = []
    for fila in lector_csv:
        if not fila.get("COMPRA"):
            continue  # línea vacía al final del archivo
        if (fila.get("TIPO") or "").strip().lower() != "venta":
            continue
        filas.append(fila)
    return filas


def crear_comprobante_desde_fila_payway(fila_csv, usuario_id, empresa):
    """
    Arma un Comprobante "pendiente" a partir de una fila ya parseada del CSV
    de Payway (ver parsear_csv_payway) -- mismo criterio que
    crear_comprobante_desde_pago_mercadopago: nunca hay nombre ni
    identificación de quien pagó (un cupón de POS no lo trae, a diferencia
    de un pago de Mercado Pago con link/QR), así que factura siempre a
    Consumidor Final con "DNI" sin número. Devuelve el Comprobante nuevo, o
    None si esa venta ya se había traído antes (para no duplicarla).
    """
    establecimiento = (fila_csv.get("ESTABLECIMIENTO") or "").strip()
    lote = (fila_csv.get("LOTE") or "").strip()
    num_cupon = (fila_csv.get("NUM.CUPON") or "").strip()
    id_transaccion = f"PAYWAY-{establecimiento}-{lote}-{num_cupon}"
    crear, estado_inicial, facturado_en_previo = _estado_inicial_para_transaccion(id_transaccion, empresa.id)
    if not crear:
        return None

    try:
        importe_total = float((fila_csv.get("MONTO_BRUTO") or "0").replace(",", ""))
    except ValueError:
        importe_total = 0.0
    cantidad = 1.0

    medio_pago_detectado, tipo_pago, tipo_pago_detalle = _mapear_medio_pago_payway(fila_csv.get("MARCA"))

    if medio_pago_detectado == "Débito":
        condicion_venta_default = "Tarjeta de Débito"
    elif medio_pago_detectado == "Crédito":
        condicion_venta_default = "Tarjeta de Crédito"
    else:
        condicion_venta_default = (empresa.config_condicion_venta or "").split(",")[0]

    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_compra_str = (fila_csv.get("COMPRA") or "").strip()
    try:
        fecha_comprobante = datetime.strptime(fecha_compra_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except ValueError:
        fecha_comprobante = (datetime.now() - timedelta(days=dias_atras)).strftime("%d/%m/%Y")

    descripcion_elegida = _elegir_descripcion(empresa)
    descripcion_elegida = _aplicar_regla_monto_bajo(empresa, descripcion_elegida, importe_total)
    punto_venta_elegido, tipo_comprobante_elegido = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida)
    alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
    if alicuota_elegida is None:
        alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    # Últimos dígitos de la tarjeta -- Payway ya los manda enmascarados
    # ("************1518"), así que se toman tal cual vienen (los últimos
    # 4 números, sin los asteriscos).
    num_tarjeta_crudo = (fila_csv.get("NUM.TARJETA") or "").strip()
    numero_pago = None
    if medio_pago_detectado in ("Débito", "Crédito"):
        solo_digitos = "".join(c for c in num_tarjeta_crudo if c.isdigit())
        numero_pago = solo_digitos[-4:] if solo_digitos else None

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,

        punto_venta=punto_venta_elegido,
        tipo_comprobante=tipo_comprobante_elegido,
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        alicuota_iva=alicuota_elegida,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=importe_total / cantidad,

        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=tipo_pago,
        tipo_pago_detalle=tipo_pago_detalle,
        numero_pago=numero_pago,
        # Un cupón de POS nunca trae CUIT/CUIL del comprador -- se factura
        # a Consumidor Final con DNI (ARCA no exige el número con ese tipo
        # de documento), igual que un pago de Mercado Pago sin identificación.
        tipo_documento="DNI",
        cuit_receptor=None,
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=condicion_venta_default,
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        importe_total=importe_total,
        cantidad=cantidad,
        archivo_origen=f"Payway cupón #{num_cupon} (lote {lote})" + (" -- ya facturado anteriormente (registro recuperado)" if estado_inicial == "facturado" else ""),
        estado=estado_inicial,
        facturado_en=facturado_en_previo,
    )
    db.session.add(fila)
    return fila


# ---------- Banco Galicia (Excel de "Cuentas" / movimientos de home banking) ----------
# A diferencia de Mercado Pago y Payway, el resumen de cuenta de Galicia SÍ
# trae el CUIT/CUIL de quien transfirió en casi todos los casos (viene
# adentro del bloque de texto de la columna "Movimiento") -- así que acá
# se factura con ese CUIT real en vez de "DNI" genérico, salvo que la
# transferencia no traiga ninguno.
TIPOS_MOVIMIENTO_GALICIA_VENTA = {"TRANSFERENCIA DE TERCEROS", "CREDITO TRANSFERENCIA COELSA"}
# Tipos que aparecen en el mismo extracto pero NO son una venta -- se
# ignoran aunque tengan importe en la columna Crédito: intereses que paga
# el banco, y transferencias que el propio dueño de la cuenta se hizo a sí
# mismo entre sus propias cuentas (no es un cobro de un cliente).
TIPOS_MOVIMIENTO_GALICIA_IGNORAR = {"INTERES CAPITALIZADO", "TRANSFERENCIA DE CUENTA PROPIA"}


def _parsear_monto_ar(texto):
    """Convierte un monto en formato argentino ("25.000,00") a float. Devuelve 0.0 si no se puede."""
    texto = (texto or "").strip()
    if not texto:
        return 0.0
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def parsear_excel_galicia(archivo_like):
    """
    Parsea el Excel que exporta el home banking de Banco Galicia (pestaña
    "Cuentas") y devuelve una lista de diccionarios, uno por movimiento que
    representa un cobro real de un tercero. `archivo_like` puede ser una
    ruta de archivo o un objeto tipo archivo (ej. un BytesIO del upload).

    El archivo no es una tabla limpia desde la fila 1: las primeras filas
    son encabezado libre del banco (nombre de cuenta, número, fecha/hora de
    generación, rango de fechas consultado) -- recién más abajo aparece la
    fila real de columnas ("Fecha", "Movimiento", "Débito", "Crédito",
    "Saldo Parcial", "Comentarios"), así que se busca esa fila a mano en vez
    de asumir que está en la posición 1, igual que se hace con el CSV de
    Payway (ahí el problema es el mismo: encabezado libre antes del real).

    Cada celda "Movimiento" es un bloque de texto de varias líneas: la
    primera dice el TIPO de movimiento (con eso se filtran intereses y
    transferencias entre cuentas propias, ver TIPOS_MOVIMIENTO_GALICIA_*),
    la segunda casi siempre es el nombre de quien transfirió, y en alguna
    línea más abajo aparece su CUIT/CUIL como un número de exactamente 11
    dígitos solo (se lo distingue de un CBU, que tiene 22).
    """
    import re
    import openpyxl

    libro = openpyxl.load_workbook(archivo_like, data_only=True)
    hoja = libro.worksheets[0]

    filas_crudas = list(hoja.iter_rows(values_only=True))
    idx_encabezado = next(
        (i for i, fila in enumerate(filas_crudas) if (fila[0] or "").strip().lower() == "fecha"),
        None,
    ) if filas_crudas else None
    if idx_encabezado is None:
        raise ValueError(
            "No se encontró la fila de encabezados (\"Fecha, Movimiento, Débito, Crédito...\") en el "
            "archivo -- ¿es realmente el Excel de movimientos/cuentas de Banco Galicia?"
        )

    resultado = []
    for fila in filas_crudas[idx_encabezado + 1:]:
        fecha_celda = fila[0]
        if not fecha_celda:
            continue  # fila vacía (puede haber alguna al final)

        movimiento_texto = (fila[1] or "").strip()
        lineas = [l.strip() for l in movimiento_texto.split("\n") if l.strip()]
        if not lineas:
            continue
        tipo_movimiento = lineas[0].upper()
        if tipo_movimiento in TIPOS_MOVIMIENTO_GALICIA_IGNORAR:
            continue
        if tipo_movimiento not in TIPOS_MOVIMIENTO_GALICIA_VENTA:
            continue  # tipo no reconocido -- se prefiere no facturar algo que no se sabe bien qué es

        credito = _parsear_monto_ar(fila[3] if len(fila) > 3 else None)
        if credito <= 0:
            continue  # no es plata que entró (o es un débito, columna 2)

        nombre = lineas[1] if len(lineas) > 1 else None
        cuit = next((l for l in lineas[1:] if re.fullmatch(r"\d{11}", l)), None)

        # La fecha puede venir como texto "28/09/2026" o como objeto date/
        # datetime real, según cómo Excel haya guardado la celda.
        if isinstance(fecha_celda, str):
            fecha_str = fecha_celda.strip()
        else:
            fecha_str = fecha_celda.strftime("%d/%m/%Y")

        resultado.append({
            "fecha": fecha_str,
            "monto": credito,
            "nombre": nombre,
            "cuit": cuit,
            "movimiento_texto": movimiento_texto,
        })
    return resultado


def crear_comprobante_desde_transferencia_galicia(fila_galicia, usuario_id, empresa):
    """
    Arma un Comprobante "pendiente" a partir de una transferencia ya
    parseada del Excel de Banco Galicia (ver parsear_excel_galicia).
    Devuelve el Comprobante nuevo, o None si esa transferencia ya se había
    traído antes (para no duplicarla).

    Como el extracto no trae ningún número de operación aparte, el ID único
    para detectar duplicados se arma con un hash del bloque de texto
    completo de "Movimiento" (que ya trae varios datos que lo hacen único:
    CBU, nombre, referencias internas del banco) junto con la fecha y el
    monto -- así, aunque el cliente vuelva a exportar el mismo rango de
    fechas superpuesto con una importación anterior, no se duplican las
    transferencias ya cargadas.
    """
    import hashlib

    fecha_comprobante = fila_galicia["fecha"]
    monto = fila_galicia["monto"]
    huella = hashlib.sha1(fila_galicia["movimiento_texto"].encode("utf-8")).hexdigest()[:16]
    id_transaccion = f"GALICIA-{fecha_comprobante.replace('/', '')}-{monto:.2f}-{huella}"
    crear, estado_inicial, facturado_en_previo = _estado_inicial_para_transaccion(id_transaccion, empresa.id)
    if not crear:
        return None

    cantidad = 1.0
    dias_atras = empresa.config_dias_atras_fecha_emision or 10

    descripcion_elegida = _elegir_descripcion(empresa)
    descripcion_elegida = _aplicar_regla_monto_bajo(empresa, descripcion_elegida, monto)
    punto_venta_elegido, tipo_comprobante_elegido = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida)
    alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
    if alicuota_elegida is None:
        alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    cuit = fila_galicia.get("cuit")
    nombre = fila_galicia.get("nombre")
    if cuit:
        tipo_documento = "CUIT"
        cuit_receptor = cuit
    else:
        # Muy raro (en la práctica casi todas las transferencias de
        # terceros de Galicia traen CUIT), pero por las dudas: sin CUIT,
        # se factura igual a Consumidor Final con DNI, como con Payway.
        tipo_documento = "DNI"
        cuit_receptor = None

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,

        punto_venta=punto_venta_elegido,
        tipo_comprobante=tipo_comprobante_elegido,
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        alicuota_iva=alicuota_elegida,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=monto / cantidad,

        nombre_remitente=nombre,
        # A diferencia de Mercado Pago/Payway, acá SÍ tenemos el nombre real
        # de quien pagó -- se lo pone también en "Recibe" (nombre_razon_social)
        # para que la tabla lo muestre en vez de "No detectado". No hace
        # falta mandárselo a ARCA: con el CUIT cargado, ARCA busca la razón
        # social sola (ver facturar_comprobante, arca_bot.py).
        nombre_razon_social=nombre,
        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado="Transferencia",
        tipo_documento=tipo_documento,
        cuit_receptor=cuit_receptor,
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=(empresa.config_condicion_venta or "").split(",")[0] if empresa.config_condicion_venta else "",
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        importe_total=monto,
        cantidad=cantidad,
        archivo_origen=(
            f"Transferencia Galicia -- {nombre or 'sin nombre'}"
            + (f" (CUIT {cuit})" if cuit else "")
            + (" -- ya facturado anteriormente (registro recuperado)" if estado_inicial == "facturado" else "")
        ),
        estado=estado_inicial,
        facturado_en=facturado_en_previo,
    )
    db.session.add(fila)
    return fila


# ---------- NAVE (Excel de "Informe de detalles - Cobros con QR") ----------
# Igual que Banco Galicia, el informe de NAVE trae el CUIT/CUIL/DNI de quien
# pagó en una columna propia (no hay que adivinarlo de un bloque de texto),
# así que acá también se factura con el documento real casi siempre.
MAPA_MEDIOS_NAVE = {
    # Débito
    "VISA DÉBITO": ("Débito", "Otra...", "VISA Débito"),  # "Visa" a secas en Débito no es una opción real de ARCA
    "MASTERCARD DÉBITO": ("Débito", "Mastercard Débito", None),
    "MASTERCARD PREPAGA": ("Débito", "Mastercard Débito", None),  # prepaga funciona como débito para ARCA
    "MAESTRO": ("Débito", "Maestro", None),
    "CABAL DÉBITO": ("Débito", "Cabal 24 hs", None),
    # Crédito
    "VISA CRÉDITO": ("Crédito", "Visa", None),
    "MASTERCARD CRÉDITO": ("Crédito", "Mastercard", None),
    "AMERICAN EXPRESS": ("Crédito", "American Express", None),
    "CABAL CRÉDITO": ("Crédito", "Cabal", None),
    "NARANJA CRÉDITO": ("Crédito", "Tarjeta Naranja", None),
    "CENCOSUD": ("Crédito", "Tarjeta Shopping", None),
    "CORDIAL": ("Crédito", "Credencial", None),
    # Dinero en cuenta / QR sin tarjeta asociada -- es plata que entra
    # directo a la cuenta, igual que "account_money" en Mercado Pago, así
    # que se carga como Transferencia.
    "DINERO EN CUENTA": ("Transferencia", None, None),
}


def _mapear_medio_pago_nave(medio_de_pago):
    """
    Traduce la columna "Medio de Pago" del informe de NAVE (ej. "Visa
    Débito", "Dinero en cuenta") a (medio_pago_detectado, tipo_pago,
    tipo_pago_detalle) -- mismo formato que el resto de las fuentes. Si no
    está en el mapa, se decide por el sufijo (" DÉBITO"/" CRÉDITO") si lo
    tiene, o se carga como Transferencia si no se puede saber qué es (mejor
    no inventar una tarjeta que no es).
    """
    medio_norm = (medio_de_pago or "").strip().upper()
    if medio_norm in MAPA_MEDIOS_NAVE:
        return MAPA_MEDIOS_NAVE[medio_norm]
    if medio_norm.endswith(" DÉBITO") or medio_norm.endswith(" DEBITO"):
        return "Débito", "Otra...", medio_norm
    if medio_norm.endswith(" CRÉDITO") or medio_norm.endswith(" CREDITO"):
        return "Crédito", "Otra...", medio_norm
    return "Transferencia", None, None


def parsear_excel_nave(archivo_like):
    """
    Parsea el Excel "Informe de detalles - Cobros con QR" de NAVE y devuelve
    una lista de diccionarios, uno por cobro acreditado. `archivo_like`
    puede ser una ruta de archivo o un objeto tipo archivo (ej. un BytesIO
    del upload).

    El archivo tiene unas 20 filas de encabezado libre (título del informe,
    nombre y CUIT del titular, fecha/hora de consulta, totales) antes de la
    fila real de columnas ("Fecha de operación", "Fecha de acreditación",
    ..., "CUIT/CUIL/DNI", "Medio de Pago", "Monto bruto", ..., "Estado",
    ...), así que se la busca a mano en vez de asumir que está en una
    posición fija, igual que con Payway y Banco Galicia.
    """
    import openpyxl

    libro = openpyxl.load_workbook(archivo_like, data_only=True)
    hoja = libro.worksheets[0]

    filas_crudas = list(hoja.iter_rows(values_only=True))
    idx_encabezado = next(
        (i for i, fila in enumerate(filas_crudas)
         if fila and (fila[0] or "").strip().lower() == "fecha de operación"),
        None,
    ) if filas_crudas else None
    if idx_encabezado is None:
        raise ValueError(
            "No se encontró la fila de encabezados (\"Fecha de operación, Fecha de acreditación...\") en "
            "el archivo -- ¿es realmente el informe de detalles de cobros con QR de NAVE?"
        )

    resultado = []
    for fila in filas_crudas[idx_encabezado + 1:]:
        fecha_celda = fila[0] if len(fila) > 0 else None
        if not fecha_celda:
            continue  # fila vacía (puede haber alguna al final, ej. la de totales)

        estado = (fila[13] if len(fila) > 13 else "") or ""
        if estado.strip().lower() != "acreditado":
            continue  # se ignoran cobros rechazados/pendientes/anulados, etc.

        # "Fecha de operación" viene como texto "01/09/2026 10:16" -- se toma
        # solo la parte de la fecha.
        fecha_texto = str(fecha_celda).strip()
        fecha_str = fecha_texto.split(" ")[0]

        codigo_operacion = (fila[2] if len(fila) > 2 else "") or ""
        nombre = (fila[6] if len(fila) > 6 else "") or ""
        documento = (fila[7] if len(fila) > 7 else "") or ""
        medio_de_pago = (fila[8] if len(fila) > 8 else "") or ""
        monto_bruto = fila[9] if len(fila) > 9 else None

        try:
            monto = float(monto_bruto)
        except (TypeError, ValueError):
            continue
        if monto <= 0:
            continue

        resultado.append({
            "fecha": fecha_str,
            "monto": monto,
            "nombre": nombre.strip() or None,
            "documento": documento.strip() or None,
            "medio_de_pago": medio_de_pago.strip(),
            "codigo_operacion": codigo_operacion.strip(),
        })
    return resultado


def crear_comprobante_desde_cobro_nave(fila_nave, usuario_id, empresa):
    """
    Arma un Comprobante "pendiente" a partir de un cobro ya parseado del
    informe de NAVE (ver parsear_excel_nave). Devuelve el Comprobante
    nuevo, o None si ese cobro ya se había traído antes (para no
    duplicarlo) -- acá sí hay un identificador único real (Código de
    operación), a diferencia de Banco Galicia.
    """
    id_transaccion = f"NAVE-{fila_nave['codigo_operacion']}"
    crear, estado_inicial, facturado_en_previo = _estado_inicial_para_transaccion(id_transaccion, empresa.id)
    if not crear:
        return None

    fecha_comprobante = fila_nave["fecha"]
    monto = fila_nave["monto"]
    cantidad = 1.0
    dias_atras = empresa.config_dias_atras_fecha_emision or 10

    medio_pago_detectado, tipo_pago, tipo_pago_detalle = _mapear_medio_pago_nave(fila_nave["medio_de_pago"])

    # NAVE nunca manda el número de tarjeta (ni siquiera enmascarado, a
    # diferencia de Payway) porque el cobro se hace con QR, no pasando una
    # tarjeta física -- aunque el "Medio de Pago" diga "Visa Crédito" o
    # "Mastercard Débito", no hay ninguna tarjeta real asociada al cobro que
    # se pueda cargar en ARCA. Sin un número que lo respalde, se factura
    # como Transferencia en vez de Débito/Crédito -- mismo criterio que ya
    # se usaba para "Dinero en cuenta" (el otro caso sin tarjeta de por
    # medio), solo que ahora se aplica a cualquier medio sin número, no
    # nada más a ese texto puntual.
    numero_pago = None  # NAVE no informa esto en ningún caso
    if medio_pago_detectado in ("Débito", "Crédito") and not numero_pago:
        medio_pago_detectado = "Transferencia"
        tipo_pago = None
        tipo_pago_detalle = None

    if medio_pago_detectado == "Débito":
        condicion_venta_default = "Tarjeta de Débito"
    elif medio_pago_detectado == "Crédito":
        condicion_venta_default = "Tarjeta de Crédito"
    else:
        condicion_venta_default = (empresa.config_condicion_venta or "").split(",")[0] if empresa.config_condicion_venta else ""

    descripcion_elegida = _elegir_descripcion(empresa)
    descripcion_elegida = _aplicar_regla_monto_bajo(empresa, descripcion_elegida, monto)
    punto_venta_elegido, tipo_comprobante_elegido = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida)
    alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
    if alicuota_elegida is None:
        alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    # El documento viene en su propia columna, sin ambigüedad: 11 dígitos es
    # CUIT/CUIL, cualquier otra longitud (normalmente 7-8) es DNI. Antes,
    # cuando era DNI, no se guardaba ningún número (quedaba "—" en la tabla
    # aunque el informe de NAVE sí lo traía) -- ahora se guarda igual en
    # cuit_receptor para que se vea en la columna "CUIL/CUIT/Documento",
    # aunque ARCA no lo exija con tipo de documento "DNI".
    documento = fila_nave.get("documento")
    if documento and len(documento) == 11 and documento.isdigit():
        tipo_documento = "CUIT"
    else:
        tipo_documento = "DNI"
    cuit_receptor = documento or None

    nombre = fila_nave.get("nombre")

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,

        punto_venta=punto_venta_elegido,
        tipo_comprobante=tipo_comprobante_elegido,
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        alicuota_iva=alicuota_elegida,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=monto / cantidad,

        nombre_remitente=nombre,
        nombre_razon_social=nombre,
        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=tipo_pago,
        tipo_pago_detalle=tipo_pago_detalle,
        numero_pago=numero_pago,
        tipo_documento=tipo_documento,
        cuit_receptor=cuit_receptor,
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=condicion_venta_default,
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        importe_total=monto,
        cantidad=cantidad,
        archivo_origen=(
            f"NAVE -- {nombre or 'sin nombre'}"
            + (f" (doc. {documento})" if documento else "")
            + (" -- ya facturado anteriormente (registro recuperado)" if estado_inicial == "facturado" else "")
        ),
        estado=estado_inicial,
        facturado_en=facturado_en_previo,
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
    if ext not in EXTENSIONES_VALIDAS or not _contenido_coincide_con_extension(ruta_local, ext):
        return "ignorado", None, (None, None)

    # Límite mensual de comprobantes del plan (ver PLANES en models.py) --
    # se chequea ACÁ, antes de gastar tiempo de OCR, porque este es el único
    # lugar por el que pasan tanto la subida manual (api_subir) como la
    # sincronización con Google Drive (drive_sync.sincronizar_carpeta).
    usuario = db.session.get(Usuario, usuario_id)
    if usuario and not usuario.le_queda_cupo_mensual():
        return "limite", None, (None, None)

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
    descripcion_elegida = _elegir_descripcion(empresa) if empresa else None
    if empresa:
        descripcion_elegida = _aplicar_regla_monto_bajo(empresa, descripcion_elegida, importe_total)

    # Si esa descripción tiene una alícuota propia cargada (ej. "Embutidos"
    # -> 10.5%, para un Responsable Inscripto que vende cosas con distinta
    # alícuota), se usa esa -- si no, se cae al default general de la
    # empresa (la primera de la lista que haya marcado).
    alicuota_elegida = None
    if empresa:
        alicuota_elegida = empresa.alicuota_para_descripcion(descripcion_elegida)
        if alicuota_elegida is None:
            alicuota_elegida = (empresa.config_alicuota_iva or "").split(",")[0] or None

    punto_venta_elegido, tipo_comprobante_elegido = (None, None)
    if empresa:
        punto_venta_elegido, tipo_comprobante_elegido = _punto_venta_y_tipo_comprobante_para(empresa, descripcion_elegida)

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa_id,
        drive_file_id=drive_file_id,
        id_transaccion=datos.get("ID_Transaccion"),

        punto_venta=punto_venta_elegido,
        tipo_comprobante=tipo_comprobante_elegido,
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

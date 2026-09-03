import os
import random
from datetime import datetime, timedelta

from lector import lector_core
from models import db, Comprobante, Empresa
from almacenamiento import guardar_archivo_persistente

EXTENSIONES_VALIDAS = {"png", "jpg", "jpeg", "pdf"}


def procesar_archivo(ruta_local, nombre_original, usuario_id, empresa_id, fecha_interfaz, cuit_propio_cliente="", drive_file_id=None):
    """
    Corre un archivo ya descargado/subido a disco por el lector, y si los datos
    son válidos y no están duplicados, lo guarda en la base para esa empresa.
    Devuelve una tupla (resultado, comprobante_relacionado, info_archivo_intento):
      - resultado: "nuevo" | "duplicado" | "error" | "ignorado"
      - comprobante_relacionado: si es "nuevo", el comprobante recién creado;
        si es "duplicado", el comprobante ORIGINAL ya existente al que corresponde
        (para poder comparar las dos imágenes); si no, None.
      - info_archivo_intento: solo para "duplicado" -- una tupla
        (archivo_ruta, archivo_drive_id) de DÓNDE quedó guardada la imagen de
        ESTE intento (distinta de la del original), para poder mostrar las
        dos una al lado de la otra. (None, None) en los demás casos.

    Los campos de facturación (tipo de comprobante, condición de IVA, etc.)
    arrancan con el valor configurado en la Empresa, pero quedan grabados en
    el propio comprobante -- de ahí en más son editables sin afectar a las
    demás facturas de esa empresa.
    """
    ext = nombre_original.lower().split(".")[-1]
    if ext not in EXTENSIONES_VALIDAS:
        return "ignorado", None, (None, None)

    if ext == "pdf":
        datos = lector_core.extraer_datos_de_pdf(ruta_local, fecha_interfaz, cuit_propio_cliente)
    else:
        datos = lector_core.extraer_datos_de_imagen(ruta_local, fecha_interfaz, cuit_propio_cliente)

    if not datos:
        return "error", None, (None, None)

    empresa = Empresa.query.get(empresa_id)

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

    fila = Comprobante(
        usuario_id=usuario_id,
        empresa_id=empresa_id,
        drive_file_id=drive_file_id,
        id_transaccion=datos.get("ID_Transaccion"),

        punto_venta=empresa.config_punto_venta if empresa else None,
        tipo_comprobante=empresa.config_tipo_comprobante if empresa else None,
        concepto=empresa.config_concepto if empresa else None,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida if empresa else None,
        precio_unitario=importe_total / cantidad,

        tipo_documento=datos.get("Tipo Documento"),
        cuit_receptor=str(datos.get("CUIT Receptor") or ""),
        nombre_razon_social=datos.get("Nombre / Razón Social"),
        nombre_remitente=datos.get("Nombre Remitente"),
        fecha_comprobante=fecha_comprobante_detectada,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=datos.get("Tipo Pago"),
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

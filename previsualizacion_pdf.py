"""
Genera un PDF de "modelo" para que el cliente vea cómo va a quedar una
factura ANTES de mandarla de verdad a ARCA -- con el mismo diseño que una
factura C real de ARCA (calcado de un comprobante real ya emitido), para
que la comparación sea directa.

No tiene validez fiscal: no existe CAE todavía (eso solo lo genera ARCA al
confirmar de verdad), así que esos campos se muestran como pendientes y se
agrega una marca de "MODELO" bien visible.
"""
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from automatizacion.arca_bot import calcular_fecha_facturacion, descomponer_neto_iva

CONCEPTOS = {"1": "Productos", "2": "Servicios", "3": "Productos y Servicios (Mixto)"}

# Códigos de tipo de comprobante de AFIP/ARCA -- son públicos y estables
# (aparecen en cualquier factura real), no algo que dependa de esta cuenta
# en particular. Cubre los tipos que Facturea ya puede emitir hoy (A/B/C
# más Notas de Débito/Crédito y Recibos); si el tipo no está en el mapa, se
# muestra "-" en vez de inventar un código.
CODIGOS_AFIP = {
    "FACTURA A": "001", "NOTA DE DÉBITO A": "002", "NOTA DE CRÉDITO A": "003", "RECIBO A": "004",
    "FACTURA B": "006", "NOTA DE DÉBITO B": "007", "NOTA DE CRÉDITO B": "008", "RECIBO B": "009",
    "FACTURA C": "011", "NOTA DE DÉBITO C": "012", "NOTA DE CRÉDITO C": "013", "RECIBO C": "015",
}

# Etiquetas visibles de cada alícuota (mismo criterio que ya usa el resto
# del sistema en la tabla de comprobantes y el editor de empresa).
ALICUOTA_ETIQUETAS = {
    "NO_GRAVADO": "No Gravado", "EXENTO": "Exento", "0": "0%",
    "2.5": "2,5%", "5": "5%", "10.5": "10,5%", "21": "21%", "27": "27%",
}

MARGEN = 15 * mm
NEGRO = colors.black
GRIS = colors.Color(0.4, 0.4, 0.4)


def _formato_ar(valor):
    """1500.5 -> '1.500,50' (formato argentino: punto de miles, coma decimal)."""
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _letra_y_codigo(tipo_comprobante):
    """"Factura C" -> ("C", "FACTURA"). Si no reconoce el patrón, usa valores genéricos."""
    texto = (tipo_comprobante or "").strip()
    partes = texto.rsplit(" ", 1)
    if len(partes) == 2 and len(partes[1]) <= 2:
        return partes[1].upper(), partes[0].upper()
    return "-", texto.upper() or "COMPROBANTE"


def generar_pdf_preview(comprobante):
    empresa = comprobante.empresa
    es_ri = bool(empresa) and empresa.tipo_contribuyente == "Responsable Inscripto"
    condicion_iva_emisor = "IVA Responsable Inscripto" if es_ri else "Responsable Monotributo"

    # La "Fecha de Emisión" que va a quedar en la factura REAL no es
    # necesariamente comprobante.fecha_comprobante (la fecha real detectada
    # en la imagen) -- es la que decide calcular_fecha_facturacion (el mismo
    # cálculo que usa el bot al facturar). Si acá se mostrara la fecha cruda,
    # la vista previa mentiría sobre qué fecha va a terminar en ARCA.
    fecha_emision_ajustada = comprobante.fecha_comprobante
    if comprobante.fecha_facturacion_manual:
        fecha_emision_ajustada = comprobante.fecha_facturacion_manual
    elif comprobante.fecha_comprobante:
        try:
            fecha_emision_ajustada = calcular_fecha_facturacion(
                comprobante.fecha_comprobante, empresa.config_dias_atras_fecha_emision if empresa else None
            )
        except ValueError:
            pass  # fecha con formato raro (o vacía) -- se muestra la cruda tal cual, sin cortar la vista previa

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    ancho, alto = A4
    x0, x1 = MARGEN, ancho - MARGEN

    def y_de(mm_desde_arriba):
        return alto - mm_desde_arriba * mm

    def texto(x_mm, y_mm, valor, tam=8.5, negrita=False, color=NEGRO, alinear="izquierda"):
        c.setFillColor(color)
        c.setFont("Helvetica-Bold" if negrita else "Helvetica", tam)
        y = y_de(y_mm)
        if alinear == "derecha":
            c.drawRightString(x_mm * mm, y, valor)
        elif alinear == "centro":
            c.drawCentredString(x_mm * mm, y, valor)
        else:
            c.drawString(x_mm * mm, y, valor)
        c.setFillColor(NEGRO)

    def campo(x_mm, y_mm, etiqueta, valor, tam=8):
        """"Etiqueta: valor" en una sola línea, etiqueta en negrita."""
        c.setFont("Helvetica-Bold", tam)
        c.drawString(x_mm * mm, y_de(y_mm), etiqueta)
        ancho_etiqueta = stringWidth(etiqueta, "Helvetica-Bold", tam)
        c.setFont("Helvetica", tam)
        c.drawString(x_mm * mm + ancho_etiqueta + 1.2 * mm, y_de(y_mm), str(valor) if valor not in (None, "") else "-")

    def rect(x_mm_a, y_mm_a, x_mm_b, y_mm_b):
        c.rect(x_mm_a * mm, y_de(y_mm_b), (x_mm_b - x_mm_a) * mm, (y_mm_b - y_mm_a) * mm, stroke=1, fill=0)

    def linea_h(y_mm, x_mm_a=None, x_mm_b=None):
        c.line((x_mm_a or x0 / mm) * mm, y_de(y_mm), (x_mm_b or x1 / mm) * mm, y_de(y_mm))

    def linea_v(x_mm, y_mm_a, y_mm_b):
        c.line(x_mm * mm, y_de(y_mm_a), x_mm * mm, y_de(y_mm_b))

    letra, palabra_comprobante = _letra_y_codigo(comprobante.tipo_comprobante)

    # ═══════════ Marca de agua diagonal ═══════════
    c.saveState()
    c.setFont("Helvetica-Bold", 70)
    c.setFillColor(colors.Color(0.88, 0.88, 0.88))
    c.translate(ancho / 2, alto / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, "MODELO")
    c.restoreState()

    # ═══════════ Cintillo superior: MODELO (en vez de ORIGINAL/DUPLICADO) ═══════════
    rect(15, 15, 195, 23)
    texto(105, 20.5, "MODELO — VISTA PREVIA, SIN VALIDEZ FISCAL", tam=10, negrita=True, alinear="centro", color=colors.Color(0.6, 0.1, 0.1))

    # ═══════════ Encabezado: emisor (izq) y tipo de comprobante (der) ═══════════
    y_header_ini, y_header_fin = 23, 65
    rect(15, y_header_ini, 195, y_header_fin)
    x_div = 122
    linea_v(x_div, y_header_ini, y_header_fin)

    # -- Columna izquierda: datos del emisor --
    texto(65, 30, empresa.razon_social_arca or "-", tam=12, negrita=True, alinear="centro")
    campo(18, 42, "Razón Social:", empresa.razon_social_arca)
    campo(18, 47, "Domicilio Comercial:", "-")
    campo(18, 52, "Condición frente al IVA:", condicion_iva_emisor)

    # -- Columna derecha: caja con la letra + código, título FACTURA --
    codigo_afip = CODIGOS_AFIP.get(f"{palabra_comprobante} {letra}".strip(), "-")
    rect(126, 26, 144, 40)
    texto(135, 32, letra, tam=20, negrita=True, alinear="centro")
    texto(135, 38, f"COD. {codigo_afip}", tam=6.5, alinear="centro")
    texto(148, 33, palabra_comprobante, tam=15, negrita=True)

    campo(126, 46, "Punto de Venta:", (comprobante.punto_venta or "-").zfill(5) if comprobante.punto_venta else "-", tam=8)
    campo(163, 46, "Comp. Nro:", "(a asignar)", tam=8)
    campo(126, 51, "Fecha de Emisión:", fecha_emision_ajustada, tam=8)
    campo(126, 56, "CUIT:", empresa.cuil_arca, tam=8)
    campo(126, 60, "Ingresos Brutos:", "-", tam=8)
    campo(126, 64, "Fecha de Inicio de Actividades:", "-", tam=8)

    # ═══════════ Período facturado ═══════════
    y_periodo = 65, 74
    rect(15, y_periodo[0], 195, y_periodo[1])
    campo(18, 71, "Período Facturado Desde:", comprobante.fecha_desde, tam=8.5)
    campo(90, 71, "Hasta:", comprobante.fecha_hasta, tam=8.5)
    campo(130, 71, "Fecha de Vto. para el pago:", comprobante.fecha_comprobante, tam=8.5)

    # ═══════════ Receptor ═══════════
    y_recep_ini, y_recep_fin = 74, 100
    rect(15, y_recep_ini, 195, y_recep_fin)
    linea_v(115, y_recep_ini, y_recep_fin)

    campo(18, 80, "Doc.:", f"{comprobante.tipo_documento} {comprobante.cuit_receptor}".strip() if comprobante.cuit_receptor else "-", tam=8.5)
    campo(18, 86, "Condición frente al IVA:", comprobante.condicion_iva, tam=8.5)
    campo(18, 92, "Condición de venta:", comprobante.condicion_venta, tam=8.5)
    if comprobante.tipo_pago:
        campo(18, 97, "Tarjeta:", f"{comprobante.tipo_pago} terminada en {comprobante.numero_pago or '-'}", tam=8)

    campo(118, 80, "Apellido y Nombre / Razón Social:", comprobante.nombre_razon_social, tam=8.5)
    campo(118, 86, "Domicilio:", "-", tam=8.5)

    # ═══════════ Detalle (tabla de ítems) -- soporta varias líneas ═══════════
    # La línea principal es siempre la del propio comprobante; las líneas
    # extra (solo existen para Responsable Inscripto) se agregan después,
    # en el mismo orden en que se cargaron.
    lineas_todas = [{
        "descripcion": comprobante.descripcion, "cantidad": comprobante.cantidad or 1,
        "unidad_medida": comprobante.unidad_medida, "precio_unitario": comprobante.precio_unitario or 0,
        "alicuota_iva": comprobante.alicuota_iva,
    }]
    for extra in (comprobante.lineas_extra or []):
        lineas_todas.append({
            "descripcion": extra.descripcion, "cantidad": extra.cantidad or 1,
            "unidad_medida": extra.unidad_medida, "precio_unitario": extra.precio_unitario or 0,
            "alicuota_iva": extra.alicuota_iva,
        })

    y_tabla_header_ini, y_tabla_header_fin = 100, 108
    y_tabla_fin = 165  # deja espacio en blanco debajo del último ítem, como en el comprobante real

    if es_ri:
        # Responsable Inscripto discrimina IVA por línea -- se cambian las
        # columnas de % Bonif./Imp. Bonif. (que Facturea no usa) por
        # Alícuota y Subtotal c/IVA, matcheando la pantalla real de ARCA.
        columnas = [
            ("Código", 18, 28), ("Producto / Servicio", 28, 75), ("Cant.", 75, 88),
            ("U. Medida", 88, 104), ("Prec. Unit.", 104, 124), ("Alícuota", 124, 138),
            ("Subtotal", 138, 165), ("Subt. c/IVA", 165, 195),
        ]
        largo_desc = 28
    else:
        columnas = [
            ("Código", 18, 33), ("Producto / Servicio", 33, 95), ("Cantidad", 95, 115),
            ("U. Medida", 115, 132), ("Precio Unit.", 132, 152), ("% Bonif", 152, 163),
            ("Imp. Bonif.", 163, 178), ("Subtotal", 178, 195),
        ]
        largo_desc = 38

    rect(15, y_tabla_header_ini, 195, y_tabla_fin)
    linea_h(y_tabla_header_fin, 15, 195)
    columnas_numericas = {"Cantidad", "Cant.", "Precio Unit.", "Prec. Unit.", "% Bonif", "Imp. Bonif.", "Subtotal", "Alícuota", "Subt. c/IVA"}
    for nombre_col, x_ini, x_fin in columnas:
        if x_ini != 18:
            linea_v(x_ini, y_tabla_header_ini, y_tabla_fin)
        alinear = "derecha" if nombre_col in columnas_numericas else "izquierda"
        x_texto = x_fin - 1.5 if alinear == "derecha" else x_ini + 1.5
        texto(x_texto, 105.5, nombre_col, tam=7, negrita=True, alinear=alinear)

    # Alto de cada fila: 6mm de siempre, salvo que hagan falta tantas líneas
    # que no entrarían en el espacio reservado -- ahí se achica un poco para
    # que todas queden visibles en vez de desbordar la tabla.
    alto_fila = min(6.0, max(3.5, (y_tabla_fin - y_tabla_header_fin - 4) / max(len(lineas_todas), 1)))

    # Acumuladores para el desglose de IVA de los Totales (solo se usan si es_ri).
    importe_neto_no_gravado = 0.0
    importe_exento = 0.0
    importe_neto_gravado = 0.0
    iva_por_alicuota = {"27": 0.0, "21": 0.0, "10.5": 0.0, "5": 0.0, "2.5": 0.0, "0": 0.0}
    subtotal_general = 0.0  # suma de subtotales NETOS (RI) o del único ítem (Monotributo)

    for i, linea in enumerate(lineas_todas):
        y_fila = y_tabla_header_fin + 4 + i * alto_fila
        cantidad = linea["cantidad"] or 1
        precio_unitario_gross = linea["precio_unitario"] or 0
        subtotal_gross = cantidad * precio_unitario_gross

        if es_ri:
            neto, iva = descomponer_neto_iva(subtotal_gross, linea["alicuota_iva"] or "0")
            subtotal_general += neto
            etiqueta_alicuota = ALICUOTA_ETIQUETAS.get(linea["alicuota_iva"], "-")
            if linea["alicuota_iva"] == "NO_GRAVADO":
                importe_neto_no_gravado += subtotal_gross
            elif linea["alicuota_iva"] == "EXENTO":
                importe_exento += subtotal_gross
            else:
                importe_neto_gravado += neto
                if linea["alicuota_iva"] in iva_por_alicuota:
                    iva_por_alicuota[linea["alicuota_iva"]] += iva

            fila = [
                ("", 18, 28, "izquierda"),
                ((linea["descripcion"] or "-")[:largo_desc], 28, 75, "izquierda"),
                (_formato_ar(cantidad), 75, 88, "derecha"),
                (linea["unidad_medida"] or "-", 88, 104, "izquierda"),
                (_formato_ar(neto / cantidad if cantidad else 0), 104, 124, "derecha"),
                (etiqueta_alicuota, 124, 138, "derecha"),
                (_formato_ar(neto), 138, 165, "derecha"),
                (_formato_ar(subtotal_gross), 165, 195, "derecha"),
            ]
        else:
            subtotal_general += subtotal_gross
            fila = [
                ("", 18, 33, "izquierda"),
                ((linea["descripcion"] or "-")[:largo_desc], 33, 95, "izquierda"),
                (_formato_ar(cantidad), 95, 115, "derecha"),
                (linea["unidad_medida"] or "-", 115, 132, "izquierda"),
                (_formato_ar(precio_unitario_gross), 132, 152, "derecha"),
                ("0,00", 152, 163, "derecha"),
                ("0,00", 163, 178, "derecha"),
                (_formato_ar(subtotal_gross), 178, 195, "derecha"),
            ]

        for valor, x_ini, x_fin, alinear in fila:
            x_texto = x_fin - 1.5 if alinear == "derecha" else x_ini + 1.5
            texto(x_texto, y_fila, valor, tam=min(8, alto_fila + 2), alinear=alinear)

    # ═══════════ Totales ═══════════
    if es_ri:
        # Responsable Inscripto discrimina el IVA por alícuota, igual que la
        # pantalla real de ARCA -- se muestran las 3 categorías generales y
        # las 6 alícuotas, aunque alguna dé $0,00 (así se ve igual que ARCA).
        y_tot_ini, y_tot_fin = 172, 246
        rect(120, y_tot_ini, 195, y_tot_fin)
        filas_totales = [
            ("Importe Neto no Gravado: $", _formato_ar(importe_neto_no_gravado)),
            ("Importe Exento: $", _formato_ar(importe_exento)),
            ("Importe Neto Gravado: $", _formato_ar(importe_neto_gravado)),
            ("IVA 27%: $", _formato_ar(iva_por_alicuota["27"])),
            ("IVA 21%: $", _formato_ar(iva_por_alicuota["21"])),
            ("IVA 10,5%: $", _formato_ar(iva_por_alicuota["10.5"])),
            ("IVA 5%: $", _formato_ar(iva_por_alicuota["5"])),
            ("IVA 2,5%: $", _formato_ar(iva_por_alicuota["2.5"])),
            ("IVA 0%: $", _formato_ar(iva_por_alicuota["0"])),
            ("Importe Otros Tributos: $", "0,00"),
        ]
        for idx, (etiqueta, valor) in enumerate(filas_totales):
            campo(122, 181 + idx * 6, etiqueta, valor, tam=8)
        texto(193, y_tot_fin - 3, f"Importe Total: $ {_formato_ar(comprobante.importe_total or subtotal_general)}",
              tam=9.5, negrita=True, alinear="derecha")
    else:
        y_tot_ini, y_tot_fin = 172, 200
        rect(120, y_tot_ini, 195, y_tot_fin)
        campo(122, 181, "Subtotal: $", _formato_ar(subtotal_general), tam=8.5)
        campo(122, 189, "Importe Otros Tributos: $", "0,00", tam=8.5)
        texto(193, 197, f"Importe Total: $ {_formato_ar(comprobante.importe_total or subtotal_general)}",
              tam=9.5, negrita=True, alinear="derecha")

    # ═══════════ Pie: en vez de QR + CAE real, estado de la vista previa ═══════════
    y_pie = 255
    rect(15, y_pie, 100, y_pie + 32)
    texto(18, y_pie + 8, "MODELO", tam=11, negrita=True, color=colors.Color(0.6, 0.1, 0.1))
    texto(18, y_pie + 15, "Vista previa -- no válido como comprobante fiscal", tam=7.5)
    texto(18, y_pie + 22, "Esta imagen se genera con los datos actuales del", tam=7)
    texto(18, y_pie + 27, "comprobante, sin haberse emitido todavía en ARCA.", tam=7)

    texto(195, y_pie + 8, "Pág. 1/1", tam=8.5, alinear="derecha")
    texto(195, y_pie + 16, "CAE N°: (se genera al facturar)", tam=8.5, negrita=True, alinear="derecha")
    texto(195, y_pie + 22, "Fecha de Vto. de CAE: -", tam=8.5, alinear="derecha")

    if comprobante.id_transaccion:
        texto(15, 292, f"ID transacción: {comprobante.id_transaccion}", tam=6.5, color=GRIS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

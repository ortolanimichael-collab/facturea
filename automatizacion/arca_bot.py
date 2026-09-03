"""
Bot de facturación para clientes sin certificado WSFE (Grupo B).
Reemplaza a facturador_manual.py (que usaba pyautogui), ahora con Playwright.

Construido a partir de grabaciones reales con `playwright codegen` sobre
el flujo de "Comprobantes en línea" de ARCA. Los selectores de acá abajo
son los reales del sitio, no inventados.

Criterio para los <select>: cuando el desplegable tiene pocas opciones y
usamos siempre el mismo valor fijo (ej: punto de venta), elegimos por el
VALOR interno. Cuando el desplegable tiene muchas opciones con texto
descriptivo largo, o el cliente puede configurar cualquiera de ellas (tipo
de comprobante, condición de IVA, tipo de documento, unidad de medida),
elegimos por el TEXTO visible (select_option(label=...)) -- así no hace
falta mantener un mapa a mano de código-por-texto para cada campo.
"""

from datetime import datetime, timedelta
import os
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def _headless_por_defecto():
    """
    En el servidor (producción) tiene que ser True -- ahí no hay nadie mirando
    la pantalla. En tu PC lo dejás en ARCA_BOT_HEADLESS=false (o directamente
    sin definir la variable) para seguir viendo el navegador mientras probás.
    """
    return os.environ.get("ARCA_BOT_HEADLESS", "false").strip().lower() in ("1", "true", "yes", "si", "sí")


def calcular_fecha_facturacion(fecha_comprobante_str, dias_atras=10):
    """
    Regla de fecha para facturar:
    - Por defecto, se usa hoy - `dias_atras` días (configurable por empresa;
      10 si no se especifica, que es el criterio histórico).
    - Pero esa fecha por defecto solo vale si es POSTERIOR a la fecha real
      de la transacción (no se puede facturar con fecha anterior a cuando
      pasó la operación real).
    - Si la transacción es más reciente que "hoy - dias_atras", se usa la
      fecha real del comprobante en su lugar.

    fecha_comprobante_str viene en formato "DD/MM/AAAA" (el mismo que usa
    el lector). Devuelve la fecha elegida en el mismo formato.
    """
    fecha_por_defecto = datetime.now() - timedelta(days=dias_atras or 10)
    fecha_real = datetime.strptime(fecha_comprobante_str, "%d/%m/%Y")

    fecha_elegida = fecha_por_defecto if fecha_real < fecha_por_defecto else fecha_real
    return fecha_elegida.strftime("%d/%m/%Y")


def iniciar_sesion(page, cuil, password):
    page.goto("https://auth.afip.gob.ar/contribuyente_/login.xhtml")
    page.get_by_role("spinbutton").fill(cuil)
    page.get_by_role("button", name="Siguiente").click()
    page.get_by_role("textbox", name="TU CLAVE").fill(password)
    page.get_by_role("button", name="Ingresar").click()


def entrar_a_comprobantes_en_linea(page, razon_social):
    """
    "Comprobantes en línea" se abre en una ventana emergente (popup) aparte,
    no en la misma pestaña -- por eso el resto de las funciones reciben
    esa ventana nueva, no la original.
    """
    with page.expect_popup() as popup_info:
        page.locator("a").filter(has_text="Comprobantes en línea").click()
    ventana = popup_info.value
    ventana.get_by_role("button", name=razon_social).click()
    ventana.get_by_role("button", name="Generar Comprobantes").click()
    return ventana


def elegir_punto_de_venta_y_tipo_comprobante(ventana, punto_venta, texto_tipo_comprobante):
    """
    Punto de venta y tipo de comprobante están en la MISMA pantalla, con un
    solo botón "Continuar >" para los dos -- confirmado con una grabación
    real de playwright codegen. Hay que elegir los dos ANTES de continuar:
    si se aprieta Continuar habiendo elegido solo el punto de venta, el tipo
    de comprobante queda en su valor por defecto (que puede no ser el que
    el cliente configuró) y ARCA puede mandar por un flujo de pasos
    distinto según qué tipo haya quedado seleccionado.

    El punto de venta se elige por VALOR interno (ej: "1"), porque ese
    desplegable muestra la dirección completa en el texto y es más frágil
    matchear por texto. El tipo de comprobante se elige por el TEXTO visible
    (ej: "Factura C"), porque no tenemos mapeados los códigos numéricos
    internos de cada tipo -- esto también evita tener que armar un mapa a
    mano para las variantes MiPyMEs (FCE) el día que algún cliente las use.
    """
    ventana.locator("#puntodeventa").select_option(punto_venta)
    ventana.locator("#universocomprobante").select_option(label=texto_tipo_comprobante)
    ventana.get_by_role("button", name="Continuar >").click()


def completar_paso_uno(ventana, fecha_emision, concepto, fecha_desde, fecha_hasta):
    """
    "Datos de Emisión, Paso 1 de 4" -- ARCA junta en UNA sola pantalla la
    "Fecha del Comprobante" (que es la fecha de EMISIÓN real de la factura,
    pese al nombre parecido a "Fecha del Comprobante" en nuestra propia
    tabla), "Conceptos a incluir" y -- para ciertos conceptos -- el período
    facturado Desde/Hasta. Hay que completar TODO esto antes de un único
    Continuar; antes se apretaba Continuar apenas se tocaba la fecha, sin
    haber elegido el concepto todavía, lo que rompía el resto del flujo
    (confirmado con una grabación real de playwright codegen).

    El período Desde/Hasta solo aparece en esta pantalla para algunos
    conceptos (no para "Productos y Servicios (Mixto)", confirmado con
    grabación real) -- si los campos no están, se los ignora en vez de
    quedar esperándolos para siempre.

    Al cambiar la fecha del valor por defecto (hoy) a otra, ARCA puede
    disparar un diálogo de confirmación (alert de JS) que hay que
    descartar antes de poder seguir.

    Si la fecha pedida ya quedó "tapada" por un comprobante emitido con
    fecha más nueva (ARCA no deja facturar con una fecha más vieja que la
    del último comprobante ya emitido en ese punto de venta/tipo -- tira
    "Error: La Fecha del Comprobante es inválida"), se reintenta solo con
    el día siguiente, avanzando de a un día hasta que ARCA la acepte -- sin
    pasarse nunca de HOY, que siempre es válida por defecto. Cada reintento
    vuelve a completar la pantalla entera (el "< Volver" la deja en blanco
    de nuevo).

    Devuelve la fecha de emisión que finalmente quedó cargada (puede ser
    distinta a la pedida si tuvo que avanzar por este motivo).
    """
    fecha_dt = datetime.strptime(fecha_emision, "%d/%m/%Y")
    hoy_dt = datetime.now()
    intentos_maximos = max((hoy_dt.date() - fecha_dt.date()).days + 1, 1)

    for intento in range(intentos_maximos):
        fecha_intento = (fecha_dt + timedelta(days=intento)).strftime("%d/%m/%Y")

        campo_fecha = ventana.get_by_role("textbox", name="Fecha del Comprobante")
        campo_fecha.click()
        campo_fecha.fill(fecha_intento)

        ventana.locator("#idconcepto").select_option(concepto)

        campo_desde = ventana.get_by_role("textbox", name="Desde")
        if campo_desde.count() > 0:
            campo_desde.click()
            campo_desde.fill(fecha_desde)
            campo_hasta = ventana.get_by_role("textbox", name="Hasta")
            campo_hasta.click()
            campo_hasta.fill(fecha_hasta)

        ventana.once("dialog", lambda dialog: dialog.dismiss())
        ventana.get_by_role("button", name="Continuar >").click()

        # Espera activa (no un sleep fijo): apenas aparece el cartel de error
        # seguimos -- si no aparece en 6s, asumimos que ARCA avanzó de pantalla.
        error = ventana.get_by_text("La Fecha del Comprobante es inválida")
        try:
            error.wait_for(state="visible", timeout=6000)
        except PlaywrightTimeoutError:
            return fecha_intento  # no apareció el error -> se avanzó de pantalla

        # Fecha rechazada -- ARCA ya tiene un comprobante con fecha posterior a esta.
        # Volvemos a la pantalla anterior para probar con el día siguiente. El "<
        # Volver" también puede disparar su propio diálogo de confirmación (visto
        # en una grabación real), así que se registra el dismiss antes de tocarlo.
        ventana.once("dialog", lambda dialog: dialog.dismiss())
        ventana.get_by_role("button", name="< Volver").click()

    raise ValueError(
        f"ARCA rechazó todas las fechas probadas entre {fecha_emision} y hoy "
        "(ya existen comprobantes emitidos con fecha posterior a todas ellas)."
    )


def completar_receptor(ventana, condicion_iva, tipo_doc_receptor, condicion_venta, medio_pago_detectado=None, tipo_pago=None, numero_pago=None):
    """
    Si medio_pago_detectado es "Débito" o "Crédito", después de tildar el
    checkbox de la tarjeta correspondiente aparece un panel extra con Tipo y
    Número de tarjeta, que hay que completar ANTES de tocar Continuar --
    confirmado con grabaciones reales para las dos. No hace falta apretar
    "Agregar": ese botón es para sumar VARIAS tarjetas al mismo comprobante
    (pago dividido); con una sola alcanza con completar Tipo y Número y
    continuar directo.
    """
    ventana.locator("#idivareceptor").select_option(label=condicion_iva)
    ventana.locator("#idtipodocreceptor").select_option(label=tipo_doc_receptor)
    ventana.get_by_role("checkbox", name=condicion_venta).check()

    if medio_pago_detectado == "Débito":
        ventana.locator("#tarjeta_id_tipo_debito1").select_option(label=tipo_pago)
        campo_numero = ventana.locator("#tarjeta_nro_debito1")
        campo_numero.click()
        campo_numero.fill(numero_pago)
    elif medio_pago_detectado == "Crédito":
        ventana.locator("#tarjeta_id_tipo_credito1").select_option(label=tipo_pago)
        campo_numero = ventana.locator("#tarjeta_nro_credito1")
        campo_numero.click()
        campo_numero.fill(numero_pago)

    ventana.get_by_role("button", name="Continuar >").click()


def completar_detalle(ventana, descripcion, unidad_medida, importe):
    ventana.locator("#detalle_descripcion1").fill(descripcion)
    ventana.locator("#detalle_medida1").select_option(label=unidad_medida)
    campo_precio = ventana.locator("#detalle_precio1")
    campo_precio.fill(str(importe))
    campo_precio.press("Enter")
    ventana.get_by_role("button", name="Continuar >").click()


def confirmar_y_facturar(ventana, modo_prueba=False):
    ventana.get_by_role("button", name="Confirmar Datos...").click()

    if modo_prueba:
        # Se detiene ACÁ, en la pantalla de revisión final de ARCA -- NO
        # toca el botón real de "Confirmar", así que no se emite ninguna
        # factura ni se genera ningún CAE. Se queda un rato visible para
        # poder mirarla con tranquilidad antes de que se cierre el navegador.
        ventana.wait_for_timeout(30000)
        return

    ventana.get_by_role("button", name="Confirmar", exact=True).click()
    # TODO: leer el CAE real que aparece en esta pantalla antes de volver al menú
    ventana.get_by_role("button", name="Menú Principal").click()


def obtener_credenciales(empresa):
    if not empresa.cuil_arca or not empresa.password_arca_cifrada:
        raise ValueError(f"La empresa '{empresa.nombre_interno}' no tiene credenciales de ARCA cargadas.")
    return empresa.cuil_arca, empresa.get_password_arca()


def facturar_comprobante(comprobante, modo_prueba=False):
    """
    El login de ARCA (cuil/password) y la razón social a representar salen
    de comprobante.empresa -- cada empresa tiene su PROPIO acceso a ARCA,
    porque cada una tiene su propia Clave Fiscal. El resto de los datos de
    facturación (tipo de comprobante, punto de venta, condición de IVA,
    etc.) salen del propio COMPROBANTE: arrancan copiados de la
    configuración de la empresa al subir el archivo, pero el usuario pudo
    haberlos editado para esta factura en particular sin afectar a las demás.

    Con modo_prueba=True, hace TODO el recorrido real contra ARCA pero se
    detiene en la pantalla de revisión final sin confirmar -- no se emite
    ninguna factura ni se genera CAE. Sirve para chequear visualmente que
    todos los campos se completan bien antes de facturar de verdad.

    Devuelve un dict {"fecha_usada": "DD/MM/AAAA", "fecha_ajustada": bool}:
    fecha_usada es la fecha que REALMENTE se escribió en ARCA (puede no ser
    la de comprobante.fecha_comprobante, ver calcular_fecha_facturacion);
    fecha_ajustada avisa si hubo que corregirla -- para que quien llama
    pueda avisarle al usuario en vez de dejar el cambio pasar en silencio.
    """
    empresa = comprobante.empresa

    campos_obligatorios = [
        comprobante.punto_venta, comprobante.tipo_comprobante, comprobante.concepto,
        comprobante.condicion_iva, comprobante.tipo_documento, comprobante.condicion_venta,
        comprobante.descripcion, comprobante.unidad_medida,
    ]
    if comprobante.medio_pago_detectado in ("Débito", "Crédito"):
        campos_obligatorios += [comprobante.tipo_pago, comprobante.numero_pago]

    if not empresa.razon_social_arca or not all(campos_obligatorios):
        raise ValueError(
            f"El comprobante #{comprobante.id} de '{empresa.nombre_interno}' todavía "
            "tiene campos de facturación sin completar."
        )

    fecha_facturacion = calcular_fecha_facturacion(comprobante.fecha_comprobante, empresa.config_dias_atras_fecha_emision)

    cuil, password = obtener_credenciales(empresa)

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=_headless_por_defecto())
        contexto = navegador.new_context()
        pagina = contexto.new_page()

        try:
            iniciar_sesion(pagina, cuil, password)
            ventana = entrar_a_comprobantes_en_linea(pagina, empresa.razon_social_arca)
            elegir_punto_de_venta_y_tipo_comprobante(
                ventana, comprobante.punto_venta, comprobante.tipo_comprobante
            )

            # "Paso 1 de 4" de ARCA -- fecha de emisión, concepto y período
            # (si corresponde) van todos juntos acá, con un solo Continuar.
            fecha_realmente_usada = completar_paso_uno(
                ventana, fecha_facturacion, comprobante.concepto,
                comprobante.fecha_desde, comprobante.fecha_hasta,
            )

            completar_receptor(
                ventana,
                comprobante.condicion_iva,
                comprobante.tipo_documento,
                comprobante.condicion_venta,
                medio_pago_detectado=comprobante.medio_pago_detectado,
                tipo_pago=comprobante.tipo_pago,
                numero_pago=comprobante.numero_pago,
            )
            completar_detalle(
                ventana,
                descripcion=comprobante.descripcion,
                unidad_medida=comprobante.unidad_medida,
                importe=comprobante.precio_unitario,
            )
            confirmar_y_facturar(ventana, modo_prueba=modo_prueba)
        finally:
            contexto.close()
            navegador.close()

    return {
        "fecha_usada": fecha_realmente_usada,
        "fecha_ajustada": fecha_realmente_usada != comprobante.fecha_comprobante,
    }

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
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def _registrar_actividad_bot(empresa_id=None, comprobante_id=None, nivel="info", evento="info",
                              detalle=None, fecha_probada=None, resultado=None, tiempo_respuesta_seg=None):
    """
    Guarda un renglón en RegistroActividadBot (log técnico solo para vos
    como administrador, ver /admin/log-bot) -- import local para no crear
    una dependencia circular entre este módulo y models.py/app.py, y
    envuelto en try/except porque esto es solo diagnóstico: un problema acá
    (ej. sin contexto de Flask disponible) NUNCA tiene que frenar ni romper
    una facturación real.
    """
    try:
        from models import db, RegistroActividadBot
        registro = RegistroActividadBot(
            empresa_id=empresa_id,
            comprobante_id=comprobante_id,
            nivel=nivel,
            evento=evento,
            detalle=detalle,
            fecha_probada=fecha_probada,
            resultado=resultado,
            tiempo_respuesta_seg=tiempo_respuesta_seg,
        )
        db.session.add(registro)
        db.session.commit()
    except Exception:
        # Diagnóstico best-effort -- nunca interrumpe la facturación real.
        pass


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


def concepto_efectivo(fecha_comprobante_str, concepto_configurado, dias_atras=10):
    """
    Si la empresa factura por defecto en concepto "Productos y Servicios
    (Mixto)" ("3") pero la fecha del comprobante está DENTRO de los últimos
    `dias_atras` días (la misma ventana que usa calcular_fecha_facturacion,
    mismo criterio de comparación), se declara como "Productos" ("1") en su
    lugar -- no tiene sentido facturar como si fuera un período de servicio
    cuando la operación es reciente y se factura casi al toque.

    Fuera de esa ventana (comprobante viejo) o con cualquier otro concepto
    configurado (no-mixto), se respeta tal cual el que eligió la empresa.
    """
    if concepto_configurado != "3":
        return concepto_configurado
    try:
        fecha_real = datetime.strptime(fecha_comprobante_str, "%d/%m/%Y")
    except (TypeError, ValueError):
        return concepto_configurado

    fecha_por_defecto = datetime.now() - timedelta(days=dias_atras or 10)
    if fecha_real >= fecha_por_defecto:
        return "1"
    return concepto_configurado


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

    Ahí adentro, "Seleccione la Empresa a representar" muestra un botón por
    cada empresa que el CUIL logueado puede representar -- en el caso más
    común (un monotributista con una sola razón social) hay UN solo botón.
    Antes se buscaba ese botón por texto EXACTO contra empresa.razon_social_arca,
    y bastaba una letra distinta, un espacio de más o una mayúscula/minúscula
    diferente para que Playwright no encontrara nada y se quedara esperando
    para siempre, sin ningún aviso de error -- quedaba "tildado" en esa
    pantalla. Si hay un solo botón, se clickea directo sin importar el
    texto; el matcheo por razón social solo hace falta (y solo entonces se
    exige que esté bien escrita) cuando aparece más de una empresa para
    elegir.
    """
    with page.expect_popup() as popup_info:
        page.locator("a").filter(has_text="Comprobantes en línea").click()
    ventana = popup_info.value

    botones_empresa = ventana.get_by_role("button")
    botones_empresa.first.wait_for(timeout=45000)
    cantidad_empresas = botones_empresa.count()

    if cantidad_empresas == 1:
        botones_empresa.first.click()
    elif cantidad_empresas > 1:
        boton_por_nombre = ventana.get_by_role("button", name=razon_social)
        if boton_por_nombre.count() == 0:
            nombres_disponibles = botones_empresa.all_inner_texts()
            raise ValueError(
                f"Hay {cantidad_empresas} empresas para elegir en ARCA y ninguna coincide con "
                f"la razón social configurada (\"{razon_social}\"). Las que aparecen ahí son: "
                f"{', '.join(nombres_disponibles)}. Revisá que 'Razón social en ARCA' esté escrita "
                "EXACTO como aparece ahí (mayúsculas incluidas)."
            )
        boton_por_nombre.first.click()
    else:
        raise ValueError("No apareció ninguna empresa para elegir en 'Comprobantes en línea' de ARCA.")

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

    ARCA en algún momento de 2026 empezó a mostrar "Punto de Ventas a
    utilizar" con un buscador visual encima (el que dice "seleccionar..."
    y despliega una lista al abrirlo) -- pero el mensaje de error que
    reportó un cliente confirma que el <select id="puntodeventa"> de toda
    la vida SIGUE estando ahí en el HTML ("locator resolved to <select
    id="puntodeventa"...>"), lo que pasa es que ARCA ahora lo tapa/oculta
    con CSS para mostrar el buscador nuevo en su lugar. select_option()
    exige que el elemento esté visible antes de tocarlo, y como ARCA lo
    esconde a propósito, eso nunca pasa -- de ahí el timeout eterno. Ver
    _elegir_punto_de_venta() para el arreglo.
    """
    _elegir_punto_de_venta(ventana, punto_venta)
    ventana.locator("#universocomprobante").select_option(label=texto_tipo_comprobante)
    ventana.get_by_role("button", name="Continuar >").click()


def _elegir_punto_de_venta(ventana, punto_venta, timeout_normal=6000):
    """
    Se intenta primero de la forma de siempre (por si ARCA vuelve a
    mostrarlo visible como antes, o en cuentas donde no cambió nada). Si
    eso tarda de más, hay dos problemas posibles y hay que resolver los DOS:

    1. El <select> está oculto detrás del buscador nuevo de ARCA (visible
       en pantalla como "seleccionar...") -- select_option exige que el
       elemento esté visible, y como ARCA lo tapa a propósito, nunca lo
       está. Se resuelve con force=True, que le dice a Playwright que
       ignore ese chequeo.
    2. El "value" interno que ARCA le pone a cada <option> puede haber
       cambiado (dejar de ser un número simple como "1"). Un primer intento
       de arreglo asumía que el ÚNICO problema era la visibilidad, pero un
       cliente lo probó y le siguió fallando con el mismo timeout de
       30000ms -- eso significa que el problema real es que "1" ya no
       matchea ningún <option> real, no solo que estaba tapado.

    Por eso ahora, en vez de asumir cuál es el value interno, se LEE
    directo del HTML (con JavaScript, algo que funciona aunque el <select>
    esté escondido, porque es inspeccionar el DOM y no una acción simulada
    de mouse/teclado) cuál es el <option> cuyo TEXTO VISIBLE empieza con el
    punto de venta con cero a la izquierda tal como ARCA lo muestra en la
    lista (ej: punto_venta="1" -> "00001-..."), y se usa el value real de
    ESE option -- así no importa qué formato interno use ARCA.
    """
    selector = ventana.locator("#puntodeventa")
    try:
        selector.select_option(punto_venta, timeout=timeout_normal)
        return
    except PlaywrightTimeoutError:
        pass

    prefijo_visible = f"{int(punto_venta):05d}"
    valor_real = selector.evaluate(
        """(select, prefijo) => {
            const opciones = Array.from(select.options);
            const encontrada = opciones.find(o => o.text.trim().startsWith(prefijo));
            return encontrada ? encontrada.value : null;
        }""",
        prefijo_visible,
    )
    if valor_real is None:
        raise ValueError(
            f"No se encontró en ARCA ningún punto de venta que empiece con \"{prefijo_visible}-\" "
            f"en el desplegable de \"Punto de Ventas a utilizar\". Revisá que el punto de venta "
            f"configurado ({punto_venta}) siga habilitado en ARCA para esta empresa."
        )
    selector.select_option(valor_real, force=True)


def completar_paso_uno(ventana, fecha_emision, concepto, fecha_desde, fecha_hasta,
                        empresa_id=None, comprobante_id=None):
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

    IMPORTANTE (fix de un bug real de octubre 2026): antes, "¿aceptó o
    rechazó ARCA esta fecha?" se decidía con un timeout fijo de 6
    segundos -- si no aparecía el cartel de error en ese lapso, se asumía
    que había avanzado de pantalla. Si ARCA estaba lenta (tráfico alto,
    problemas de red) y tardaba más de 6s en MOSTRAR el error de verdad,
    el bot interpretaba la fecha como aceptada sin que lo fuera. Como ARCA
    nunca deja retroceder la fecha de un comprobante dentro de un mismo
    punto de venta, ese error de lectura quedaba "pisado" para siempre: el
    resto del lote completo terminaba facturándose con fechas cada vez más
    adelantadas, hasta llegar a la fecha de hoy. Ahora se espera lo que
    haga falta -- sin asumir nada por tiempo -- a que aparezca CUALQUIERA
    de los dos resultados reales: el cartel de error, o el campo
    "#idivareceptor" que solo existe en la pantalla siguiente ("Paso 2 de
    4, Datos del Receptor") y por lo tanto es prueba real de haber
    avanzado. El timeout de 2 minutos es solo una salvaguarda ante un
    cuelgue real de ARCA, no el mecanismo de detección en sí.
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
        inicio_espera = time.monotonic()
        ventana.get_by_role("button", name="Continuar >").click()

        # Espera activa a cualquiera de los dos resultados reales -- nunca
        # se asume nada por un timeout corto (ver docstring de arriba).
        error = ventana.get_by_text("La Fecha del Comprobante es inválida")
        avanzo_a_paso_dos = ventana.locator("#idivareceptor")
        try:
            error.or_(avanzo_a_paso_dos).first.wait_for(state="visible", timeout=120000)
        except PlaywrightTimeoutError:
            tiempo_respuesta = time.monotonic() - inicio_espera
            _registrar_actividad_bot(
                empresa_id=empresa_id, comprobante_id=comprobante_id,
                nivel="error", evento="intento_fecha",
                detalle=(
                    f"Fecha probada: {fecha_intento} -> ARCA no respondió ni con el error ni "
                    f"con la pantalla siguiente en 2 minutos (tardó {tiempo_respuesta:.1f}s)."
                ),
                fecha_probada=fecha_intento, resultado="sin_respuesta",
                tiempo_respuesta_seg=tiempo_respuesta,
            )
            raise ValueError(
                f"ARCA no respondió (ni aceptó ni rechazó) la fecha {fecha_intento} en 2 minutos "
                "-- probablemente esté caída o muy lenta en este momento. Reintentá más tarde."
            )
        tiempo_respuesta = time.monotonic() - inicio_espera

        if error.is_visible():
            # Fecha rechazada -- ARCA ya tiene un comprobante con fecha posterior a esta.
            _registrar_actividad_bot(
                empresa_id=empresa_id, comprobante_id=comprobante_id,
                nivel="intento_fecha", evento="intento_fecha",
                detalle=f"Fecha probada: {fecha_intento} -> rechazada por ARCA (tardó {tiempo_respuesta:.1f}s).",
                fecha_probada=fecha_intento, resultado="rechazada",
                tiempo_respuesta_seg=tiempo_respuesta,
            )
            # Volvemos a la pantalla anterior para probar con el día siguiente. El "<
            # Volver" también puede disparar su propio diálogo de confirmación (visto
            # en una grabación real), así que se registra el dismiss antes de tocarlo.
            ventana.once("dialog", lambda dialog: dialog.dismiss())
            ventana.get_by_role("button", name="< Volver").click()
            continue

        # Avanzó de pantalla -- confirmado de verdad (apareció #idivareceptor), no por descarte.
        _registrar_actividad_bot(
            empresa_id=empresa_id, comprobante_id=comprobante_id,
            nivel="intento_fecha", evento="intento_fecha",
            detalle=f"Fecha probada: {fecha_intento} -> aceptada por ARCA (tardó {tiempo_respuesta:.1f}s).",
            fecha_probada=fecha_intento, resultado="aceptada",
            tiempo_respuesta_seg=tiempo_respuesta,
        )
        return fecha_intento

    raise ValueError(
        f"ARCA rechazó todas las fechas probadas entre {fecha_emision} y hoy "
        "(ya existen comprobantes emitidos con fecha posterior a todas ellas)."
    )


def _completar_tarjeta(ventana, prefijo, tipo_pago, tipo_pago_detalle, numero_pago):
    """
    Completa el panel de Tipo/Descripción/Número que aparece al tildar
    Tarjeta de Débito o Crédito. `prefijo` es "debito" o "credito" -- arma
    los ids reales de ARCA: #tarjeta_id_tipo_<prefijo>1 (select),
    #tarjeta_desc_tipo_<prefijo>1 (descripción libre, solo si el tipo es
    "Otra...") y #tarjeta_nro_<prefijo>1 (número).

    Si tipo_pago es "Otra..." -- el caso de una marca que no es una opción
    real del desplegable de ARCA, ej. "VISA Débito" en vez de
    "Visa Electrón" -- hay que tipear tipo_pago_detalle en la casilla de
    Descripción que aparece al lado. Si esa casilla queda vacía, ARCA no
    deja avanzar de pantalla (por eso el intento anterior se quedó
    esperando el Paso 3 sin que nunca cargara).

    El campo Número exige el número COMPLETO de tarjeta (20 dígitos) con
    ceros a la izquierda -- lo único que se puede leer de un comprobante
    real son los últimos dígitos, así que se rellena acá con zfill(20).

    IMPORTANTE: no se toca el botón "Agregar" de este panel. Ya está
    confirmado que es solo para sumar una tarjeta ADICIONAL en un pago
    dividido entre varias -- no hace falta para que la que ya se tipeó
    quede cargada. Clickearlo de más agrega una segunda fila vacía en el
    panel que ARCA exige completar antes de dejar avanzar (eso fue lo que
    trabó el intento anterior en el Paso 2, y por eso el bot terminó
    esperando un campo del Paso 3 que nunca llegó a aparecer).
    """
    ventana.locator(f"#tarjeta_id_tipo_{prefijo}1").select_option(label=tipo_pago)
    if tipo_pago == "Otra...":
        ventana.locator(f"#tarjeta_desc_tipo_{prefijo}1").fill(tipo_pago_detalle or "")
    numero_completo = str(numero_pago or "").strip().zfill(20)
    ventana.locator(f"#tarjeta_nro_{prefijo}1").fill(numero_completo)


def completar_receptor(ventana, condicion_iva, tipo_doc_receptor, condicion_venta, medio_pago_detectado=None, tipo_pago=None, tipo_pago_detalle=None, numero_pago=None):
    """
    Si medio_pago_detectado es "Débito" o "Crédito", después de tildar el
    checkbox de la tarjeta correspondiente aparece un panel extra con Tipo,
    Descripción (si el tipo es "Otra...") y Número de tarjeta -- ver
    _completar_tarjeta() para el detalle de cómo se completa cada uno.
    """
    ventana.locator("#idivareceptor").select_option(label=condicion_iva)
    ventana.locator("#idtipodocreceptor").select_option(label=tipo_doc_receptor)
    ventana.get_by_role("checkbox", name=condicion_venta).check()

    if medio_pago_detectado == "Débito":
        ventana.get_by_role("checkbox", name="Tarjeta de Débito").check()
        _completar_tarjeta(ventana, "debito", tipo_pago, tipo_pago_detalle, numero_pago)
    elif medio_pago_detectado == "Crédito":
        ventana.get_by_role("checkbox", name="Tarjeta de Crédito").check()
        _completar_tarjeta(ventana, "credito", tipo_pago, tipo_pago_detalle, numero_pago)

    ventana.get_by_role("button", name="Continuar >").click()


def completar_detalle(ventana, descripcion, unidad_medida, importe):
    ventana.locator("#detalle_descripcion1").fill(descripcion)
    ventana.locator("#detalle_medida1").select_option(label=unidad_medida)
    campo_precio = ventana.locator("#detalle_precio1")
    campo_precio.fill(str(importe))
    campo_precio.press("Enter")
    ventana.get_by_role("button", name="Continuar >").click()


# Alícuota de IVA -- valores internos de ARCA confirmados con una captura
# real del desplegable (no siguen ningún orden obvio, así que se elige
# por VALOR, no por texto: evita depender de que "No gravado"/"10,5%"
# etc. coincidan letra por letra, coma y mayúscula con lo que guarda el
# sistema).
MAPA_ALICUOTA_IVA_ARCA = {
    "NO_GRAVADO": "1", "EXENTO": "2", "0": "3",
    "10.5": "4", "21": "5", "27": "6", "5": "8", "2.5": "9",
}

# Tipos de comprobante de Responsable Inscripto ya confirmados de punta a
# punta con una grabación real de Playwright codegen -- cualquier otro
# (Factura T, Recibo A/B, las variantes FCE) corta con un aviso claro en
# vez de arriesgarse a mandar algo mal a una factura real.
TIPOS_COMPROBANTE_RI_CONFIRMADOS = {"Factura A", "Factura B"}


def completar_receptor_ri(
    ventana, condicion_iva, tipo_doc_receptor, cuit_dni, condicion_venta,
    medio_pago_detectado=None, tipo_pago=None, tipo_pago_detalle=None, numero_pago=None,
):
    """
    "Datos del Receptor, Paso 2 de 4" para Responsable Inscripto --
    confirmado con grabaciones reales de Factura A y Factura B. Muy
    parecido a completar_receptor() de Monotributo (misma pantalla base,
    "genComDatosOperacion.do"), con una diferencia real: en Factura A el
    Tipo de Documento queda FIJO en "CUIT" (se ve como texto fijo en la
    pantalla, no como desplegable) -- el <select id="idtipodocreceptor">
    directamente no existe ahí. En Factura B sí existe y hay que elegirlo
    (por ejemplo, DNI para un Consumidor Final).

    Si medio_pago_detectado es "Débito" o "Crédito", hay que tildar el
    checkbox de esa tarjeta y completar el panel de Tipo/Descripción/Número
    -- ver _completar_tarjeta() para el detalle. Mismos ids que en
    Monotributo -- no hace falta un mapa nuevo para esto.
    """
    ventana.locator("#idivareceptor").select_option(label=condicion_iva)

    selector_tipo_doc = ventana.locator("#idtipodocreceptor")
    if selector_tipo_doc.count() > 0:
        selector_tipo_doc.select_option(label=tipo_doc_receptor or "DNI")

    if cuit_dni:
        ventana.locator("#nrodocreceptor").fill(cuit_dni)

    ventana.get_by_role("checkbox", name=condicion_venta).check()

    if medio_pago_detectado == "Débito":
        ventana.get_by_role("checkbox", name="Tarjeta de Débito").check()
        _completar_tarjeta(ventana, "debito", tipo_pago, tipo_pago_detalle, numero_pago)
    elif medio_pago_detectado == "Crédito":
        ventana.get_by_role("checkbox", name="Tarjeta de Crédito").check()
        _completar_tarjeta(ventana, "credito", tipo_pago, tipo_pago_detalle, numero_pago)

    ventana.get_by_role("button", name="Continuar >").click()


def completar_lineas_productos_ri(ventana, lineas):
    """
    "Datos de la Operación, Paso 3 de 4" para Responsable Inscripto --
    confirmado con una grabación real agregando una segunda línea con el
    botón "Agregar línea descripción". Cada línea tiene sus propios
    campos numerados (detalle_descripcion1, detalle_descripcion2, ...).

    Por ahora se manda siempre UNA sola línea, armada con los datos que
    ya tiene el comprobante (cargar varias líneas a mano en un mismo
    comprobante es una mejora pendiente aparte, todavía no está el lugar
    en la tabla para hacerlo) -- pero la función ya soporta la lista
    completa para cuando esa mejora esté lista, sin tener que tocar esto.

    Cada línea es un dict con: descripcion, cantidad, unidad_medida,
    precio_unitario_neto (SIN IVA -- ARCA calcula el IVA y el subtotal
    solos a partir de este dato y la alícuota, confirmado con captura
    real) y alicuota_iva.
    """
    for i, linea in enumerate(lineas, start=1):
        if i > 1:
            ventana.get_by_role("button", name="Agregar línea descripción").click()

        ventana.locator(f"#detalle_descripcion{i}").fill(linea["descripcion"])
        ventana.locator(f"#detalle_cantidad{i}").fill(str(linea.get("cantidad") or 1))
        ventana.locator(f"#detalle_medida{i}").select_option(label=linea["unidad_medida"])
        ventana.locator(f"#detalle_precio{i}").fill(str(linea["precio_unitario_neto"]))

        codigo_alicuota = MAPA_ALICUOTA_IVA_ARCA.get(linea["alicuota_iva"])
        if codigo_alicuota is None:
            raise ValueError(f"Alícuota de IVA inválida o sin configurar: {linea['alicuota_iva']!r}")
        ventana.locator(f"#detalle_tipo_iva{i}").select_option(codigo_alicuota)

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


def descomponer_neto_iva(importe_total, alicuota_iva, discrimina_iva):
    """
    SOLO se descompone en neto + IVA cuando el comprobante DISCRIMINA el IVA
    al receptor -- Factura A (y notas de débito/crédito A, si algún día se
    suman): ahí el receptor es otro Responsable Inscripto que va a tomarse
    ese IVA como crédito fiscal, así que ARCA pide el neto gravado por
    separado y él mismo suma el IVA arriba para calcular el total.

    Factura B (venta a Consumidor Final u otro sujeto que NO discrimina
    IVA) es DISTINTO, y esto se confirmó con una factura real: ahí "Precio
    Unit." en ARCA es directamente el precio FINAL que se cobró (con el IVA
    ya adentro, como en cualquier góndola) -- ARCA no le suma IVA arriba,
    solo calcula hacia atrás un "IVA Contenido" informativo (por la Ley de
    Transparencia Fiscal al Consumidor). Si acá se llegaba a mandar el neto
    en vez del total, la factura terminaba emitida por MENOS plata de la
    que el cliente realmente pagó (pasó con un comprobante real: cobró
    $7000, pero al descomponerlo con 21% se cargó $5785,12 como precio y la
    factura salió por ese monto, no por los $7000 reales).

    alicuota_iva viene como uno de los 8 valores reales que tiene ARCA
    (confirmado con una captura real de "Alícuota IVA"):
    "NO_GRAVADO", "EXENTO", "0", "2.5", "5", "10.5", "21", "27" -- los
    primeros dos NO son porcentajes (son conceptos legales distintos: "no
    gravado" es algo fuera del alcance del IVA, "exento" es una operación
    puntualmente exenta), pero para esta cuenta dan el mismo resultado que
    una alícuota de 0%: todo el importe es neto, el IVA da $0.

    Devuelve (precio_a_cargar, iva) como floats, redondeados a 2 decimales.
    Con discrimina_iva=False, precio_a_cargar es el total sin tocar y el IVA
    devuelto es solo informativo (no se usa para completar nada en ARCA).
    Con discrimina_iva=True, ninguno de los dos se ajusta para que la suma
    dé EXACTO el total centavo a centavo (puede haber una diferencia de
    $0,01 por redondeo, común en este tipo de cálculo y que ARCA tolera).
    """
    total = float(importe_total or 0)

    if not discrimina_iva:
        return round(total, 2), 0.0

    if alicuota_iva in ("NO_GRAVADO", "EXENTO"):
        return round(total, 2), 0.0

    try:
        porcentaje = float(alicuota_iva)
    except (TypeError, ValueError):
        raise ValueError(f"Alícuota de IVA inválida: {alicuota_iva!r}")

    if porcentaje <= 0:
        return round(total, 2), 0.0

    neto = total / (1 + porcentaje / 100)
    iva = total - neto
    return round(neto, 2), round(iva, 2)


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

    Responsable Inscripto (Factura A y B, únicos tipos confirmados con
    grabación real por ahora): el Paso 4 de ARCA (revisión final y
    confirmación) resultó ser LA MISMA pantalla y los mismos botones que ya
    estaban grabados para Monotributo (confirmado con una grabación real de
    Factura B a Consumidor Final, con CAE emitido de verdad) -- por eso usa
    la misma confirmar_y_facturar() de más abajo, sin distinción entre
    regímenes en este paso.

    Devuelve un dict {"fecha_usada": "DD/MM/AAAA", "fecha_ajustada": bool}:
    fecha_usada es la fecha que REALMENTE se escribió en ARCA (puede no ser
    la de comprobante.fecha_comprobante, ver calcular_fecha_facturacion);
    fecha_ajustada avisa si hubo que corregirla -- para que quien llama
    pueda avisarle al usuario en vez de dejar el cambio pasar en silencio.
    """
    empresa = comprobante.empresa
    es_ri = empresa.tipo_contribuyente == "Responsable Inscripto"

    if es_ri and (comprobante.tipo_comprobante or "").strip() not in TIPOS_COMPROBANTE_RI_CONFIRMADOS:
        raise ValueError(
            f"El comprobante #{comprobante.id} es \"{comprobante.tipo_comprobante}\" -- todavía no está "
            "grabado con Playwright cómo se carga ese tipo en ARCA para Responsable Inscripto (solo están "
            "confirmadas Factura A y Factura B por ahora). Facturalo a mano en ARCA mientras tanto."
        )

    # Se juntan TODOS los problemas de datos en una sola pasada -- antes,
    # cada chequeo cortaba con su propio ValueError apenas encontraba el
    # primer problema, así que si un comprobante tenía dos o tres campos
    # mal, el cliente tenía que "Reintentar" varias veces (uno por vuelta)
    # para enterarse de todos. Ahora el mensaje de "Ver error" trae la
    # lista completa de una sola vez.
    problemas = []

    campos_obligatorios = [
        ("Punto de venta", comprobante.punto_venta),
        ("Tipo de factura", comprobante.tipo_comprobante),
        ("Concepto", comprobante.concepto),
        ("Condición de IVA", comprobante.condicion_iva),
        ("Tipo de documento", comprobante.tipo_documento),
        ("Condición de venta", comprobante.condicion_venta),
        ("Descripción", comprobante.descripcion),
        ("Unidad de medida", comprobante.unidad_medida),
    ]
    if comprobante.medio_pago_detectado in ("Débito", "Crédito"):
        campos_obligatorios += [
            ("Tipo de tarjeta", comprobante.tipo_pago),
            ("Número de tarjeta", comprobante.numero_pago),
        ]

    campos_faltantes = [nombre for nombre, valor in campos_obligatorios if not valor]
    if not empresa.razon_social_arca:
        campos_faltantes.insert(0, "Razón Social ARCA de la empresa (se carga en Empresas > Editar)")
    if campos_faltantes:
        problemas.append("faltan completar estos campos: " + ", ".join(campos_faltantes))

    # "Otra..." en Tipo de tarjeta necesita el texto del Detalle para poder
    # completar la casilla de Descripción que aparece al lado en ARCA -- sin
    # eso, ARCA no deja avanzar de esa pantalla.
    if comprobante.tipo_pago == "Otra..." and not comprobante.tipo_pago_detalle:
        problemas.append(
            'tiene "Otra..." como Tipo de tarjeta pero le falta el Detalle (ej. "VISA Débito")'
        )

    # Cualquier comprobante "clase A" (Factura A, Nota de Débito/Crédito A,
    # Recibo A, FCE A -- todos terminan en " A") exige CUIT del receptor
    # SIEMPRE, sin importar qué Condición de IVA tenga. Además, sea cual sea
    # el tipo de comprobante: si el operador eligió "CUIT" o "CUIL" como
    # Tipo de documento, ese número tiene que estar completo -- solo con
    # "DNI" puede quedar vacío. Se corta acá para no llegar hasta ARCA y que
    # rebote recién ahí.
    es_clase_a = (comprobante.tipo_comprobante or "").strip().endswith(" A")
    tipo_doc = (comprobante.tipo_documento or "").strip().upper()
    exige_numero_documento = es_clase_a or tipo_doc in ("CUIT", "CUIL")
    if exige_numero_documento and not comprobante.cuit_receptor:
        problemas.append(
            (
                f'es "{comprobante.tipo_comprobante}" -- ese tipo exige CUIT del receptor sin importar su Condición de IVA'
                if es_clase_a else
                f'tiene "{comprobante.tipo_documento}" como Tipo de documento -- hace falta completar el número de CUIT/CUIL'
            )
        )

    if problemas:
        raise ValueError(
            f"El comprobante #{comprobante.id} de '{empresa.nombre_interno}' no se puede facturar todavía: "
            + "; además, ".join(problemas)
            + ". Completalo en Revisión Manual (o en la configuración de la empresa, si es la Razón Social "
            "ARCA) y volvé a intentar."
        )

    if es_ri:
        if not comprobante.alicuota_iva or comprobante.alicuota_iva not in MAPA_ALICUOTA_IVA_ARCA:
            raise ValueError(
                f"El comprobante #{comprobante.id} no tiene una Alícuota de IVA válida cargada -- "
                "completala antes de facturar."
            )
        for linea_extra in comprobante.lineas_extra:
            campos_linea = [linea_extra.descripcion, linea_extra.unidad_medida, linea_extra.alicuota_iva]
            if not all(campos_linea) or linea_extra.alicuota_iva not in MAPA_ALICUOTA_IVA_ARCA:
                raise ValueError(
                    f"El comprobante #{comprobante.id} tiene una línea de producto extra sin completar "
                    "(descripción, unidad de medida o alícuota) -- completala antes de facturar."
                )
        # El Paso 4 (revisión final y confirmación) ya está confirmado para
        # este régimen -- ver el comentario de más arriba, en el docstring de
        # esta función -- así que de acá para abajo Responsable Inscripto
        # sigue el mismo camino que Monotributo en ese paso.

    # Un comprobante con $0 (o un monto irrisorio) es señal segura de que el
    # lector no pudo leer bien la imagen -- facturarlo así generaría una
    # factura inválida en ARCA. Se corta acá, antes de tocar el navegador.
    if (comprobante.importe_total or 0) < 100:
        raise ValueError(
            f"El comprobante #{comprobante.id} tiene un monto de ${comprobante.importe_total or 0:.2f} "
            "-- revisalo antes de facturar (mínimo $100)."
        )

    fecha_facturacion = comprobante.fecha_facturacion_manual or calcular_fecha_facturacion(
        comprobante.fecha_comprobante, empresa.config_dias_atras_fecha_emision
    )

    cuil, password = obtener_credenciales(empresa)

    _registrar_actividad_bot(
        empresa_id=empresa.id, comprobante_id=comprobante.id,
        nivel="info", evento="inicio_comprobante",
        detalle=(
            f"Arranca facturación del comprobante #{comprobante.id} ({empresa.nombre_interno}) -- "
            f"fecha real: {comprobante.fecha_comprobante}, fecha a usar: {fecha_facturacion}, "
            f"modo_prueba={modo_prueba}."
        ),
    )

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
                empresa_id=empresa.id, comprobante_id=comprobante.id,
            )

            if es_ri:
                completar_receptor_ri(
                    ventana,
                    comprobante.condicion_iva,
                    comprobante.tipo_documento,
                    comprobante.cuit_receptor,
                    comprobante.condicion_venta,
                    medio_pago_detectado=comprobante.medio_pago_detectado,
                    tipo_pago=comprobante.tipo_pago,
                    tipo_pago_detalle=comprobante.tipo_pago_detalle,
                    numero_pago=comprobante.numero_pago,
                )
                neto, _iva = descomponer_neto_iva(comprobante.precio_unitario, comprobante.alicuota_iva, es_clase_a)
                lineas = [{
                    "descripcion": comprobante.descripcion,
                    "cantidad": comprobante.cantidad,
                    "unidad_medida": comprobante.unidad_medida,
                    "precio_unitario_neto": neto,
                    "alicuota_iva": comprobante.alicuota_iva,
                }]
                for linea_extra in comprobante.lineas_extra:
                    neto_extra, _ = descomponer_neto_iva(linea_extra.precio_unitario, linea_extra.alicuota_iva, es_clase_a)
                    lineas.append({
                        "descripcion": linea_extra.descripcion,
                        "cantidad": linea_extra.cantidad,
                        "unidad_medida": linea_extra.unidad_medida,
                        "precio_unitario_neto": neto_extra,
                        "alicuota_iva": linea_extra.alicuota_iva,
                    })
                completar_lineas_productos_ri(ventana, lineas)
                # El Paso 4 (revisión final y confirmación) es la MISMA
                # pantalla que en Monotributo -- confirmado con una
                # grabación real de Factura B a Consumidor Final, CAE
                # emitido de verdad -- así que reusa exactamente la misma
                # función en vez de tener una copia aparte para Responsable
                # Inscripto.
                confirmar_y_facturar(ventana, modo_prueba=modo_prueba)
            else:
                completar_receptor(
                    ventana,
                    comprobante.condicion_iva,
                    comprobante.tipo_documento,
                    comprobante.condicion_venta,
                    medio_pago_detectado=comprobante.medio_pago_detectado,
                    tipo_pago=comprobante.tipo_pago,
                    tipo_pago_detalle=comprobante.tipo_pago_detalle,
                    numero_pago=comprobante.numero_pago,
                )
                completar_detalle(
                    ventana,
                    descripcion=comprobante.descripcion,
                    unidad_medida=comprobante.unidad_medida,
                    importe=comprobante.precio_unitario,
                )
                confirmar_y_facturar(ventana, modo_prueba=modo_prueba)
        except Exception as error:
            _registrar_actividad_bot(
                empresa_id=empresa.id, comprobante_id=comprobante.id,
                nivel="error", evento="excepcion_comprobante",
                detalle=f"Falló la facturación del comprobante #{comprobante.id}: {error}",
            )
            raise
        finally:
            contexto.close()
            navegador.close()

    _registrar_actividad_bot(
        empresa_id=empresa.id, comprobante_id=comprobante.id,
        nivel="info", evento="fin_comprobante",
        detalle=(
            f"Comprobante #{comprobante.id} facturado OK -- fecha usada: {fecha_realmente_usada}"
            + (" (AJUSTADA respecto de la fecha real del comprobante)"
               if fecha_realmente_usada != comprobante.fecha_comprobante else "")
            + f", modo_prueba={modo_prueba}."
        ),
    )

    return {
        "fecha_usada": fecha_realmente_usada,
        "fecha_ajustada": fecha_realmente_usada != comprobante.fecha_comprobante,
    }

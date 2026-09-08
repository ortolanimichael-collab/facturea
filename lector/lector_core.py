import pytesseract
from PIL import Image, ImageOps, ImageEnhance
import os
import pandas as pd
import re
import sys
from datetime import datetime
try:
    import pdfplumber
except ImportError:
    print("⚠️  pdfplumber no instalado. Ejecuta: pip install pdfplumber")
    pdfplumber = None

try:
    import fitz  # pymupdf — pip install pymupdf
except ImportError:
    print("⚠️  pymupdf no instalado. Ejecuta: pip install pymupdf")
    fitz = None

# Configuración de Tesseract
# En Windows (PC local) hay que apuntar al ejecutable instalado.
# En el servidor (Linux) Tesseract se instala vía apt y ya queda en el PATH,
# así que pytesseract lo encuentra solo sin necesidad de esta línea.
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

MESES = {
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
    'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
    'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12'
}

MESES_ABREV = {
    'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'ago': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12'
}

def _fecha_emision_efectiva(fecha_interfaz, fecha_servicio):
    """
    ⚠️ YA NO SE USA (agosto 2026) -- quedó reemplazada por el ajuste de fecha
    de facturación que se calcula por empresa en automatizacion/arca_bot.py
    (calcular_fecha_facturacion), usando el "hoy - X días" configurable ahí
    -- no "hoy" a secas, que es lo que esta función recibía como
    fecha_interfaz y terminaba pisando la fecha real del comprobante con la
    de HOY en vez de con el default correcto. Se deja el código por si hace
    falta como referencia, pero ningún llamado activo la usa.

    Comparaba la fecha del comprobante (fecha_servicio) con la fecha de emisión
    colocada en el Robot (fecha_interfaz).
    Si la fecha del comprobante es POSTERIOR a la del Robot, se usa la del comprobante
    como fecha de emisión. De lo contrario, se respeta la del Robot.
    Formato esperado: DD/MM/YYYY.
    Retorna: (fecha_final, fecha_es_posterior) donde fecha_es_posterior es True
             si la fecha del comprobante superó a la del Robot.
    """
    try:
        dt_interfaz = datetime.strptime(fecha_interfaz, '%d/%m/%Y')
        dt_servicio = datetime.strptime(fecha_servicio,  '%d/%m/%Y')
        if dt_servicio > dt_interfaz:
            return fecha_servicio, True
    except (ValueError, TypeError):
        pass
    return fecha_interfaz, False


def preprocesar_imagen(ruta):
    img = Image.open(ruta).convert('L')
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    img = ImageOps.autocontrast(img)
    img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
    img = img.point(lambda x: 0 if x < 200 else 255)
    return img

def preprocesar_imagen_oscura(ruta):
    """
    Preprocesamiento específico para comprobantes con fondo OSCURO (ej: Brubank).
    Invierte la imagen primero para que el texto claro se vuelva negro sobre blanco,
    luego aplica el mismo pipeline de mejora de contraste y binarización.
    """
    img = Image.open(ruta).convert('L')
    # Invertir: fondo negro → blanco, texto gris claro → oscuro
    img = ImageOps.invert(img)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.5)
    img = ImageOps.autocontrast(img)
    img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
    img = img.point(lambda x: 0 if x < 180 else 255)
    return img

def detectar_fondo_oscuro(ruta):
    """
    Retorna True si la imagen tiene fondo predominantemente oscuro.
    Calcula el brillo promedio: si < 100 sobre 255, se considera oscura.
    """
    try:
        img = Image.open(ruta).convert('L')
        # Muestra el centro de la imagen (evita bordes con fondos diferentes)
        w, h = img.size
        recorte = img.crop((w//4, h//4, 3*w//4, 3*h//4))
        pixeles = list(recorte.getdata())
        promedio = sum(pixeles) / len(pixeles)
        return promedio < 100
    except:
        return False

# Cada checkbox (Tarjeta de Débito / Tarjeta de Crédito) tiene su PROPIA lista
# de marcas en ARCA, con nombres que no siempre coinciden con lo que dice el
# comprobante real -- ej: un comprobante dice "Visa débito", pero la opción en
# ARCA para Débito se llama "Visa Electrón" (a secas "Visa" solo existe del
# lado de Crédito). Confirmado con capturas reales del desplegable de ARCA.
MAPA_MARCA_DEBITO = {
    "visa electron": "Visa Electrón", "visa electrón": "Visa Electrón",
    "mastercard": "Mastercard Débito", "master": "Mastercard Débito",
    "maestro": "Maestro",
    "cabal": "Cabal 24 hs",
}
MAPA_MARCA_CREDITO = {
    "visa": "Visa",
    "mastercard": "Mastercard", "master": "Mastercard",
    "american express": "American Express", "amex": "American Express",
    "cabal": "Cabal",
    "diners": "Diners", "diners club": "Diners",
    "naranja": "Tarjeta Naranja", "tarjeta naranja": "Tarjeta Naranja",
    "credencial": "Credencial",
    "carta franca": "Carta Franca",
    "shopping": "Tarjeta Shopping", "tarjeta shopping": "Tarjeta Shopping",
}


def _normalizar_marca_tarjeta(marca_cruda, medio_pago):
    """
    Traduce lo que dice el comprobante (ej: "Visa") a la opción EXACTA que
    tiene ARCA en el desplegable de Tipo, que depende de si es Débito o
    Crédito. Devuelve (marca, detalle):
    - "marca" es siempre una opción real del desplegable (incluye "Otra...").
    - "detalle" es el texto libre a cargar en la casilla que aparece al
      elegir "Otra...", o None si la marca ya es una opción directa.

    Caso especial: "Visa" a secas en DÉBITO (el caso más común en la
    práctica, confirmado con comprobantes reales de Mercado Pago Point y
    POS) NO es una opción real de ARCA para Débito -- ahí solo existe
    "Visa Electrón", que es un producto distinto. Antes esto se mapeaba mal
    a "Visa Electrón"; ahora se carga como "Otra..." con "VISA Débito" en
    el detalle, para no declarar una marca de tarjeta que no es la real.
    Solo se mapea a "Visa Electrón" cuando el texto trae explícitamente
    las dos palabras juntas ("visa electron"/"visa electrón", caso raro);
    si solo dice "Visa" a secas, siempre cae en el caso especial de arriba.
    """
    clave = marca_cruda.strip().lower()
    if medio_pago == "Débito" and clave == "visa":
        return "Otra...", "VISA Débito"
    mapa = MAPA_MARCA_DEBITO if medio_pago == "Débito" else MAPA_MARCA_CREDITO
    if clave in mapa:
        return mapa[clave], None
    return "Otra...", None


def _detectar_medio_pago(texto):
    """
    Busca la frase que usan las apps de cobro (confirmado con un comprobante
    real de Mercado Pago Point) cuando el pago fue con tarjeta:
    "<Marca> débito/crédito terminada en <dígitos>" -- ej: "Visa débito
    terminada en 2450". Si esa frase no aparece, se asume que es una
    transferencia (que es lo único que el lector reconocía hasta ahora).

    Devuelve (medio_pago, tipo_pago, numero_pago, tipo_pago_detalle).
    tipo_pago, numero_pago y tipo_pago_detalle quedan en None si es una
    transferencia (no aplica) o si no se pudo leer. tipo_pago ya viene
    traducido a la opción exacta que espera ARCA (ver
    _normalizar_marca_tarjeta); tipo_pago_detalle solo trae texto cuando
    tipo_pago es "Otra...".

    El "numero_pago" que se puede sacar de un comprobante real son SOLO los
    últimos dígitos que muestra el comprobante (nunca el número completo de
    la tarjeta -- eso no lo ve ni siquiera el que cobra, por seguridad).
    """
    patron = re.search(
        r'([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)\s+(d[eé]bito|cr[eé]dito)\s+terminada\s+en\s+(\d+)',
        texto, re.IGNORECASE,
    )
    if patron:
        marca_cruda = patron.group(1).strip()
        tipo_encontrado = patron.group(2).lower()
        numero = patron.group(3).strip()

        medio_pago = "Débito" if tipo_encontrado.startswith("d") else "Crédito"
        marca, detalle = _normalizar_marca_tarjeta(marca_cruda, medio_pago)

        return medio_pago, marca, numero, detalle

    # Formato "SmartPos" / lectores de tarjeta tipo POS (confirmado con un
    # comprobante real): "Tarjeta Visa débito Débito **3453" -- la marca y
    # el tipo aparecen juntos (a veces repetidos: "débito" en minúscula
    # como parte del nombre del producto de la tarjeta, y de nuevo
    # "Débito" como etiqueta del tipo de pago), y el número que se ve son
    # los últimos dígitos detrás de un enmascarado con asteriscos, no la
    # frase "terminada en X".
    patron_pos = re.search(
        r'Tarjeta\s+([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)\s+(d[eé]bito|cr[eé]dito)\b'
        r'(?:\s+(?:d[eé]bito|cr[eé]dito))?'
        r'\s*\*{1,2}\s*(\d{3,6})\b',
        texto, re.IGNORECASE,
    )
    if patron_pos:
        marca_cruda = patron_pos.group(1).strip()
        tipo_encontrado = patron_pos.group(2).lower()
        numero = patron_pos.group(3).strip()

        medio_pago = "Débito" if tipo_encontrado.startswith("d") else "Crédito"
        marca, detalle = _normalizar_marca_tarjeta(marca_cruda, medio_pago)

        return medio_pago, marca, numero, detalle

    return "Transferencia", None, None, None


def limpiar_nombre(texto):
    if not texto: return ""
    t = texto.lower().strip()
    t = re.sub(r'[^a-záéíóúüñ\s]', '', t)
    # Quitar etiquetas de campo al inicio (con o sin separador)
    prefijos = r'^(nombre y apellido|razon social|cuenta destino|destinatario|transferido a|receptor|apellido|cliente|titular|nombre|para|ei|el|la|los|sr|sra)\s*'
    t = re.sub(prefijos, '', t, flags=re.IGNORECASE)
    # Quitar artículos/preposiciones cortas que puedan quedar sueltos al inicio
    t = re.sub(r'^[a-z]{1,2}\s+', '', t)
    return t.strip().upper()

def _parsear_monto_argentino(enteros_str, centavos_str):
    """
    Convierte strings de monto en formato argentino a float.
    Ej: enteros='38.500', centavos='00' → 38500.0
        enteros='19.700', centavos='00' (BBVA con coma) → ya viene limpio
        enteros='10.00000', centavos=None (Personal Pay sin salto) → 10000.0
    """
    # Limpiar el string de enteros: quitar puntos (separadores de miles) y comas
    limpio = enteros_str.strip().replace(',', '').replace('.', '')
    if centavos_str:
        centavos = centavos_str.strip().replace('o', '0').replace('O', '0')
    else:
        centavos = '00'
    try:
        return float(f"{limpio}.{centavos}")
    except ValueError:
        return 0.0


def _ocr_pdf_como_imagen(ruta_pdf, nombre_archivo=""):
    """
    Convierte cada página del PDF a imagen y aplica OCR con Tesseract.
    Usa PyMuPDF (fitz) para renderizar — sin dependencia de Poppler.
    Instalar: pip install pymupdf
    """
    if fitz is None:
        print(f"  🔴 [OCR-PDF] pymupdf no instalado. Ejecuta: pip install pymupdf")
        return ""

    try:
        doc = fitz.open(ruta_pdf)
        print(f"  🟡 [OCR-PDF] '{nombre_archivo}' - {len(doc)} página(s) a renderizar con PyMuPDF")
    except Exception as e:
        print(f"  🔴 [OCR-PDF] '{nombre_archivo}' - Error al abrir PDF con PyMuPDF: {e}")
        return ""

    textos_paginas = []
    for i, pagina in enumerate(doc):
        # Renderizar a 250 DPI (matrix escala ~3.47x respecto a 72 DPI base)
        mat = fitz.Matrix(250 / 72, 250 / 72)
        pix = pagina.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)

        # Convertir pixmap a imagen PIL
        img_pil = Image.frombytes("L", (pix.width, pix.height), pix.samples)

        # Preprocesar: contraste, autocontraste, binarizar
        enhancer = ImageEnhance.Contrast(img_pil)
        img_pil = enhancer.enhance(2.0)
        img_pil = ImageOps.autocontrast(img_pil)
        img_pil = img_pil.point(lambda x: 0 if x < 200 else 255)

        texto_pagina = pytesseract.image_to_string(img_pil, lang="spa", config="--psm 6")
        print(f"  🟡 [OCR-PDF] Página {i+1}: {len(texto_pagina)} chars extraídos por OCR")
        textos_paginas.append(texto_pagina)

    doc.close()
    return "\n".join(textos_paginas)

def extraer_datos_de_pdf(ruta_pdf, fecha_interfaz, cuit_propio_cliente=""):
    """Extrae datos de comprobantes PDF con texto nativo (Claro Pay, Personal Pay, BBVA, etc.)"""
    nombre_archivo = os.path.basename(ruta_pdf)
    if pdfplumber is None:
        print(f"  🔴 [DEBUG-PDF] pdfplumber no disponible")
        return None
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            paginas_texto = []
            for i, p in enumerate(pdf.pages):
                t = p.extract_text() or ""
                paginas_texto.append(t)
                if not t.strip():
                    print(f"  🔴 [DEBUG-PDF] '{nombre_archivo}' - página {i+1} sin texto (PDF escaneado o imagen)")
            texto_raw = "\n".join(paginas_texto)

        if not texto_raw.strip():
            print(f"  🟡 [DEBUG-PDF] '{nombre_archivo}' - PDF sin texto nativo. Intentando OCR sobre páginas renderizadas...")
            texto_raw = _ocr_pdf_como_imagen(ruta_pdf, nombre_archivo)
            if not texto_raw.strip():
                print(f"  🔴 [DEBUG-PDF] '{nombre_archivo}' - OCR también vacío. No se pudo extraer texto.")
                return None
            print(f"  🟢 [DEBUG-PDF] '{nombre_archivo}' - OCR exitoso ({len(texto_raw)} chars)")

        # ── VOLCADO DE TEXTO EXTRAÍDO (primeras 800 chars) ────────────────────
        print(f"  🟡 [DEBUG-PDF] '{nombre_archivo}' - texto extraído ({len(texto_raw)} chars):")
        preview = texto_raw[:800].replace('\n', '↵')
        print(f"     >>>  {preview}  <<<")

        texto = texto_raw.lower()

        # ── DETECCIÓN DE BANCO/BILLETERA ──────────────────────────────────────
        es_claropay    = 'claro pay' in texto or 'claropay' in texto or 'nº de operación coelsa' in texto
        es_personalpay = ('personal pay' in texto or 'personalpay' in texto or
                          ('enviaste dinero' in texto and 'coelsaid' in texto) or
                          ('envía' in texto and 'recibe' in texto and 'coelsaid' in texto))
        es_bbva        = 'bbva' in texto or 'transferiste a' in texto
        # Brubank PDFs son imágenes incrustadas: pdfplumber no extrae texto nativo,
        # cae al OCR (_ocr_pdf_como_imagen). El OCR confunde "$" con "S", generando
        # "S 14.800,00" en vez de "$ 14.800,00". Se detecta por "brubank" en el texto.
        es_brubank_pdf = 'brubank' in texto and ('envio de dinero' in texto or 'envío de dinero' in texto)

        # ── DETECCIÓN UALÁ ────────────────────────────────────────────────────
        # Ualá siempre tiene "ualá" en el texto Y "comprobante de transferencia".
        # Formato 1 (Ualá app): campos "Fecha y hora", "Monto debitado",
        #                        "Cuenta destino", "CUIT destino", "Id Op."
        # Formato 2 (Ualá web): campos "Monto", "Fecha", "Destinatario",
        #                        "Banco destino" (puede decir BRUBANK), "CUIT/CUIL destino",
        #                        "Emisor", "ID. Operación"
        # IMPORTANTE: "brubank" puede aparecer como banco DESTINO en comprobantes Ualá,
        # no significa que el comprobante sea de Brubank. Se prioriza la detección de Ualá.
        tiene_uala_palabra = 'ualá' in texto or 'uala' in texto
        tiene_comprobante  = 'comprobante de transferencia' in texto
        es_uala = tiene_uala_palabra and tiene_comprobante
        print(f"  🟡 [DEBUG-PDF] '{nombre_archivo}' - detección: "
              f"uala={tiene_uala_palabra}, comprobante_transferencia={tiene_comprobante}, "
              f"claropay={es_claropay}, personalpay={es_personalpay}, bbva={es_bbva}, "
              f"ES_UALA={es_uala}")
        # Formato 1: tiene "monto debitado" y "cuenta destino"
        es_uala_fmt1 = es_uala and 'monto debitado' in texto and 'cuenta destino' in texto
        # Formato 2: tiene "destinatario" y ("emisor" o "banco destino")
        es_uala_fmt2 = es_uala and 'destinatario' in texto and ('emisor' in texto or 'banco destino' in texto)

        # ── BLOQUE BRUBANK PDF: procesamiento y retorno temprano ────────────
        if es_brubank_pdf:
            print(f"  🟢 [DEBUG-BRUBANK-PDF] Entrando al bloque Brubank PDF.")

            # --- NOMBRE DESTINO ---
            # "Envío de dinero a\nJuan Maria Moya Herrera"
            nombre_razon_social = "CONSUMIDOR FINAL"
            m = re.search(r'[Ee]nv[ií]o\s+de\s+dinero\s+a\s*[\n\r]+([A-Za-záéíóúüñÁÉÍÓÚÜÑ][^\n]+)', texto_raw)
            if m:
                nombre_razon_social = m.group(1).strip().upper()
                print(f"  🟢 [DEBUG-BRUBANK-PDF] Nombre destino: {nombre_razon_social}")
            else:
                # fallback línea siguiente a "Envío de dinero a" en misma línea
                m = re.search(r'[Ee]nv[ií]o\s+de\s+dinero\s+a\s+([A-Za-záéíóúüñÁÉÍÓÚÜÑ][^\n]+)', texto_raw)
                if m:
                    nombre_razon_social = m.group(1).strip().upper()

            # --- CUIT DESTINO ---
            # "CUIT / CUIL  20-35269484-4"  (primera aparición = destino)
            cuit = "0"
            patron_cuit_bru = r'CUIT\s*/\s*CUIL\s+([\d]{2}-[\d]{8}-[\d])'
            todos_cuits_bru = re.findall(patron_cuit_bru, texto_raw, re.IGNORECASE)
            print(f"  🟢 [DEBUG-BRUBANK-PDF] CUITs encontrados: {todos_cuits_bru}")
            for c_raw in todos_cuits_bru:
                c_limpio = re.sub(r'\D', '', c_raw)
                if c_limpio != cuit_propio_cliente and len(c_limpio) == 11:
                    cuit = c_limpio
                    print(f"  🟢 [DEBUG-BRUBANK-PDF] CUIT destino: {cuit}")
                    break

            # Otro CUIT/CUIL encontrado en el PDF (probablemente el emisor),
            # para ofrecer como alternativa en Revisión Manual.
            cuit_alternativo = "0"
            for c_raw in todos_cuits_bru:
                c_limpio = re.sub(r'\D', '', c_raw)
                if c_limpio != cuit and c_limpio != cuit_propio_cliente and len(c_limpio) == 11:
                    cuit_alternativo = c_limpio
                    break

            # --- FECHA ---
            # "09 de mayo de 2026 - 22:26"  o  "O9 de mayo de 2026" (OCR: 0→O)
            fecha_servicio = datetime.now().strftime('%d/%m/%Y')
            m = re.search(
                r'([O0]?\d|\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})',
                texto_raw, re.IGNORECASE
            )
            if m:
                dia_raw = re.sub(r'[^\d]', '', m.group(1)) or '01'
                dia = dia_raw.zfill(2)
                mes_txt = m.group(2).lower()[:3]
                anio = m.group(3)
                mes_num = MESES_ABREV.get(mes_txt, MESES.get(mes_txt, MESES.get(m.group(2).lower(), '01')))
                fecha_servicio = f"{dia}/{mes_num}/{anio}"
                print(f"  🟢 [DEBUG-BRUBANK-PDF] Fecha: {fecha_servicio}")

            # --- MONTO ---
            importe_encontrado = 0.0
            monto_total_texto = ""
            m = re.search(r'(?:\$|S)\s*([\d]{1,3}(?:\.[\d]{3})*),([\d]{2})', texto_raw)
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
            if importe_encontrado == 0.0:
                m = re.search(r'\b([\d]{1,3}(?:\.[\d]{3})+),([\d]{2})\b', texto_raw)
                if m:
                    importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))

            if importe_encontrado > 0:
                monto_total_texto = f"{importe_encontrado:,.2f}".replace(',','X').replace('.', ',').replace('X','.')
                print(f"  🟢 [DEBUG-BRUBANK-PDF] Monto final: {monto_total_texto}")

            # --- ID (Brubank no tiene número de operación visible, generamos sintético) ---
            nro_movimiento = f"BRU-{fecha_servicio.replace('/','')}-{re.sub(chr(46),'',str(importe_encontrado))}"

            # --- RETORNO ---
            cuit_valido = cuit if cuit != "0" else ""
            cuit_alternativo_valido = cuit_alternativo if cuit_alternativo != "0" else ""
            tipo_doc = "CUIT" if cuit_valido else "DNI"
            fecha_emision_final = fecha_servicio  # la fecha real detectada, sin "pisar" con hoy (ver nota en _fecha_emision_efectiva más arriba)
            medio_pago, tipo_pago_detectado, numero_pago_detectado, tipo_pago_detalle_detectado = _detectar_medio_pago(texto_raw)
            condicion_venta_detectada = {"Débito": "Tarjeta de Débito", "Crédito": "Tarjeta de Crédito"}.get(medio_pago)  # None si es Transferencia -> deja que gane la config de la empresa, no se fuerza "Contado"
            return {
                "Tipo Documento": tipo_doc,
                "CUIT Receptor": cuit_valido,
                "CUIT Alternativo": cuit_alternativo_valido,
                "Nombre / Razón Social": nombre_razon_social if len(nombre_razon_social) > 2 else "CONSUMIDOR FINAL",
                "Nombre Remitente": "No detectado",  # TODO: extracción de remitente pendiente para este formato PDF
                "Fecha del Comprobante": fecha_emision_final,
                "Condicion IVA": "Consumidor Final",
                "Condicion Venta": condicion_venta_detectada,
                "Medio Pago": medio_pago,
                "Tipo Pago": tipo_pago_detectado,
                "Tipo Pago Detalle": tipo_pago_detalle_detectado,
                "Numero Pago": numero_pago_detectado,
                "Fecha Desde": fecha_servicio,
                "Fecha Hasta": fecha_servicio,
                "Importe Total": importe_encontrado,
                "Monto Texto Completo": monto_total_texto,
                "Archivo Origen": os.path.basename(ruta_pdf),
                "Nro Movimiento": nro_movimiento,
                "ID_Transaccion": nro_movimiento
            }

        # ── BLOQUE UALÁ: procesamiento completo y retorno temprano ────────────
        if es_uala:
            print(f"  🟢 [DEBUG-UALÁ] Entrando al bloque Ualá. fmt1={es_uala_fmt1}, fmt2={es_uala_fmt2}")

            # --- NOMBRE (destinatario) ---
            nombre_razon_social = "CONSUMIDOR FINAL"
            # Formato 1: "Cuenta destino  Celeste Maria Giselle Rodriguez"
            m = re.search(r'[Cc]uenta\s+destino\s+([A-Za-záéíóúüñÁÉÍÓÚÜÑ][^\n]+)', texto_raw)
            if m:
                nombre_razon_social = m.group(1).strip().upper()
                print(f"  🟢 [DEBUG-UALÁ] Nombre (fmt1 'Cuenta destino'): {nombre_razon_social}")
            # Formato 2: "Destinatario  Celeste Maria Giselle Rodriguez"
            if nombre_razon_social == "CONSUMIDOR FINAL":
                m = re.search(r'[Dd]estinatario\s+([A-Za-záéíóúüñÁÉÍÓÚÜÑ][^\n]+)', texto_raw)
                if m:
                    nombre_razon_social = m.group(1).strip().upper()
                    print(f"  🟢 [DEBUG-UALÁ] Nombre (fmt2 'Destinatario'): {nombre_razon_social}")
                else:
                    print(f"  🔴 [DEBUG-UALÁ] Nombre NO encontrado. Líneas con 'dest': {[l for l in texto_raw.splitlines() if 'dest' in l.lower() or 'cuenta' in l.lower()][:5]}")

            # --- CUIT ---
            cuit = "0"
            patron_cuit = r'\b(20|23|24|27|30|33|34)-?(\d{8})-?(\d)\b'
            todos_cuits = re.findall(patron_cuit, texto_raw)
            cuits_limpios = [''.join(c) for c in todos_cuits]
            print(f"  🟢 [DEBUG-UALÁ] CUITs encontrados en texto: {cuits_limpios}")
            # Formato 1: "CUIT destino  27353876932"
            m = re.search(r'CUIT\s+destino\s+([\d\s\-]+)', texto_raw, re.IGNORECASE)
            if m:
                cuit_raw = re.sub(r'\D', '', m.group(1).split('\n')[0])
                print(f"  🟢 [DEBUG-UALÁ] CUIT destino raw='{m.group(1)[:30]}' → limpio='{cuit_raw}' (len={len(cuit_raw)})")
                if len(cuit_raw) >= 10:
                    cuit = cuit_raw
            # Formato 2: "CUIT/CUIL destino  27-35387693-2"
            if cuit == "0":
                m = re.search(r'CUIT/CUIL\s+destino\s+([\d\-\s]+)', texto_raw, re.IGNORECASE)
                if m:
                    cuit_raw = re.sub(r'\D', '', m.group(1).split('\n')[0])
                    print(f"  🟢 [DEBUG-UALÁ] CUIT/CUIL destino raw='{m.group(1)[:30]}' → limpio='{cuit_raw}' (len={len(cuit_raw)})")
                    if len(cuit_raw) >= 10:
                        cuit = cuit_raw
                else:
                    print(f"  🔴 [DEBUG-UALÁ] No se encontró 'CUIT destino' ni 'CUIT/CUIL destino'. Líneas con 'cuit': {[l for l in texto_raw.splitlines() if 'cuit' in l.lower()][:5]}")
            # Fallback: primer CUIT que no sea el propio
            if cuit == "0" and cuits_limpios:
                for c in cuits_limpios:
                    if c != cuit_propio_cliente:
                        cuit = c
                        print(f"  🟡 [DEBUG-UALÁ] CUIT por fallback regex: {cuit}")
                        break

            # Otro CUIT/CUIL encontrado en la imagen (probablemente el emisor),
            # para ofrecer como alternativa en Revisión Manual -- se calcula
            # ANTES de descartar el propio, así si el elegido resulta ser el
            # de la empresa, hay un candidato listo para poner en su lugar
            # en vez de dejar el campo vacío sin necesidad.
            cuit_alternativo = "0"
            for c in cuits_limpios:
                if c != cuit and c != cuit_propio_cliente:
                    cuit_alternativo = c
                    break

            if cuit_propio_cliente and cuit == cuit_propio_cliente:
                # El elegido era el CUIT propio de la empresa (el emisor, no
                # el receptor) -- se usa el otro que se haya encontrado en
                # su lugar; si no hay otro, queda vacío (Tipo Documento =
                # DNI más abajo, vía cuit_valido).
                cuit = cuit_alternativo
                cuit_alternativo = "0"
            print(f"  🟢 [DEBUG-UALÁ] CUIT final: {cuit}")

            # --- FECHA ---
            fecha_servicio = datetime.now().strftime('%d/%m/%Y')
            # Formato 1: "Fecha y hora  30/04/2026 23:02 hs"
            m = re.search(r'[Ff]echa\s+y\s+hora\s+(\d{1,2}/\d{2}/\d{4})', texto_raw)
            if m:
                fecha_servicio = m.group(1)
                print(f"  🟢 [DEBUG-UALÁ] Fecha (fmt1 'Fecha y hora'): {fecha_servicio}")
            else:
                # Formato 2: "Fecha  1 de may del 2026 - 00:24hs"
                # o         "Fecha  30 de abr del 2026 - 22:50hs"
                m = re.search(r'[Ff]echa\s+(\d{1,2})\s+de\s+(\w+)\s+del?\s+(\d{4})', texto_raw)
                if m:
                    dia = m.group(1).zfill(2)
                    mes_txt = m.group(2).lower()[:3]
                    anio = m.group(3)
                    mes_num = MESES_ABREV.get(mes_txt, MESES.get(mes_txt, '01'))
                    fecha_servicio = f"{dia}/{mes_num}/{anio}"
                    print(f"  🟢 [DEBUG-UALÁ] Fecha (fmt2 'X de mes del YYYY'): {fecha_servicio}")
                else:
                    # fallback dd/mm/yyyy genérico
                    m = re.search(r'\b(\d{1,2})[/\-](\d{2})[/\-](\d{4})\b', texto_raw)
                    if m:
                        fecha_servicio = f"{m.group(1).zfill(2)}/{m.group(2)}/{m.group(3)}"
                        print(f"  🟡 [DEBUG-UALÁ] Fecha (fallback dd/mm/yyyy): {fecha_servicio}")
                    else:
                        print(f"  🔴 [DEBUG-UALÁ] Fecha NO encontrada. Líneas con 'fecha': {[l for l in texto_raw.splitlines() if 'fecha' in l.lower()][:5]}")

            # --- MONTO ---
            importe_encontrado = 0.0
            monto_total_texto = ""
            # Formato 1: "Monto debitado  $16.900,00"
            m = re.search(r'[Mm]onto\s+debitado\s+\$\s*([\d\.]+),([\d]{2})', texto_raw)
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                print(f"  🟢 [DEBUG-UALÁ] Monto (fmt1 'Monto debitado'): {importe_encontrado}")
            # Formato 2: "Monto  $ 21.000,00"
            if importe_encontrado == 0.0:
                m = re.search(r'[Mm]onto\s+\$\s*([\d\.]+),([\d]{2})', texto_raw)
                if m:
                    importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                    print(f"  🟢 [DEBUG-UALÁ] Monto (fmt2 'Monto $'): {importe_encontrado}")
            # Formato 3: "Comprobante de transferencia\n$14.600" -- el monto es el
            # título grande de la pantalla, sin la palabra "Monto" al lado y SIN
            # centavos (no trae ",00"). Confirmado con un comprobante real de este
            # formato que las reglas de arriba no llegaban a encontrar porque
            # todas piden coma + 2 dígitos decimales.
            if importe_encontrado == 0.0:
                m = re.search(r'\$\s*([\d]{1,3}(?:\.[\d]{3})*)(?:,([\d]{2}))?\b', texto_raw)
                if m:
                    importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2) or "00")
                    print(f"  🟢 [DEBUG-UALÁ] Monto (fmt3 '$ sin centavos'): {importe_encontrado}")
            # Fallback: cualquier $ X.XXX,XX en el documento
            if importe_encontrado == 0.0:
                m = re.search(r'\$\s*([\d\.]+),([\d]{2})', texto_raw)
                if m:
                    importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                    print(f"  🟡 [DEBUG-UALÁ] Monto (fallback $ X,XX): {importe_encontrado}")
                else:
                    print(f"  🔴 [DEBUG-UALÁ] Monto NO encontrado. Líneas con '$': {[l for l in texto_raw.splitlines() if '$' in l][:5]}")
            if importe_encontrado > 0:
                monto_total_texto = f"{importe_encontrado:,.2f}".replace(',','X').replace('.', ',').replace('X','.')

            # --- ID DE TRANSACCIÓN ---
            nro_movimiento = "Desconocido"
            # Formato 1a: "Id Op.  Z6OLMDN3VRK376662E7RQ5"  (mismo renglón)
            # Formato 1b: "Id Op.\nZ6OLMDN3VRK376662E7RQ5"  (OCR con salto de línea)
            # Formato 1c: "ld Op. Z6OLMDN3VRK376662E7RQ5"   (OCR confunde 'I' con 'l')
            m = re.search(r'[IiLl]d\.?\s*[Oo]p\.?\s*[\s\n]*([A-Z0-9]{10,40})', texto_raw)
            if m:
                nro_movimiento = m.group(1).strip().upper()
                print(f"  🟢 [DEBUG-UALÁ] ID (fmt1 'Id Op.'): {nro_movimiento}")
            # Formato 2: "ID. Operación  168c35f8-37a4-6d1f-b4ff-d7f27454819e"
            if nro_movimiento == "Desconocido":
                # La clase de caracteres incluye vocales acentuadas (áéíóú) porque el
                # OCR a veces "alucina" una tilde sobre una letra o número que en el
                # comprobante real no la tiene (confirmado con un ID real que salió
                # como "ORDó6LEN8..." en vez de "ORD6LEN8...") -- mejor capturar el ID
                # con ese ruido adentro que perderlo del todo y quedar "Desconocido".
                m = re.search(r'ID\.?\s+[Oo]peraci[oó]n\s*[\s\n]*([A-Za-z0-9\-áéíóúÁÉÍÓÚ]{10,50})', texto_raw)
                if m:
                    nro_movimiento = m.group(1).strip().upper()
                    print(f"  🟢 [DEBUG-UALÁ] ID (fmt2 'ID. Operación'): {nro_movimiento}")
            # Fallback: cualquier secuencia alfanumérica larga en línea posterior a "id" u "op"
            if nro_movimiento == "Desconocido":
                m = re.search(r'(?:[IiLl]d\.?\s*[Oo]p\.?|[Ii][Dd]\.?\s+[Oo]peraci[oó]n)\s*[\s\n]*([A-Za-z0-9\-]{10,50})', texto_raw)
                if m:
                    nro_movimiento = m.group(1).strip().upper()
                    print(f"  🟡 [DEBUG-UALÁ] ID (fallback genérico): {nro_movimiento}")
                else:
                    lineas_id_op = [l for l in texto_raw.splitlines() if re.search(r'\bid\b|\bop\b|operaci', l, re.IGNORECASE)][:5]
                    print(f"  🔴 [DEBUG-UALÁ] ID NO encontrado. Líneas con 'id' o 'op': {lineas_id_op}")

            cuit_valido = cuit if cuit != "0" else ""
            cuit_alternativo_valido = cuit_alternativo if cuit_alternativo != "0" else ""
            tipo_doc = "CUIT" if cuit_valido else "DNI"
            fecha_emision_final = fecha_servicio  # la fecha real detectada, sin "pisar" con hoy (ver nota en _fecha_emision_efectiva más arriba)
            medio_pago, tipo_pago_detectado, numero_pago_detectado, tipo_pago_detalle_detectado = _detectar_medio_pago(texto_raw)
            condicion_venta_detectada = {"Débito": "Tarjeta de Débito", "Crédito": "Tarjeta de Crédito"}.get(medio_pago)  # None si es Transferencia -> deja que gane la config de la empresa, no se fuerza "Contado"
            print(f"  💳 [Ualá {'Fmt1' if es_uala_fmt1 else 'Fmt2'}] {nombre_razon_social[:20]} | ${monto_total_texto} | ID: {nro_movimiento[:15]}")
            return {
                "Tipo Documento": tipo_doc,
                "CUIT Receptor": cuit_valido,
                "CUIT Alternativo": cuit_alternativo_valido,
                "Nombre / Razón Social": nombre_razon_social if len(nombre_razon_social) > 2 else "CONSUMIDOR FINAL",
                "Nombre Remitente": "No detectado",  # TODO: extracción de remitente pendiente para este formato PDF
                "Fecha del Comprobante": fecha_emision_final,
                "Condicion IVA": "Consumidor Final",
                "Condicion Venta": condicion_venta_detectada,
                "Medio Pago": medio_pago,
                "Tipo Pago": tipo_pago_detectado,
                "Tipo Pago Detalle": tipo_pago_detalle_detectado,
                "Numero Pago": numero_pago_detectado,
                "Fecha Desde": fecha_servicio,
                "Fecha Hasta": fecha_servicio,
                "Importe Total": importe_encontrado,
                "Monto Texto Completo": monto_total_texto,
                "Archivo Origen": os.path.basename(ruta_pdf),
                "Nro Movimiento": nro_movimiento,
                "ID_Transaccion": nro_movimiento
            }
        # ── FIN BLOQUE UALÁ ───────────────────────────────────────────────────

        # ── NOMBRE ──────────────────────────────────────────────────────────
        nombre_razon_social = "CONSUMIDOR FINAL"

        # Personal Pay: "Envía  Apellido, Nombre" → invertir a "Nombre Apellido"
        m = re.search(r'Env[ií]a\s+([A-ZÁÉÍÓÚÑ][^\n]+)', texto_raw)
        if m:
            raw = m.group(1).strip().rstrip(',')
            partes = [p.strip() for p in raw.split(',')]
            nombre_razon_social = ' '.join(reversed(partes)) if len(partes) == 2 else raw

        # Claro Pay: "De:\nNombre Completo"
        if nombre_razon_social == "CONSUMIDOR FINAL":
            m = re.search(r'De:\s*\n\s*([A-ZÁÉÍÓÚÑ][^\n]+)', texto_raw)
            if m:
                nombre_razon_social = m.group(1).strip()

        # BBVA: "Titular  NOMBRE EN MAYUSCULAS"
        if nombre_razon_social == "CONSUMIDOR FINAL":
            m = re.search(r'Titular\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ ]+)', texto_raw)
            if m:
                nombre_razon_social = m.group(1).strip().title()

        nombre_razon_social = nombre_razon_social.upper().strip()

        # ── CUIT ─────────────────────────────────────────────────────────────
        cuit = "0"
        patron_cuit = r'\b(20|23|24|27|30|33|34)-?(\d{8})-?(\d)\b'
        todos_cuits = re.findall(patron_cuit, texto_raw)
        cuits_limpios = [''.join(c) for c in todos_cuits]

        # Personal Pay: el CUIT de "Recibe" es el destinatario (nuestro cliente)
        m_recibe = re.search(r'Recibe.*?CUIL/CUIT\s+([\d\-]+)', texto_raw, re.DOTALL | re.IGNORECASE)
        if m_recibe:
            cuit_recibe = re.sub(r'\D', '', m_recibe.group(1))
            if cuit_recibe != cuit_propio_cliente:
                cuit = cuit_recibe
            else:
                for c in cuits_limpios:
                    if c != cuit_recibe:
                        cuit = c
                        break

        # Claro Pay: CUIT/CUIL bajo "Para:"
        if cuit == "0":
            m_para = re.search(r'Para:.*?CUIT/CUIL\s+([\d]+)', texto_raw, re.DOTALL | re.IGNORECASE)
            if m_para:
                cuit = re.sub(r'\D', '', m_para.group(1))

        # BBVA: "CUIT destinatario XXXXXXXXXXX"
        if cuit == "0":
            m = re.search(r'CUIT\s+destinatario\s+(\d{10,11})', texto_raw, re.IGNORECASE)
            if m: cuit = m.group(1)

        # Fallback: primer CUIT que no sea el propio
        if cuit == "0" and cuits_limpios:
            for c in cuits_limpios:
                if c != cuit_propio_cliente:
                    cuit = c
                    break

        # Si el lector encontró más de un CUIT/CUIL en la imagen (lo normal:
        # uno es el emisor y el otro el receptor), se guarda el otro como
        # alternativa -- en Revisión Manual se puede elegir ese en vez del
        # que se tomó acá, por si el lector se equivocó de cuál es cuál.
        cuit_alternativo = "0"
        for c in cuits_limpios:
            if c != cuit and c != cuit_propio_cliente:
                cuit_alternativo = c
                break

        # Si el CUIT encontrado es el propio del cliente → se usa el otro
        # que se haya encontrado en su lugar; si no hay otro, queda vacío.
        if cuit_propio_cliente and cuit == cuit_propio_cliente:
            cuit = cuit_alternativo
            cuit_alternativo = "0"

        # ── FECHA ─────────────────────────────────────────────────────────────
        fecha_servicio = datetime.now().strftime('%d/%m/%Y')

        # Formato "dd/mm/yyyy"
        m = re.search(r'\b(\d{1,2})[/\-](\d{2})[/\-](\d{4})\b', texto_raw)
        if m:
            fecha_servicio = f"{m.group(1).zfill(2)}/{m.group(2)}/{m.group(3)}"
        else:
            # "25 de Abril a las..."
            for mes_nombre, mes_num in MESES.items():
                m2 = re.search(rf'(\d{{1,2}})\s+de\s+{mes_nombre}', texto, re.IGNORECASE)
                if m2:
                    fecha_servicio = f"{m2.group(1).zfill(2)}/{mes_num}/2026"
                    break

        # ── MONTO ─────────────────────────────────────────────────────────────
        importe_encontrado = 0.0
        monto_total_texto = ""

        # ── PATRÓN CLARO PAY ──────────────────────────────────────────────────
        # pdfplumber extrae el texto de Claro Pay en este orden EXACTO:
        #   "Comprobante de transferencia\n38.50000\n$\n25 de Abril..."
        #
        # IMPORTANTE: el número aparece ANTES del simbolo $, y los ultimos 2
        # digitos son los centavos pegados. Es decir "38.50000" = 38.500 + 00.
        # El $ queda solo en la linea siguiente al numero.
        if es_claropay:
            m = re.search(
                r'[Cc]omprobante\s+de\s+transferencia\s*\n\s*(\d{1,3}(?:\.\d{3})*)(\d{2})\s*\n\s*\$',
                texto_raw
            )
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                metodo_debug = f"ClaroPay: {m.group(1)}+{m.group(2)}"

        # ── PATRÓN PERSONAL PAY ───────────────────────────────────────────────
        # pdfplumber extrae el texto de Personal Pay en este orden EXACTO
        # (igual que Claro Pay: numero ANTES del $):
        #
        #   "Enviaste dinero\n10.00000\n$\nFecha 25/04/2026..."
        #   "Enviaste dinero\n12.50000\n$\nFecha 19/04/2026..."
        #   "Enviaste dinero\n20.20000\n$\nFecha 25/04/2026..."
        #   "Enviaste dinero\n90000\n$\nFecha 25/04/2026..."    ← sin punto (monto chico)
        #   "Enviaste dinero\n20.00000\n$\nFecha 25/04/2026..."
        #
        # El numero siempre tiene los ultimos 2 digitos como centavos pegados.
        # El $ queda solo en la linea siguiente al numero.
        if es_personalpay and importe_encontrado == 0.0:
            m = re.search(
                r'[Ee]nviaste\s+dinero\s*\n\s*(\d{1,3}(?:\.\d{3})*)(\d{2})\s*\n\s*\$',
                texto_raw
            )
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))

        # ── PATRÓN BRUBANK PDF ───────────────────────────────────────────────
        # El OCR de páginas Brubank confunde "$" con "S" (misma forma visual).
        # El monto aparece como: "S 14.800,00" o "$ 14.800,00" (punto=miles, coma=decimal)
        # Se busca tanto "S" como "$" para cubrir ambos casos.
        if es_brubank_pdf and importe_encontrado == 0.0:
            # Patrón: "S 14.800,00" o "$ 14.800,00" — acepta S o $ como símbolo
            m = re.search(r'(?:\$|S)\s*([\d]{1,3}(?:\.[\d]{3})*),([\d]{2})', texto_raw)
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                print(f"  💰 [Brubank-PDF] Monto detectado: {importe_encontrado} (raw: '{m.group(0)}')")
            # Fallback: número grande con coma decimal sin símbolo previo
            if importe_encontrado == 0.0:
                m = re.search(r'\b([\d]{1,3}(?:\.[\d]{3})+),([\d]{2})\b', texto_raw)
                if m:
                    importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))
                    print(f"  💰 [Brubank-PDF] Monto fallback: {importe_encontrado} (raw: '{m.group(0)}')")

        # ── PATRÓN B: BBVA "$ 19.700,00" (punto miles, coma decimal)
        if importe_encontrado == 0.0:
            m = re.search(r'\$\s*([\d\.]+),([\d]{2})', texto_raw)
            if m:
                importe_encontrado = _parsear_monto_argentino(m.group(1), m.group(2))

        # ── PATRÓN C: fallback — tomar el maximo $ encontrado en el documento
        # Evita agarrar precios unitarios chicos tomando el valor mas grande
        if importe_encontrado == 0.0:
            todos_montos = re.findall(r'\$\s*(\d{1,3}(?:\.\d{3})*|\d+)', texto_raw)
            valores = []
            for t in todos_montos:
                try:
                    valores.append(float(t.replace('.', '')))
                except ValueError:
                    pass
            if valores:
                importe_encontrado = max(valores)

        if importe_encontrado > 0:
            monto_total_texto = f"{importe_encontrado:,.2f}".replace(',','X').replace('.', ',').replace('X','.')

        # ── ID DE TRANSACCIÓN ─────────────────────────────────────────────────
        nro_movimiento = "Desconocido"

        # CoelsaID (Personal Pay Receipt): "CoelsaID WY7ZEPN6... - Id Bancario"
        m = re.search(r'CoelsaID\s+([A-Z0-9]{15,30})', texto_raw, re.IGNORECASE)
        if m: nro_movimiento = m.group(1).upper()

        # Nº de operación COELSA (Claro Pay): "Nº de operación COELSA Z6OLMDN3..."
        if nro_movimiento == "Desconocido":
            m = re.search(r'operaci[oó]n\s+COELSA\s+([A-Z0-9]{15,30})', texto_raw, re.IGNORECASE)
            if m: nro_movimiento = m.group(1).upper()

        # Prex: "ID COELSA\nORD6LEN8PZ5XWJK4NM1Y30"
        if nro_movimiento == "Desconocido":
            m = re.search(r'ID\s+COELSA[\s\n:]*([A-Z0-9]{10,40})', texto_raw, re.IGNORECASE)
            if m: nro_movimiento = m.group(1).upper()

        # Ualá: "id Op. JMQKYZ9QQ75KPGRV9V50P3"
        if nro_movimiento == "Desconocido":
            m = re.search(r'id\s+Op\.?[\s\n]*([A-Z0-9]{10,40})', texto_raw, re.IGNORECASE)
            if m: nro_movimiento = m.group(1).upper()

        # BBVA: "Número de referencia 30260262007260425"
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Nn][uú]mero\s+de\s+referencia\s+(\d{10,25})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # BNA+: "Número de transacción 00744722"
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Nn][uú]mero\s+de\s+transacci[oó]n[\s\n:]*(\d{6,20})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # Mercado Pago: "Número de operación de Mercado Pago\n155706725481"
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Nn][uú]mero\s+de\s+operaci[oó]n\s+de\s+[Mm]ercado\s+[Pp]ago[\s\n:]*(\d{8,20})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # Personal Pay: "Nº de la\noperación  2534498847"
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Nn][º°o]\s*de\s+la\s*[\n\s]+operaci[oó]n\s+(\d{8,20})', texto_raw)
            if not m:
                m = re.search(r'[Nn][º°o]\s*de\s+la\s+operaci[oó]n\s+(\d{8,20})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # Santander: "Nº comprobante  71544747"
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Nn][\xba\xb0o2\?]?\.?\s*(?:ro\.?\s*|um\.?\s*)?(?:de\s+)?comprobante(?!\s+de\s)[\s\n:\-]*(\d{5,12})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # Banco Hipotecario y otros: "Operación: 00197462" (etiqueta sola seguida de número)
        # Se excluyen los casos ya atrapados arriba (COELSA, referencia, transacción, Nº de la operación)
        if nro_movimiento == "Desconocido":
            m = re.search(r'[Oo]peraci[oó]n[:\s\n]+(\d{5,20})', texto_raw)
            if m: nro_movimiento = m.group(1)

        # ── RESULTADO ─────────────────────────────────────────────────────────
        cuit_valido = cuit if cuit != "0" else ""
        cuit_alternativo_valido = cuit_alternativo if cuit_alternativo != "0" else ""
        tipo_doc = "CUIT" if cuit_valido else "DNI"

        fecha_emision_final = fecha_servicio  # la fecha real detectada, sin "pisar" con hoy (ver nota en _fecha_emision_efectiva más arriba)
        medio_pago, tipo_pago_detectado, numero_pago_detectado, tipo_pago_detalle_detectado = _detectar_medio_pago(texto_raw)
        condicion_venta_detectada = {"Débito": "Tarjeta de Débito", "Crédito": "Tarjeta de Crédito"}.get(medio_pago)  # None si es Transferencia -> deja que gane la config de la empresa, no se fuerza "Contado"
        return {
            "Tipo Documento": tipo_doc,
            "CUIT Receptor": cuit_valido,
            "CUIT Alternativo": cuit_alternativo_valido,
            "Nombre / Razón Social": nombre_razon_social if len(nombre_razon_social) > 2 else "CONSUMIDOR FINAL",
            "Nombre Remitente": "No detectado",  # TODO: extracción de remitente pendiente para este formato PDF
            "Fecha del Comprobante": fecha_emision_final,
            "Condicion IVA": "Consumidor Final",
            "Condicion Venta": condicion_venta_detectada,
            "Medio Pago": medio_pago,
            "Tipo Pago": tipo_pago_detectado,
            "Tipo Pago Detalle": tipo_pago_detalle_detectado,
            "Numero Pago": numero_pago_detectado,
            "Fecha Desde": fecha_servicio,
            "Fecha Hasta": fecha_servicio,
            "Importe Total": importe_encontrado,
            "Monto Texto Completo": monto_total_texto,
            "Archivo Origen": os.path.basename(ruta_pdf),
            "Nro Movimiento": nro_movimiento,
            "ID_Transaccion": nro_movimiento
        }
    except Exception as e:
        import traceback
        print(f"❌ ERROR PDF ({os.path.basename(ruta_pdf)}): {e}")
        print(f"  🔴 [DEBUG-PDF] Traceback completo:")
        traceback.print_exc()
        return None


def extraer_datos_de_imagen(ruta_imagen, fecha_interfaz, cuit_propio_cliente=""):
    try:
        # Detectar si es fondo oscuro (Brubank) para usar preprocesamiento invertido
        es_fondo_oscuro = detectar_fondo_oscuro(ruta_imagen)
        if es_fondo_oscuro:
            img_preprocesada = preprocesar_imagen_oscura(ruta_imagen)
        else:
            img_preprocesada = preprocesar_imagen(ruta_imagen)

        texto = pytesseract.image_to_string(img_preprocesada, lang='spa', config='--psm 11').lower()

        # Si fondo oscuro y el texto extraído es muy pobre, hacer segunda pasada con psm 6
        if es_fondo_oscuro and len(texto.strip()) < 50:
            texto = pytesseract.image_to_string(img_preprocesada, lang='spa', config='--psm 6').lower()

        if re.search(r'-\s?\$', texto): return None

        es_brubank = 'brubank' in texto
        es_lemon   = 'lemon' in texto or 'digifin' in texto

        # ── DETECCIÓN BANCO SIN NOMBRE (formato "Detalle" con Débito/Crédito) ─
        # Capturas del tipo: pantalla "Detalle" con campos Movimiento, Fecha, Débito, Crédito.
        # No tienen logo ni nombre de banco. Se identifican por la combinación de:
        #   - "movimiento" como etiqueta
        #   - "débito" O "crédito" (con UNA alcanza -- una transferencia RECIBIDA
        #     solo muestra "Crédito" en la pantalla, nunca aparecen las dos juntas.
        #     Antes se exigían las dos, lo que hacía que este formato nunca se
        #     reconociera y todos los comprobantes cayeran en un ID genérico
        #     que terminaba siendo la palabra "TRANSFERENCIA" repetida siempre.)
        # El monto a usar es el que NO es $0,00 (ya sea crédito o débito).
        es_detalle_coelsa = (
            'débito' in texto or 'debito' in texto or 'crédito' in texto or 'credito' in texto
        ) and 'movimiento' in texto
        cuit = "0"
        nombre_razon_social = "No detectado"
        fecha_servicio = datetime.now().strftime('%d/%m/%Y')
        hora_servicio = ""          # Solo para Brubank, no va al Excel
        importe_encontrado = 0.0
        monto_total_texto = "" 
        nro_movimiento = "Desconocido" 
        
        # --- INICIO DE EXTRACCIÓN DE ID INTELIGENTE Y MULTI-BANCO ---
        # Capturas de banco sin nombre (Detalle): el ID se genera más abajo
        # con datos propios (fecha+monto+nombre+cuit). Se omite la detección general.
        if not es_detalle_coelsa:
            # 1. Identificar y ocultar CBUs (22 dígitos numéricos) para que no se confundan con el ID
            # Esta es la clave para evitar que el script tome un CBU como ID.
            cbu_pattern = r'\b\d{22}\b'
            cbus_encontrados = re.findall(cbu_pattern, texto)

            # Crear una versión del texto sin CBUs para la búsqueda general
            texto_sin_cbus = texto
            for cbu in cbus_encontrados:
                texto_sin_cbus = texto_sin_cbus.replace(cbu, "[CBU_OCULTADO]")

            # Prioridad especial: Lemon "COELSA ID WY7ZEPN6MVQL8JR42Q0M51"
            lemon_coelsa_pattern = r'coelsa\s+id[\s\n:]*([a-z0-9]{10,40})'
            match_lemon_coelsa = re.search(lemon_coelsa_pattern, texto_sin_cbus)

            # Prioridad especial: Lemon "ID de la transacción ded0d789-d315-..."
            lemon_txid_pattern = r'id\s+de\s+la\s+transacci[oó]n[\s\n:]*([a-z0-9\-]{10,40})'
            match_lemon_txid = re.search(lemon_txid_pattern, texto_sin_cbus)

            # 2. Prioridad 1: Buscar COELSA ID (Personal Pay/Naranja X: "CoelsaID...") o ID COELSA (Prex: "ID COELSA\nORD...")
            # El ID de COELSA es una cadena alfanumérica de 10-40 caracteres.
            coelsa_id_pattern = r'(?:coelsa[\s\n:\-]*id|id[\s\n:\-]*coelsa)[\s\n:\-]*([a-z0-9]{10,40})'
            match_coelsa = re.search(coelsa_id_pattern, texto_sin_cbus)

            # Prioridad especial: BNA+ "Número de transacción XXXXXXXX"
            bna_id_pattern = r'n[uú]mero\s+de\s+transacci[oó]n[\s\n:]*(\d{6,20})'
            match_bna = re.search(bna_id_pattern, texto_sin_cbus)

            # Prioridad especial: Mercado Pago "Número de operación de Mercado Pago XXXXXXXXXX"
            mp_id_pattern = r'n[uú]mero\s+de\s+operaci[oó]n\s+de\s+mercado\s+pago\s*\n?\s*(\d{8,20})'
            match_mp = re.search(mp_id_pattern, texto_sin_cbus)

            # Prioridad especial: Ualá "id Op. JMQKYZ9QQ75KPGRV9V50P3"
            # Se acepta salto de línea y 'ld' (OCR confunde 'I' mayúscula con 'l' minúscula)
            uala_id_pattern = r'[il]d\.?\s*op\.?\s*[\s\n]*([a-z0-9]{10,40})'
            match_uala = re.search(uala_id_pattern, texto_sin_cbus)

            # Prioridad especial: Santander "Nº comprobante  71544747"
            # Se busca número puro de 5-12 dígitos tras la etiqueta
            # Patrón ampliado: cubre nº/n°/no/n/n2/nro/num/n°. seguido de 'comprobante'
            santander_id_pattern = r'n[\xba\xb0o2\?]?\.?\s*(?:ro\.?\s*|um\.?\s*)?(?:de\s+)?comprobante(?!\s+de\s)[\s\n:\-]*(\d{5,12})'
            match_santander = re.search(santander_id_pattern, texto_sin_cbus)

            # Fallback Santander: si hay 'importe debitado' (firma del Santander) y el patrón
            # no capturó nada, tomar el ÚLTIMO número de 6-10 dígitos del texto
            # (el Nº comprobante siempre es el último campo numérico del comprobante)
            match_santander_num = None
            if not match_santander and 'importe debitado' in texto_sin_cbus:
                todos_nums = re.findall(r'\b(\d{6,10})\b', texto_sin_cbus)
                # Filtrar años (19xx/20xx) y dejar solo el último candidato
                candidatos = [n for n in todos_nums if not re.match(r'^(19|20)\d{2}$', n)]
                if candidatos:
                    match_santander_num = candidatos[-1]

            if match_lemon_coelsa:
                nro_movimiento = match_lemon_coelsa.group(1).upper()
            elif match_lemon_txid:
                nro_movimiento = match_lemon_txid.group(1).upper()
            elif match_bna:
                nro_movimiento = match_bna.group(1).upper()
            elif match_mp:
                nro_movimiento = match_mp.group(1).upper()
            elif match_santander:
                nro_movimiento = match_santander.group(1)
            elif match_santander_num:
                nro_movimiento = match_santander_num
            elif match_uala:
                nro_movimiento = match_uala.group(1).upper()
            elif match_coelsa:
                nro_movimiento = match_coelsa.group(1).upper()
            else:
                # Prioridad 2: Buscar palabras clave generales para el ID
                # (Mercado Pago, Brubank, Naranja X (código), etc.)
                general_id_keywords = ['movimiento', 'operaci[óo]n', 'c[óo]digo de transacci[óo]n', 'id de transacci[óo]n']
                # Algunos IDs tienen guiones (como Naranja X en image_0.png) o puntos.
                keywords_pattern = r'(?:' + '|'.join(general_id_keywords) + r')[\s\n:\-]*([a-z0-9\-\.]{10,40})'
                match_general = re.search(keywords_pattern, texto_sin_cbus)

                # Banco Hipotecario y similares: "Operación: 00197462" (número corto, solo dígitos)
                # Se busca ANTES del Plan B para tener prioridad sobre el fallback alfanumérico
                match_hip = re.search(r'operaci[oó]n[:\s\n]+(\d{5,20})', texto_sin_cbus)

                if match_general:
                    # Limpiar el ID si tiene guiones o puntos al principio o final
                    nro_movimiento = match_general.group(1).upper().strip('- .')
                elif match_hip:
                    nro_movimiento = match_hip.group(1)
                else:
                    # Plan B Final: Buscar cualquier cadena alfanumérica de longitud razonable (10-40 caracteres)
                    # que no sea un CBU y que no sea trivial.
                    matches_alfanumericos = re.findall(r'\b([a-z0-9]{10,40})\b', texto_sin_cbus)

                    # Palabras comunes que el OCR puede leer y confundir con un ID real
                    PALABRAS_FALSAS = {
                        "COMPROBANTE", "TRANSFERENCIA", "TRANSACCION", "DESTINATARIO",
                        "CONSUMIDOR", "DESCRIPCION", "OPERACION", "MOVIMIENTO",
                        "CONTRASENA", "CONFIRMACION", "COMPROBANTE", "MERCADOPAGO",
                        "DESCONOCIDO", "INGRESANDO", "INMEDIATO", "PERSONAS",
                        "BANCARIAS", "CONSULTAR", "MODULO", "TRANSFERENCIAS",
                        "TRANSFERENCIA", "COMPROBANTES", "ACREDITACION",
                        "TRANSFERISTE", "TRANSFERIR", "TRANSFIRIENDO", "TRANSFIRIO",
                    }
                    # Filtrar para asegurarse de que no es un CBU ocultado, palabra falsa, o algo muy corto/largo.
                    # IMPORTANTE: también exigimos que tenga al menos un dígito -- ningún ID real de
                    # los que reconoce el sistema (Coelsa, Ualá, Brubank GUID, etc.) es puro texto sin
                    # números, así que una cadena solo de letras es casi siempre una palabra del título
                    # o del cuerpo de la pantalla (como "TRANSFERISTE") que el OCR devolvió pegada sin
                    # espacios, no un número de operación real -- confiar en ella causaba que CUALQUIER
                    # captura con esa misma palabra (sin importar monto ni fecha) se marcara como
                    # duplicado de cualquier otra.
                    candidatos_validos = [
                        m.upper() for m in matches_alfanumericos
                        if m != "[CBU_OCULTADO]" and 10 <= len(m) <= 40
                        and m.upper() not in PALABRAS_FALSAS
                        and re.search(r'\d', m)
                    ]

                    if candidatos_validos:
                        # Tomamos el primero que sea lo suficientemente largo
                        # (el ID de Mercado Pago es 17, el COELSA es 22, Brubank GUID 36)
                        for candidato in candidatos_validos:
                            if len(candidato) >= 15:
                                nro_movimiento = candidato
                                break
                        else:
                            nro_movimiento = candidatos_validos[0]
                    # Si no hay candidatos válidos, nro_movimiento permanece como "Desconocido"

            # --- FIN DE EXTRACCIÓN DE ID INTELIGENTE Y MULTI-BANCO ---

        # --- BÚSQUEDA DE NOMBRE (RECEPTOR) — POR NIVELES DE CONFIANZA ---
        # IMPORTANTE: en casi todas estas apps los datos del EMISOR
        # ("De"/"Origen"/"Titular") aparecen ANTES que los del RECEPTOR
        # ("Para"/"Destino"/"Destinatario") en el texto. La versión anterior
        # recorría las líneas una sola vez y cortaba (break) apenas
        # encontraba CUALQUIER CUIT o palabra ambigua como "titular", lo que
        # en la práctica agarraba casi siempre al emisor en vez del receptor.
        # Ahora se buscan primero, en TODO el documento, las etiquetas que
        # sólo puede tener el receptor. Sólo si eso falla se recurre a
        # etiquetas ambiguas (evitando las que caen dentro de un bloque
        # "Origen") y, como último recurso, al primer CUIT del documento.
        lineas = [l.strip() for l in texto.split('\n') if len(l.strip()) > 2]
        idx_nombre_destino = None  # línea donde se encontró el nombre del receptor

        # ── BANCO SIN NOMBRE (Detalle): extraer nombre del campo "Movimiento" ─
        # El campo "Movimiento" tiene el formato:
        #   "CREDITO TRANSFERENCIA COELSA\nNOMBRE APELLIDO\nCUIT"
        #   "TRANSFERENCIA DE TERCEROS\nNOMBRE APELLIDO\nCUIT\n..."
        # Se toma la línea inmediatamente después de la etiqueta de tipo de movimiento.
        if es_detalle_coelsa and nombre_razon_social == "No detectado":
            m_mov = re.search(
                r'(?:cr[eé]dito\s+transferencia\s+coelsa|transferencia\s+de\s+terceros)\s*\n\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]{3,50})\n',
                texto.upper()
            )
            if m_mov:
                candidato = m_mov.group(1).strip()
                # Excluir si el candidato es un número (CUIT suelto)
                if not re.match(r'^\d+$', candidato):
                    nombre_razon_social = limpiar_nombre(candidato)
                    print(f"  👤 [Detalle-SinBanco] Nombre extraído de Movimiento: {nombre_razon_social}")

        # ── NIVEL 1: etiquetas EXCLUSIVAS del receptor (máxima confianza) ──────
        # "Para" (Mercado Pago, Macro, Galicia), "Destinatario" (BNA+, Ualá),
        # "Cuenta destino" (Naranja X), "Transferido a" (Brubank app claro),
        # "Envío de dinero (a)" (encabezado de la tarjeta de Brubank app).
        if nombre_razon_social == "No detectado":
            for i, linea in enumerate(lineas):
                linea_solo_letras = re.sub(r'[^a-záéíóúüñ]', '', linea.lower())
                es_para_exacto = linea_solo_letras == 'para'
                es_envio_header = linea_solo_letras in ('enviodedinero', 'enviodedineroa')
                tiene_marca_fuerte = any(p in linea for p in ['destinatario', 'cuenta destino', 'transferido a'])
                # Evita falsos positivos como "CUIT destinatario 27..." (eso es un
                # campo de CUIT, no la etiqueta de nombre del receptor).
                if tiene_marca_fuerte and re.search(r'\bcuit\b|\bcuil\b', linea):
                    tiene_marca_fuerte = False

                if not (es_para_exacto or es_envio_header or tiene_marca_fuerte):
                    continue

                if tiene_marca_fuerte:
                    partes = re.split(r'[:\-]', linea)
                    if len(partes) > 1:
                        candidato = partes[-1].strip()
                    else:
                        candidato = re.sub(
                            r'^(destinatario|cuenta destino|transferido a)\s*',
                            '', linea, flags=re.IGNORECASE
                        ).strip()
                    j_extra = i + 1
                else:
                    # "Para" o "Envío de dinero (a)": el nombre va en la línea siguiente
                    candidato = lineas[i + 1] if i + 1 < len(lineas) else ""
                    j_extra = i + 2

                nombre_limpio = limpiar_nombre(candidato)
                ETIQUETAS_NO_NOMBRE = ['CUIT', 'TOTAL', 'FECHA', 'IVA', 'CBU', 'CUIL', 'ALIAS', 'CVU', 'MONTO', 'ENTIDAD', 'DESTINO', 'ORIGEN']
                # Puede requerir más de una línea extra: "Envío de dinero a" en Brubank
                # suele cortar el nombre en 2-3 líneas (ej. "Celeste Maria" / "Giselle Rodriguez").
                intentos_merge = 2 if es_envio_header else 1
                while (len(nombre_limpio.split()) < 2 or (es_envio_header and len(nombre_limpio.split()) < 4)) \
                        and intentos_merge > 0 and j_extra < len(lineas):
                    siguiente_linea = limpiar_nombre(lineas[j_extra])
                    es_valida = (
                        len(siguiente_linea) > 2
                        and siguiente_linea not in ETIQUETAS_NO_NOMBRE
                        and not re.search(r'\d', lineas[j_extra])
                    )
                    if not es_valida:
                        break
                    nombre_limpio = f"{nombre_limpio} {siguiente_linea}".strip()
                    j_extra += 1
                    intentos_merge -= 1

                if len(nombre_limpio) > 2:
                    nombre_razon_social = nombre_limpio
                    idx_nombre_destino = i
                    print(f"  👤 [Nivel1-Destino] Nombre extraído ('{linea[:25]}'): {nombre_razon_social}")
                    break

        # ── NIVEL 2: etiquetas ambiguas, salvo que caigan en un bloque "Origen" ─
        # "Titular", "Nombre", "Apellido", etc. pueden referirse al emisor
        # (ej. Brubank: "Origen / Titular NOMBRE DEL QUE ENVÍA"), así que se
        # descartan mientras estemos dentro de un bloque marcado como origen.
        if nombre_razon_social == "No detectado":
            dentro_de_origen = False
            for i, linea in enumerate(lineas):
                linea_solo_letras = re.sub(r'[^a-záéíóúüñ]', '', linea.lower())
                if linea_solo_letras == 'origen' or any(p in linea for p in ['cuenta origen', 'remitente', 'titular origen']):
                    dentro_de_origen = True
                    continue
                if linea_solo_letras == 'destino' or any(p in linea for p in ['cuenta destino', 'destinatario']):
                    dentro_de_origen = False
                    continue
                if dentro_de_origen:
                    continue
                if any(p in linea for p in ['receptor', 'cliente', 'nombre', 'razon', 'titular', 'apellido']):
                    partes = re.split(r'[:\-]', linea)
                    if len(partes) > 1:
                        candidato = partes[-1].strip()
                    else:
                        # Lemon y otros: "nombre celeste maria giselle rodriguez" (solo espacios)
                        candidato = re.sub(
                            r'^(nombre y apellido|razon social|receptor|apellido|cliente|titular|nombre)\s+',
                            '', linea, flags=re.IGNORECASE
                        ).strip()
                    nombre_limpio = limpiar_nombre(candidato)
                    if len(nombre_limpio.split()) < 2 and i + 1 < len(lineas):
                        siguiente_linea = limpiar_nombre(lineas[i + 1])
                        if len(siguiente_linea) > 2 and siguiente_linea not in ['CUIT', 'TOTAL', 'FECHA', 'IVA', 'DESTINATARIO', 'RECEPTOR', 'CBU', 'CUIL']:
                            nombre_limpio = f"{nombre_limpio} {siguiente_linea}"
                    if len(nombre_limpio) > 2:
                        nombre_razon_social = nombre_limpio
                        idx_nombre_destino = i
                        print(f"  👤 [Nivel2-Ambiguo] Nombre extraído fuera de bloque Origen ('{linea[:25]}'): {nombre_razon_social}")
                        break

        # ── BUSQUEDA DE CUIT ─────────────────────────────────────────────────
        # OJO: el patrón tiene un grupo de captura, así que se usa finditer
        # (no findall) para quedarse con el match COMPLETO de cada CUIT
        # encontrado, no solo el prefijo de 2 dígitos.
        patron_cuit = r'\b(20|23|24|27|30|33|34)-?\d{8}-?\d{1}\b'
        matches_cuit = list(re.finditer(patron_cuit, texto))
        cuit_match = matches_cuit[0] if matches_cuit else None
        cuits_limpios_img = [m.group().replace("-", "").replace(".", "").strip() for m in matches_cuit]

        # ── BANCO SIN NOMBRE: el CUIT está en la línea de "Movimiento" ─────────
        # Formato: "NOMBRE APELLIDO\n20XXXXXXXXX" (11 dígitos sin guiones)
        if es_detalle_coelsa and not cuit_match:
            m_cuit_mov = re.search(
                r'(?:cr[eé]dito\s+transferencia\s+coelsa|transferencia\s+de\s+terceros)[\s\S]{5,80}?\n\s*(\d{11})\b',
                texto
            )
            if m_cuit_mov:
                cuit = m_cuit_mov.group(1)
                print(f"  🔑 [Detalle-SinBanco] CUIT en Movimiento: {cuit}")

        # NIVEL 1 de CUIT: buscarlo cerca de donde se encontró el nombre del
        # receptor — evita repetir el bug de tomar el primer CUIT del
        # documento, que casi siempre es el del emisor.
        if cuit == "0" and idx_nombre_destino is not None:
            fin = min(idx_nombre_destino + 9, len(lineas))
            for k in range(idx_nombre_destino, fin):
                m_local = re.search(patron_cuit, lineas[k])
                if m_local:
                    candidato_cuit = m_local.group().replace("-", "").replace(".", "").strip()
                    if candidato_cuit != cuit_propio_cliente:
                        cuit = candidato_cuit
                        print(f"  🔑 [CUIT-cercano-al-nombre] {cuit}")
                        break

        # Último recurso: primer CUIT del documento (comportamiento anterior,
        # sólo se usa si no se pudo ubicar el nombre del receptor o el CUIT
        # cercano a él).
        if cuit == "0" and cuit_match:
            cuit = str(cuit_match.group().replace("-", "").replace(".", "").strip())

        # Otro CUIT/CUIL encontrado en la imagen (probablemente el emisor, ya
        # que el primero del documento casi siempre lo es -- ver comentario
        # arriba), para ofrecer como alternativa en Revisión Manual, por si
        # el lector se equivocó de cuál de los dos es el receptor.
        cuit_alternativo = "0"
        for c in cuits_limpios_img:
            if c != cuit and c != cuit_propio_cliente:
                cuit_alternativo = c
                break

        # --- BÚSQUEDA DE NOMBRE DE QUIEN TRANSFIERE (remitente) ---
        # Se reconocen las etiquetas más habituales para identificar al emisor
        # del dinero: "Remitente", "Origen", "Cuenta origen", "Titular origen".
        # NOTA: por ahora NO se usa la palabra "de" sola como disparador porque
        # es una preposición demasiado común en español (aparece en "fecha de
        # emisión", "número de comprobante", etc.) y generaría falsos positivos.
        # Si en algún comprobante tuyo el remitente aparece bajo otra etiqueta
        # (por ejemplo en Mercado Pago, Naranja X, BNA, Macro, Lemon, Brubank o
        # Galicia), decime exactamente qué palabra la precede y la agrego.
        nombre_remitente = "No detectado"
        for i, linea in enumerate(lineas):
            if any(p in linea for p in ['remitente', 'titular origen', 'cuenta origen', 'origen']):
                partes = re.split(r'[:\-]', linea)
                if len(partes) > 1:
                    candidato = partes[-1].strip()
                else:
                    candidato = re.sub(
                        r'^(remitente|titular origen|cuenta origen|origen)\s+',
                        '', linea, flags=re.IGNORECASE
                    ).strip()
                nombre_limpio_r = limpiar_nombre(candidato)
                if len(nombre_limpio_r.split()) < 2 and i + 1 < len(lineas):
                    siguiente_linea_r = limpiar_nombre(lineas[i + 1])
                    if len(siguiente_linea_r) > 2 and siguiente_linea_r not in ['CUIT', 'TOTAL', 'FECHA', 'IVA', 'CBU', 'CUIL']:
                        nombre_limpio_r = f"{nombre_limpio_r} {siguiente_linea_r}"
                if len(nombre_limpio_r) > 2:
                    nombre_remitente = nombre_limpio_r
                break

        # --- BUSQUEDA DE FECHA (con soporte especial Brubank fecha+hora) ---
        # Brubank: "19 de abril de 2026 - 22:59"
        if es_brubank:
            for mes_nombre, mes_num in MESES.items():
                m_bru = re.search(
                    rf'(\d{{1,2}})\s+de\s+{mes_nombre}\s+de\s+(\d{{4}})(?:\s*[-–]\s*(\d{{1,2}}:\d{{2}}))?',
                    texto
                )
                if m_bru:
                    dia  = m_bru.group(1).zfill(2)
                    anio = m_bru.group(2)
                    hora_servicio = m_bru.group(3) if m_bru.group(3) else ""
                    fecha_servicio = f"{dia}/{mes_num}/{anio}"
                    break

        match_fecha_nx = re.search(r'\b(\d{1,2})\s*/\s*([a-z]{3})\s*/\s*(\d{4})\b', texto)
        if match_fecha_nx and match_fecha_nx.group(2) in MESES_ABREV:
            dia = match_fecha_nx.group(1).zfill(2)
            mes = MESES_ABREV[match_fecha_nx.group(2)]
            anio = match_fecha_nx.group(3)
            fecha_servicio = f"{dia}/{mes}/{anio}"
        else:
            # Fecha con año 4 dígitos: DD/MM/YYYY o DD-MM-YYYY
            match_fecha_bna = re.search(r'\b(\d{1,2})\s*[-\/|]\s*(\d{1,2})\s*[-\/|]\s*(\d{4})\b', texto)
            # Fecha con año 2 dígitos: DD/MM/YY (ej: 10/06/26 → 10/06/2026)
            match_fecha_yy  = re.search(r'\b(\d{1,2})[/](\d{1,2})[/](\d{2})\b', texto)

            if match_fecha_bna:
                dia = match_fecha_bna.group(1).zfill(2)
                mes = match_fecha_bna.group(2).zfill(2)
                anio = match_fecha_bna.group(3)
                fecha_servicio = f"{dia}/{mes}/{anio}"
            elif match_fecha_yy:
                dia  = match_fecha_yy.group(1).zfill(2)
                mes  = match_fecha_yy.group(2).zfill(2)
                anio = "20" + match_fecha_yy.group(3)
                fecha_servicio = f"{dia}/{mes}/{anio}"
                print(f"  📅 [Fecha-YY] Detectada fecha con año corto: {fecha_servicio}")
            else:
                for mes_nombre, mes_num in MESES.items():
                    if mes_nombre in texto:
                        match_fecha = re.search(rf'(\d{{1,2}})\s+de\s+{mes_nombre}', texto)
                        if match_fecha:
                            dia = match_fecha.group(1).zfill(2)
                            fecha_servicio = f"{dia}/{mes_num}/2026" 
                            break
        
        # --- BUSQUEDA DE MONTO Y CENTAVOS (MEJORADA) ---
        importe_encontrado = 0.0
        monto_total_texto = ""

        # ── PATRÓN BANCO SIN NOMBRE (Detalle: Débito / Crédito) ─────────────
        # Formato: etiqueta "Débito" seguida de "$0,00", luego etiqueta "Crédito"
        # seguida del monto real (o viceversa). Se toma el valor que no sea cero.
        # Ejemplos OCR:
        #   "débito\n$0,00\ncrédito\n$ 17.700,00"
        #   "crédito\n$0,00\ndébito\n$ 3.400,00"
        if es_detalle_coelsa and importe_encontrado == 0.0:
            # Buscar el bloque débito+crédito y quedarnos con el no-cero
            # Patrón: (débito|crédito) seguido de $ monto (en la misma línea o la siguiente)
            bloques = re.findall(
                r'(d[eé]bito|cr[eé]dito)\s*\n?\s*\$\s*([\d\.]+)[,\.](\d{2})',
                texto
            )
            for etiqueta, enteros, centavos in bloques:
                valor = _parsear_monto_argentino(enteros, centavos)
                if valor > 0.0:
                    importe_encontrado = valor
                    monto_total_texto = f"{importe_encontrado:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
                    print(f"  💰 [Detalle-SinBanco] Monto ({etiqueta}): {monto_total_texto}")
                    break

        # ── PATRÓN BRUBANK ────────────────────────────────────────────────────
        # Brubank muestra: "Importe:  28500.00" (sin $, sin separador de miles)
        # El OCR con --psm 11 puede fragmentar "28500.00" como "2.800 00" o "2 8500 00",
        # por eso capturamos TODA la línea después de "importe:" y reconstruimos los dígitos.
        if es_brubank and importe_encontrado == 0.0:
            # Buscar la línea completa que contiene "importe"
            m_bru_linea = re.search(r'importe\s*[:\-]?\s*(.+)', texto)
            if m_bru_linea:
                linea_imp = m_bru_linea.group(1).strip()
                # Extraer todos los grupos de dígitos de esa línea
                grupos = re.findall(r'\d+', linea_imp)
                if grupos:
                    # Caso: [..., "XX"] donde los últimos 2 dígitos son centavos
                    # pero solo si hay más de un grupo (ej: ["28500", "00"] o ["2", "800", "00"])
                    if len(grupos) >= 2 and len(grupos[-1]) == 2:
                        parte_entera = ''.join(grupos[:-1])
                        centavos = grupos[-1]
                    else:
                        # Un solo grupo o el último grupo no tiene 2 dígitos
                        # Ej: ["2850000"] o ["28500"]
                        todos = ''.join(grupos)
                        # Si termina en 2 dígitos y tiene más de 4, separar centavos
                        if len(todos) > 4:
                            parte_entera = todos[:-2]
                            centavos = todos[-2:]
                        else:
                            parte_entera = todos
                            centavos = '00'
                    try:
                        importe_encontrado = float(f"{parte_entera}.{centavos}")
                        monto_total_texto = f"{importe_encontrado:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
                        print(f"  💰 [Brubank] Importe detectado: {monto_total_texto} (OCR línea: '{linea_imp}')")
                    except ValueError:
                        pass

        # ── PATRÓN LEMON ─────────────────────────────────────────────────────
        # El comprobante muestra: "ARS 11,000.00"
        # Formato anglosajón: coma = separador de miles, punto = decimal.
        # El OCR puede leer: "ARS 11,000.00" o "ars 11.000,00" (invertido por locale).
        # Capturamos ambas variantes.
        if es_lemon and importe_encontrado == 0.0:
            # Variante 1: "ARS 11,000.00"  (coma=miles, punto=decimal — formato original Lemon)
            # Requiere al menos una coma seguida de 3 dígitos para no confundirse con var 2
            m_lem = re.search(r'ars\s+(\d{1,3}(?:,\d{3})+(?:\.\d{2})?)', texto)
            if m_lem:
                raw = m_lem.group(1)            # ej: "11,000.00"
                importe_encontrado = float(raw.replace(',', ''))
            if importe_encontrado == 0.0:
                # Variante 2: "ARS 11.000,00"  (punto=miles, coma=decimal — OCR con locale ES)
                m_lem2 = re.search(r'ars\s+(\d{1,3}(?:\.\d{3})+(?:,\d{2})?)', texto)
                if m_lem2:
                    raw = m_lem2.group(1)       # ej: "11.000,00"
                    partes = raw.rsplit(',', 1)
                    parte_entera = partes[0].replace('.', '')
                    decimales = partes[1] if len(partes) == 2 else '00'
                    importe_encontrado = float(f"{parte_entera}.{decimales}")
            if importe_encontrado == 0.0:
                # Variante 3: sin separador de miles  "ARS 11000" o "ARS 11000.00"
                m_lem3 = re.search(r'ars\s+(\d+)(?:[.,](\d{2}))?', texto)
                if m_lem3:
                    entero = m_lem3.group(1)
                    decs   = m_lem3.group(2) or '00'
                    importe_encontrado = float(f"{entero}.{decs}")
            if importe_encontrado > 0:
                monto_total_texto = f"{importe_encontrado:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

        # PRIORIDAD MÁXIMA: "Monto:" o "Importe:" con el $ en la línea siguiente (Banco Macro, etc.)
        # Esto evita que se tome un número de cuenta como "CA $ 9573" antes que el monto real.
        patrones_monto = [
            # Mercado Pago: monto grande en línea propia "$ 4.800" justo después de "comprobante de transferencia"
            # o después de "$ " como monto principal (con punto como separador de miles, sin decimales)
            r'comprobante\s+de\s+transferencia[^\n]*\n[^\n]*\n\s*\$\s*([\d\.]+)\s*\n',
            r'(?:monto|importe)\s*:\s*\$?\s*([\d.,]+)(?:\s+([0-9]{2}|oo|00)(?!\d))?',  # Monto: $ 12.500 (misma línea)
            r'(?:monto|importe)[^\n]*\n\s*\$?\s*([\d.,]+)(?:\s+([0-9]{2}|oo|00)(?!\d))?',  # Monto:\n$ 12.500 (línea siguiente)
            r'(?:enviaste|transferiste)\s*(?:\$|s|5)?\s*([\d.,]+)(?:\s+([0-9]{2}|oo|00)(?!\d))?',
            r'\$\s*([\d.,]+)(?:\s+([0-9]{2}|oo|00)(?!\d))?'
        ]
        
        for patron in patrones_monto:
            if importe_encontrado > 0.0:
                break   # ya tenemos el monto (ej: Lemon), no pisar
            monto_match = re.search(patron, texto)
            if monto_match:
                enteros = monto_match.group(1).strip()
                centavos = monto_match.group(2) if monto_match.lastindex and monto_match.lastindex >= 2 else None
                
                # Limpiar comas o puntos finales sueltos por errores de lectura
                enteros = enteros.strip('.,')
                
                # Corregir error común de OCR en Naranja X (leer los ceros chicos como letras 'o')
                if centavos and centavos.lower() == 'oo':
                    centavos = '00'
                    
                if centavos:
                    raw_monto = f"{enteros},{centavos}"
                else:
                    raw_monto = enteros
                
                # Lógica robusta de conversión para asegurar que float() no falle nunca
                if ',' in raw_monto:
                    partes = raw_monto.rsplit(',', 1)
                    parte_entera = partes[0].replace('.', '').replace(',', '')
                    valor_final = f"{parte_entera}.{partes[1]}"
                elif '.' in raw_monto:
                    partes = raw_monto.rsplit('.', 1)
                    # CLAVE: si después del punto hay exactamente 3 dígitos → es separador de MILES (formato argentino)
                    # Ejemplos: "4.800" → 4800, "14.000" → 14000, "1.500.000" → 1500000
                    # Si hay exactamente 2 dígitos → es decimal (ej: "14000.00")
                    if len(partes[1]) == 2:  # Punto como decimal (ej: 14000.00)
                        parte_entera = partes[0].replace('.', '')
                        valor_final = f"{parte_entera}.{partes[1]}"
                    else:  # Punto como miles (ej: 4.800, 14.500, 1.500.000)
                        parte_entera = raw_monto.replace('.', '')
                        valor_final = f"{parte_entera}.00"
                else:
                    valor_final = f"{raw_monto}.00"

                try:
                    importe_encontrado = float(valor_final)
                    # Formatear el texto prolijo para tu Excel final (Ej: 14.000,00)
                    monto_total_texto = f"{importe_encontrado:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
                    break # Si todo salió bien, salimos del bucle
                except ValueError:
                    continue # Si falló la conversión, probamos el siguiente patrón de la lista

        # Si no se detectó CUIT válido, Tipo = DNI y número en blanco
        cuit_valido = cuit if cuit != "0" else ""
        cuit_alternativo_valido = cuit_alternativo if cuit_alternativo != "0" else ""

        # --- DETECCIÓN DE CUIT PROPIO (REMITENTE) ---
        # Si el CUIT encontrado en la imagen es el del cliente activo,
        # significa que es el que ENVÍA, no el destinatario -- se usa el
        # otro CUIT/CUIL que se haya encontrado en su lugar (si lo hay); si
        # no hay otro, queda vacío y se trata como DNI sin número.
        if cuit_valido and cuit_propio_cliente and cuit_valido == cuit_propio_cliente:
            print(f"  ⚠️  CUIT {cuit_valido} es el del remitente (cliente activo) → se reemplaza por el otro detectado")
            cuit_valido = cuit_alternativo_valido if cuit_alternativo_valido != cuit_propio_cliente else ""
            cuit_alternativo_valido = ""
        if cuit_alternativo_valido and cuit_propio_cliente and cuit_alternativo_valido == cuit_propio_cliente:
            cuit_alternativo_valido = ""

        tipo_doc = "CUIT" if cuit_valido else "DNI"

        # --- BRUBANK: generar ID único sintético si no hay código propio ---
        # Brubank no emite número de operación visible, así que construimos uno
        # combinando datos extraídos para garantizar unicidad y trazabilidad.
        #
        # Formato: BRU-<2 dígitos monto>-<DDMMMM><AA>-<HHMM>-<3 chars archivo>
        # Ejemplo con monto 5000, fecha 19/04/2026, hora 22:59, archivo "WhatsApp...":
        #   BRU-50-190426-2259-WHA
        #
        if es_brubank and nro_movimiento == "Desconocido":
            try:
                # Parte monto: primeros 2 dígitos del importe entero
                monto_str = str(int(importe_encontrado)) if importe_encontrado > 0 else "00"
                parte_monto = monto_str[:2].zfill(2)

                # Parte fecha: DDMMAA desde fecha_servicio ("19/04/2026" → "190426")
                if fecha_servicio and "/" in fecha_servicio:
                    f_partes = fecha_servicio.split("/")
                    parte_fecha = f_partes[0] + f_partes[1] + f_partes[2][2:]  # DDMMAA
                else:
                    parte_fecha = datetime.now().strftime("%d%m%y")

                # Parte hora: HHMM sin los dos puntos ("22:59" → "2259")
                parte_hora = hora_servicio.replace(":", "") if hora_servicio else datetime.now().strftime("%H%M")

                # Parte archivo: primeros 3 chars del nombre sin extensión, solo alfanuméricos
                nombre_base = os.path.splitext(os.path.basename(ruta_imagen))[0]
                parte_archivo = re.sub(r'[^A-Z0-9]', '', nombre_base.upper())[:3].ljust(3, 'X')

                nro_movimiento = f"BRU-{parte_monto}-{parte_fecha}-{parte_hora}-{parte_archivo}"
                print(f"  🔑 [Brubank] ID sintético generado: {nro_movimiento}")
            except Exception as e_id:
                nro_movimiento = f"BRU-{datetime.now().strftime('%d%m%y%H%M%S')}"
                print(f"  🔑 [Brubank] ID fallback: {nro_movimiento} ({e_id})")

        # --- BANCO SIN NOMBRE (Detalle): generar ID único ──────────────────────
        # PRIORIDAD 1: la mayoría de estos comprobantes SÍ tienen un número de
        # referencia real y único de la operación (un número largo, de 18 a 25
        # dígitos, que aparece después del CUIT del remitente) -- confirmado con
        # ejemplos reales. Hay otros dos números que se repiten IGUAL en todos
        # los comprobantes de este formato (no son de la operación, son fijos
        # del sistema) y hay que ignorarlos para no confundirlos con la referencia.
        # PRIORIDAD 2 (fallback si no aparece ningún número de referencia): combinar
        # fecha + monto + nombre COMPLETO + CUIT COMPLETO del remitente. Antes se
        # usaban solo 3 letras del nombre y 4 dígitos del CUIT, lo que hacía que
        # dos transferencias DISTINTAS (mismo día, mismo monto, nombres que
        # empiezan igual) terminaran con el mismo ID y se marcaran como duplicadas
        # entre sí sin serlo.
        NUMEROS_FIJOS_DEL_SISTEMA = {"200004350", "589244014300000001"}
        # El OCR a veces lee mal el PRIMER dígito de estos números fijos (ej: lee "9"
        # en vez de "5") -- comparando por los últimos 17 caracteres en vez del número
        # exacto completo, se los sigue reconociendo como fijos y no como referencia real.
        SUFIJOS_FIJOS_DEL_SISTEMA = {c[-17:] for c in NUMEROS_FIJOS_DEL_SISTEMA if len(c) >= 17}

        if es_detalle_coelsa and nro_movimiento == "Desconocido":
            try:
                # Buscar un número de referencia real: una tanda de dígitos larga,
                # tolerando que el OCR le meta un espacio o salto de línea en el medio.
                candidatos_crudos = re.findall(r'\d(?:[\d\s]{15,35})\d', texto)
                candidatos_limpios = [re.sub(r'\s', '', c) for c in candidatos_crudos]
                candidatos_utiles = [
                    c for c in candidatos_limpios
                    if 18 <= len(c) <= 25
                    and c not in NUMEROS_FIJOS_DEL_SISTEMA
                    and c[-17:] not in SUFIJOS_FIJOS_DEL_SISTEMA
                ]

                if candidatos_utiles:
                    nro_movimiento = f"DET-REF-{candidatos_utiles[0]}"
                    print(f"  🔑 [Detalle-SinBanco] Referencia real encontrada: {nro_movimiento}")
                else:
                    if fecha_servicio and "/" in fecha_servicio:
                        f_partes = fecha_servicio.split("/")
                        parte_fecha = f_partes[0] + f_partes[1] + f_partes[2][2:]
                    else:
                        parte_fecha = datetime.now().strftime("%d%m%y")

                    parte_monto = str(int(importe_encontrado)) if importe_encontrado > 0 else "0"
                    nombre_completo_id = re.sub(r'[^A-Z]', '', nombre_razon_social.upper())
                    cuit_para_id = cuit_valido if cuit_valido else cuit
                    cuit_completo_id = re.sub(r'\D', '', str(cuit_para_id)) if cuit_para_id and cuit_para_id != "0" else "SINCUIT"

                    nro_movimiento = f"DET-{parte_fecha}-{parte_monto}-{nombre_completo_id}-{cuit_completo_id}"
                    print(f"  🔑 [Detalle-SinBanco] Sin referencia numérica, ID por datos completos: {nro_movimiento}")
            except Exception as e_id:
                nro_movimiento = f"DET-{datetime.now().strftime('%d%m%y%H%M%S%f')}"
                print(f"  🔑 [Detalle-SinBanco] ID fallback: {nro_movimiento} ({e_id})")

        # --- FALLBACK GENERAL: cualquier otro formato sin ID visible ───────────
        # Pantallas de confirmación simples (ej. Mercado Pago "Transferiste $X"
        # sin entrar a "Mostrar comprobante") no muestran ningún número de
        # operación. Hasta acá, todas esas capturas quedaban con
        # id_transaccion = "Desconocido" -- literal, la misma palabra siempre --
        # y como la detección de duplicados compara ese texto tal cual, CUALQUIER
        # transferencia nueva de este tipo (de cualquier monto, cualquier fecha,
        # a cualquier destinatario) chocaba contra la primera que se hubiera
        # guardado alguna vez con esa palabra para esa empresa, marcándose como
        # duplicado sin serlo. Se arma un ID con los mismos datos que ya se
        # extrajeron (fecha + monto + nombre + cuit), igual que el fallback de
        # Detalle-Coelsa de arriba -- dos transferencias distintas casi nunca
        # comparten los cuatro datos a la vez.
        if nro_movimiento == "Desconocido":
            try:
                if fecha_servicio and "/" in fecha_servicio:
                    f_partes = fecha_servicio.split("/")
                    parte_fecha = f_partes[0] + f_partes[1] + f_partes[2][2:]
                else:
                    parte_fecha = datetime.now().strftime("%d%m%y")

                parte_monto = str(int(importe_encontrado)) if importe_encontrado > 0 else "0"
                nombre_completo_id = re.sub(r'[^A-Z]', '', nombre_razon_social.upper())
                cuit_para_id = cuit_valido if cuit_valido else cuit
                cuit_completo_id = re.sub(r'\D', '', str(cuit_para_id)) if cuit_para_id and cuit_para_id != "0" else "SINCUIT"

                nro_movimiento = f"GEN-{parte_fecha}-{parte_monto}-{nombre_completo_id}-{cuit_completo_id}"
                print(f"  🔑 [Fallback general] Sin ID visible, ID por datos completos: {nro_movimiento}")
            except Exception as e_id:
                nro_movimiento = f"GEN-{datetime.now().strftime('%d%m%y%H%M%S%f')}"
                print(f"  🔑 [Fallback general] ID fallback: {nro_movimiento} ({e_id})")

        fecha_emision_final = fecha_servicio  # la fecha real detectada, sin "pisar" con hoy (ver nota en _fecha_emision_efectiva más arriba)
        medio_pago, tipo_pago_detectado, numero_pago_detectado, tipo_pago_detalle_detectado = _detectar_medio_pago(texto)
        condicion_venta_detectada = {"Débito": "Tarjeta de Débito", "Crédito": "Tarjeta de Crédito"}.get(medio_pago)  # None si es Transferencia -> deja que gane la config de la empresa, no se fuerza "Contado"
        return {
            "Tipo Documento": tipo_doc,
            "CUIT Receptor": cuit_valido,
            "CUIT Alternativo": cuit_alternativo_valido,
            "Nombre / Razón Social": nombre_razon_social if len(nombre_razon_social) > 2 else "CONSUMIDOR FINAL",
            "Nombre Remitente": nombre_remitente,  # Quien TRANSFIERE el dinero (si se pudo detectar)
            "Fecha del Comprobante": fecha_emision_final,
            "Condicion IVA": "Consumidor Final",
            "Condicion Venta": condicion_venta_detectada,
            "Medio Pago": medio_pago,
            "Tipo Pago": tipo_pago_detectado,
            "Tipo Pago Detalle": tipo_pago_detalle_detectado,
            "Numero Pago": numero_pago_detectado,
            "Fecha Desde": fecha_servicio,
            "Fecha Hasta": fecha_servicio,
            "Importe Total": importe_encontrado,
            "Monto Texto Completo": monto_total_texto,
            "Archivo Origen": os.path.basename(ruta_imagen),
            "Nro Movimiento": nro_movimiento,
            "ID_Transaccion": nro_movimiento,  # Esta será la nueva columna en el Excel
            "Hora_Servicio": hora_servicio,     # Solo Brubank por ahora; no va al Excel
        }
    except Exception as e:
        return None


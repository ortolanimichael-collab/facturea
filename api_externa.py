"""
API para sistemas externos (por ahora: el sistema de gestión de negocios / tienda online
del mismo ecosistema) que quieren facturar sus ventas con AutoFacturación.

Idea: el sistema externo NO factura por su cuenta. Manda cada venta como un Comprobante
"pendiente" de UNA empresa -- exactamente igual que ya hacen los importadores de Mercado
Pago, Payway, Galicia y NAVE (ver procesador.py) -- y el bot de siempre la factura en ARCA,
ya sea cuando el usuario toca "Facturar todo" acá, o cuando el sistema externo lo pide con
POST /api/externo/facturar.

Autenticación: cada empresa genera un token desde su pantalla de edición ("Conectar con
el sistema de gestión"). Solo se guarda el HASH del token (sha256), nunca el token en sí:
se muestra una única vez al generarlo. El token solo sirve para ESA empresa y solo para
estas rutas -- no da acceso a la cuenta ni a la Clave Fiscal.

Todos los comprobantes que llegan por acá llevan id_transaccion con el prefijo "NEG-",
así:
  - se deduplican solos con el mismo mecanismo que el resto (_estado_inicial_para_transaccion
    + IdTransaccionFacturada: una venta nunca se factura dos veces aunque se reenvíe);
  - "facturar" desde la API solo toca ESOS comprobantes, nunca los que el usuario tiene
    cargados de otros orígenes y quizás todavía no revisó.

Se registra desde app.py con crear_blueprint_api_externa(...), pasándole las funciones de
app.py que necesita (inyección de dependencias, para no importar app.py desde acá y evitar
un import circular).
"""
import hashlib
import hmac
import random
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from models import db, Empresa, Comprobante
from procesador import _estado_inicial_para_transaccion
from automatizacion.arca_bot import concepto_efectivo

PREFIJO = "NEG-"
MAX_POR_ENVIO = 200

CONDICIONES_IVA_RECEPTOR = {
    "Consumidor Final", "Responsable Monotributo", "IVA Responsable Inscripto", "IVA Sujeto Exento",
    "Sujeto No Categorizado", "Monotributista Social", "IVA No Alcanzado",
}


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _numero(valor, default=0.0):
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return default


def crear_comprobante_desde_venta_externa(datos, empresa):
    """
    Arma un Comprobante "pendiente" a partir de una venta del sistema externo. Mismos
    valores por defecto que crear_comprobante_desde_pago_mercadopago() en procesador.py.
    Devuelve (comprobante | None, mensaje_error | None, estado_resultante).
    """
    id_transaccion = str(datos.get("id_externo") or "").strip()
    if not id_transaccion.startswith(PREFIJO) or len(id_transaccion) > 120:
        return None, f"id_externo inválido (tiene que empezar con {PREFIJO})", None

    crear, estado_inicial, facturado_en_previo = _estado_inicial_para_transaccion(id_transaccion, empresa.id)
    if not crear:
        existente = Comprobante.query.filter_by(id_transaccion=id_transaccion, empresa_id=empresa.id).first()
        return existente, None, existente.estado if existente else "pendiente"

    importe_total = round(_numero(datos.get("total")), 2)
    if importe_total <= 0:
        return None, "El total tiene que ser mayor a cero", None

    if estado_inicial == "pendiente" and not empresa.usuario.le_queda_cupo_mensual(1):
        return None, "Se alcanzó el límite mensual de comprobantes del plan de AutoFacturación", None

    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_comprobante = str(datos.get("fecha") or "").strip()
    try:
        datetime.strptime(fecha_comprobante, "%d/%m/%Y")
    except ValueError:
        fecha_comprobante = datetime.now().strftime("%d/%m/%Y")

    # Medio de pago -> lo que espera el bot (mismo formato que el lector y los importadores)
    medio = str(datos.get("medio_pago") or "").lower()
    tipo_pago = tipo_pago_detalle = None
    if medio in ("debito", "credito"):
        medio_pago_detectado = "Débito" if medio == "debito" else "Crédito"
        condicion_venta = "Tarjeta de Débito" if medio == "debito" else "Tarjeta de Crédito"
        tipo_pago = "Otra..."
        tipo_pago_detalle = (str(datos.get("tarjeta") or "").strip().upper()
                             or ("TARJETA DE DÉBITO" if medio == "debito" else "TARJETA DE CRÉDITO"))[:80]
    else:
        medio_pago_detectado = "Transferencia"
        condicion_venta = (empresa.config_condicion_venta or "").split(",")[0]

    if empresa.config_descripcion_aleatoria:
        opciones = [d.strip() for d in (empresa.descripciones_disponibles or "").split(",") if d.strip()]
    else:
        opciones = []
    descripcion = random.choice(opciones) if opciones else empresa.config_producto_servicio

    alicuota = empresa.alicuota_para_descripcion(descripcion)
    if alicuota is None:
        alicuota = (empresa.config_alicuota_iva or "").split(",")[0] or None

    # Receptor: Consumidor Final sin documento salvo que venga un CUIT/CUIL/DNI
    receptor = datos.get("receptor") or {}
    tipo_doc = str(receptor.get("tipo_doc") or "").upper().strip()
    numero_doc = "".join(ch for ch in str(receptor.get("numero") or "") if ch.isdigit())
    if tipo_doc in ("CUIT", "CUIL", "DNI") and numero_doc:
        tipo_documento, cuit_receptor = tipo_doc, numero_doc
    else:
        tipo_documento, cuit_receptor = "DNI", None
    condicion_iva = receptor.get("condicion_iva") if receptor.get("condicion_iva") in CONDICIONES_IVA_RECEPTOR \
        else empresa.config_condicion_iva

    origen = str(datos.get("origen") or "Sistema de gestión")[:60]
    detalle = str(datos.get("detalle") or "")[:180]

    fila = Comprobante(
        usuario_id=empresa.usuario_id,
        empresa_id=empresa.id,
        id_transaccion=id_transaccion,
        punto_venta=empresa.config_punto_venta,
        tipo_comprobante=(empresa.config_tipo_comprobante or "").split(",")[0],
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        descripcion=descripcion,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=importe_total,
        cantidad=1.0,
        importe_total=importe_total,
        alicuota_iva=alicuota if empresa.tipo_contribuyente == "Responsable Inscripto" else None,
        nombre_remitente=(str(receptor.get("nombre") or "").strip()[:200] or None),
        nombre_razon_social=(str(receptor.get("nombre") or "").strip()[:200] or None) if cuit_receptor else None,
        fecha_comprobante=fecha_comprobante,
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        medio_pago_detectado=medio_pago_detectado,
        tipo_pago=tipo_pago,
        tipo_pago_detalle=tipo_pago_detalle,
        tipo_documento=tipo_documento,
        cuit_receptor=cuit_receptor,
        condicion_iva=condicion_iva,
        condicion_venta=condicion_venta,
        archivo_origen=(f"{origen}: {detalle}" if detalle else origen)[:300]
                       + (" -- ya facturado anteriormente" if estado_inicial == "facturado" else ""),
        estado=estado_inicial,
        facturado_en=facturado_en_previo,
    )
    db.session.add(fila)
    return fila, None, estado_inicial


def crear_blueprint_api_externa(csrf, limiter, chequear_cuil, iniciar_lote, estado_lote):
    """
    csrf / limiter: los de app.py.
    chequear_cuil(empresa) -> None | mensaje de error (el anti-abuso de siempre).
    iniciar_lote(empresa, prefijo, estados) -> (cantidad | None, error | None).
    Devuelve (blueprint_panel, blueprint_api): registrar los dos en app.py.
    estado_lote(empresa_id) -> dict | None con el progreso del lote en curso.
    """
    bp = Blueprint("api_externa_panel", __name__)   # pantallas del usuario logueado (con CSRF)
    api = Blueprint("api_externa", __name__)        # servidor a servidor, con token (sin CSRF)

    def empresa_del_token():
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if len(token) < 20:
            return None
        empresa = Empresa.query.filter_by(api_token_hash=_hash(token)).first()
        if empresa and hmac.compare_digest(empresa.api_token_hash, _hash(token)):
            return empresa
        return None

    def requiere_token(f):
        @wraps(f)
        def envuelta(*args, **kwargs):
            empresa = empresa_del_token()
            if not empresa:
                return jsonify(ok=False, error="Token inválido. Generá uno nuevo en AutoFacturación > Empresas."), 401
            if not empresa.usuario.puede_usar_el_sistema:
                return jsonify(ok=False, error="La suscripción de AutoFacturación está vencida o suspendida."), 402
            return f(empresa, *args, **kwargs)
        return envuelta

    # ---------- Pantalla de la empresa: generar / revocar el token ----------

    @bp.route("/empresas/<int:empresa_id>/api-externa/token", methods=["POST"])
    @login_required
    def generar_token(empresa_id):
        empresa = current_user.empresas.filter_by(id=empresa_id).first()
        if not empresa:
            return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404
        token = "af_" + secrets.token_urlsafe(32)
        empresa.api_token_hash = _hash(token)
        db.session.commit()
        return jsonify(ok=True, token=token)  # se muestra UNA vez, no se puede recuperar después

    @bp.route("/empresas/<int:empresa_id>/api-externa/revocar", methods=["POST"])
    @login_required
    def revocar_token(empresa_id):
        empresa = current_user.empresas.filter_by(id=empresa_id).first()
        if not empresa:
            return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404
        empresa.api_token_hash = None
        db.session.commit()
        return jsonify(ok=True)

    # ---------- API (servidor a servidor, con token) ----------

    @api.route("/api/externo/ping", methods=["GET"])
    @requiere_token
    def ping(empresa):
        return jsonify(ok=True, empresa=empresa.nombre_interno or empresa.razon_social_arca,
                       tipo_contribuyente=empresa.tipo_contribuyente,
                       configuracion_completa=empresa.configuracion_completa(),
                       dias_restantes=empresa.usuario.dias_restantes)

    @api.route("/api/externo/comprobantes", methods=["POST"])
    @requiere_token
    def recibir_comprobantes(empresa):
        datos = request.get_json(silent=True) or {}
        lista = datos.get("comprobantes") or []
        if not isinstance(lista, list) or not lista:
            return jsonify(ok=False, error="No vino ningún comprobante."), 400
        if len(lista) > MAX_POR_ENVIO:
            return jsonify(ok=False, error=f"Máximo {MAX_POR_ENVIO} comprobantes por envío."), 400
        resultados = []
        for item in lista:
            try:
                comp, error, estado = crear_comprobante_desde_venta_externa(item, empresa)
            except Exception as e:  # uno mal armado no frena al resto
                comp, error, estado = None, f"Error inesperado: {e}", None
            resultados.append({"id_externo": item.get("id_externo"), "ok": error is None, "estado": estado,
                               "error": error})
        db.session.commit()
        return jsonify(ok=True, resultados=resultados)

    @api.route("/api/externo/comprobantes/estados", methods=["POST"])
    @requiere_token
    def estados(empresa):
        ids = [str(i) for i in ((request.get_json(silent=True) or {}).get("ids") or [])][:500]
        encontrados = Comprobante.query.filter(Comprobante.empresa_id == empresa.id,
                                               Comprobante.id_transaccion.in_(ids or ["-"])).all()
        por_id = {c.id_transaccion: c for c in encontrados}
        salida = {}
        for i in ids:
            c = por_id.get(i)
            salida[i] = ({"estado": c.estado, "error": c.error_facturacion,
                          "facturado_en": c.facturado_en.isoformat() if c.facturado_en else None}
                         if c else {"estado": "no_encontrado"})
        return jsonify(ok=True, estados=salida)

    @api.route("/api/externo/comprobantes/<path:id_externo>", methods=["DELETE"])
    @requiere_token
    def borrar(empresa, id_externo):
        c = Comprobante.query.filter_by(empresa_id=empresa.id, id_transaccion=id_externo).first()
        if not c:
            return jsonify(ok=True, borrado=False)
        if c.estado == "facturado":
            return jsonify(ok=False, error="Ya está facturado en ARCA: hay que emitir una nota de crédito."), 409
        db.session.delete(c)
        db.session.commit()
        return jsonify(ok=True, borrado=True)

    @api.route("/api/externo/facturar", methods=["POST"])
    @requiere_token
    def facturar(empresa):
        """Arranca el bot sobre los comprobantes PENDIENTES que llegaron por esta API (prefijo NEG-).
        Los que quedaron con error no se reintentan solos: se revisan a mano en AutoFacturación."""
        if not empresa.configuracion_completa():
            return jsonify(ok=False, error="A la empresa le falta configuración en AutoFacturación (Clave Fiscal, punto de venta, etc.)."), 400
        error_cuil = chequear_cuil(empresa)
        if error_cuil:
            return jsonify(ok=False, error=error_cuil), 403
        cantidad, error = iniciar_lote(empresa, PREFIJO, ("pendiente",))
        if error:
            return jsonify(ok=False, error=error, en_curso=True), 409
        return jsonify(ok=True, iniciado=bool(cantidad), total=cantidad or 0)

    @api.route("/api/externo/facturar/estado", methods=["GET"])
    @requiere_token
    def facturar_estado(empresa):
        estado = estado_lote(empresa.id)
        return jsonify(ok=True, **(estado or {"en_curso": False, "terminado": False}))

    # Las rutas de servidor a servidor no usan sesión de navegador: sin CSRF y con límite propio
    csrf.exempt(api)
    limiter.limit("120 per minute")(api)
    return bp, api

import os
import io
import tempfile
import requests
from datetime import datetime, timedelta
from functools import wraps
from sqlalchemy import inspect, text

from dotenv import load_dotenv
load_dotenv()  # lee el .env local si existe (no hace nada si no hay ninguno,
                # así que no rompe el deploy en Docker/Render, que ya reciben
                # las variables de otra forma) -- tiene que ir ANTES de
                # init_db(app) más abajo, que lee DATABASE_URL del entorno.

from flask import Flask, render_template, jsonify, request, redirect, url_for, send_file, session
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)

from models import db, init_db, Usuario, Empresa, Comprobante, RegistroSubida, DIAS_PRUEBA_GRATIS
import drive_sync
from procesador import procesar_archivo
from automatizacion.arca_bot import facturar_comprobante, calcular_fecha_facturacion
from previsualizacion_pdf import generar_pdf_preview
from almacenamiento import ruta_absoluta, eliminar_archivo_persistente
import google_drive_cliente

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "clave-de-desarrollo-cambiar-en-produccion")
init_db(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

# --- Panel de membresías central (gestiona clientes/vencimientos de todos los productos) ---
PANEL_MEMBRESIAS_URL = os.environ.get("PANEL_MEMBRESIAS_URL", "")  # ej: http://localhost:5050
PANEL_MEMBRESIAS_SECRET = os.environ.get("PANEL_MEMBRESIAS_SECRET", "")


def avisar_registro_al_panel(usuario):
    """
    Le avisa al panel de membresías que se registró un cliente nuevo, para
    que aparezca ahí sin tener que cargarlo a mano. Si el panel no está
    configurado o está apagado, no rompe el registro -- solo queda logueado.
    """
    if not PANEL_MEMBRESIAS_URL:
        return
    try:
        requests.post(
            f"{PANEL_MEMBRESIAS_URL}/api/registro-externo",
            json={
                "producto": "facturea",
                "nombre": usuario.nombre_razon_social or usuario.email,
                "email": usuario.email,
                "dias_prueba": DIAS_PRUEBA_GRATIS,
            },
            timeout=5,
        )
    except requests.exceptions.RequestException as e:
        print(f"[aviso] no se pudo avisar al panel de membresías: {e}")


def avisar_checkin_al_panel(email):
    """
    Le avisa al panel de membresías que este usuario se logueó ahora mismo,
    para que "última conexión" en el panel refleje la realidad. No rompe
    el login si el panel no está configurado o está apagado.
    """
    if not PANEL_MEMBRESIAS_URL:
        return
    try:
        requests.get(
            f"{PANEL_MEMBRESIAS_URL}/api/validar-licencia",
            params={"producto": "facturea", "email": email, "version": "web"},
            timeout=5,
        )
    except requests.exceptions.RequestException as e:
        print(f"[aviso] no se pudo avisar el check-in al panel de membresías: {e}")


def crear_admin_inicial():
    """
    Si no existe ningún administrador todavía y están configuradas las variables
    ADMIN_EMAIL y ADMIN_PASSWORD, crea la cuenta admin automáticamente al arrancar.
    No hace falta acceso a Shell (que en Render solo viene en planes pagos).
    Es seguro dejarlo: si el admin ya existe, no hace nada.
    """
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    if not email or not password:
        return

    with app.app_context():
        try:
            if Usuario.query.filter_by(es_admin=True).first():
                return  # ya hay un admin, no crear otro
        except Exception:
            # Las tablas todavía no existen -- pasa cuando este archivo se importa
            # desde "flask db upgrade" (que corre ANTES de que existan las tablas).
            # No hay nada para hacer todavía; se vuelve a intentar cuando arranque
            # el servidor de verdad, ya con las migraciones aplicadas.
            return

        existente = Usuario.query.filter_by(email=email.lower()).first()
        if existente:
            existente.es_admin = True
            db.session.commit()
            return

        admin = Usuario(email=email.lower(), nombre_razon_social="Administrador", es_admin=True, plan="full")
        admin.set_password(password)
        admin.fecha_vencimiento = datetime.utcnow() + timedelta(days=3650)
        db.session.add(admin)
        db.session.commit()


def _sync_missing_columns():
    """
    Agrega automáticamente, al arrancar, cualquier columna que exista en los
    modelos (models.py) pero todavía no en la base de datos real -- pasa
    cuando se refactoriza un modelo (ej: se agregó "empresa_id" a
    Comprobante) y la tabla ya existía de antes con datos, así que las
    migraciones de Alembic no la vuelven a crear desde cero.
    SOLO agrega columnas nuevas (nunca borra ni modifica una existente), y
    solo si son nullable o tienen un valor por defecto, para no poder
    romper filas que ya existen.
    """
    with app.app_context():
        inspector = inspect(db.engine)
        for table in db.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                if not col.nullable and col.default is None:
                    print(f"[aviso] columna {table.name}.{col.name} es NOT NULL sin default -- no se puede agregar sola.")
                    continue
                col_type = col.type.compile(db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                print(f"[info] columna agregada automáticamente: {table.name}.{col.name}")


_sync_missing_columns()


crear_admin_inicial()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))


def admin_required(f):
    @wraps(f)
    def decorada(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.es_admin:
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorada


@app.before_request
def exigir_configuracion():
    """
    Un usuario sin ninguna empresa cargada todavía no puede hacer nada más
    en el sistema -- se lo manda a /empresas a cargar la primera. El acceso
    a ARCA (CUIL + Clave Fiscal) se completa DENTRO de cada empresa, ya no
    es un paso aparte a nivel de cuenta.
    """
    if not current_user.is_authenticated or current_user.es_admin:
        return
    rutas_libres = {
        "empresas", "empresas_editar", "empresas_eliminar",
        "soporte", "logout", "static", "suscripcion_vencida",
    }
    if request.endpoint in rutas_libres:
        return
    if not current_user.tiene_configuracion_minima():
        return redirect(url_for("empresas"))


@app.before_request
def exigir_membresia_activa():
    """
    Si la prueba gratis (o la última renovación) venció, o el admin lo
    suspendió a mano, no lo deja usar nada del sistema -- lo manda a la
    pantalla de "renovar" en vez de dejarlo seguir facturando gratis.
    """
    if not current_user.is_authenticated or current_user.puede_usar_el_sistema:
        return
    rutas_libres = {"suscripcion_vencida", "soporte", "logout", "static"}
    if request.endpoint in rutas_libres:
        return
    return redirect(url_for("suscripcion_vencida"))


@app.context_processor
def inject_estado_membresia():
    """
    Deja disponibles estas variables en TODAS las plantillas sin tener que
    pasarlas a mano en cada return render_template(...) -- así el avisito
    de días restantes puede aparecer en cualquier página con un simple
    {% include %}.
    """
    if current_user.is_authenticated and not current_user.es_admin:
        return {
            "membresia_dias_restantes": current_user.dias_restantes,
            "membresia_vencida": current_user.esta_vencido,
        }
    return {}


@app.route("/suscripcion-vencida")
@login_required
def suscripcion_vencida():
    return render_template("suscripcion_vencida.html", usuario=current_user)


# ---------- Páginas públicas ----------

@app.route("/")
def home():
    return render_template("index.html")


# ---------- Cuenta ----------

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        nombre = request.form.get("nombre", "").strip()

        if not email or not password or not nombre:
            return render_template("registro.html", error="Completá todos los campos.")

        if Usuario.query.filter_by(email=email).first():
            return render_template("registro.html", error="Ya existe una cuenta con ese email.")

        nuevo = Usuario(email=email, nombre_razon_social=nombre)
        nuevo.set_password(password)
        nuevo.fecha_vencimiento = datetime.utcnow() + timedelta(days=DIAS_PRUEBA_GRATIS)
        db.session.add(nuevo)
        db.session.commit()

        avisar_registro_al_panel(nuevo)

        login_user(nuevo)
        return redirect(url_for("panel"))

    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        usuario = Usuario.query.filter_by(email=email).first()

        if not usuario or not usuario.check_password(password):
            return render_template("login.html", error="Email o contraseña incorrectos.")

        login_user(usuario)
        avisar_checkin_al_panel(usuario.email)
        if usuario.es_admin:
            return redirect(url_for("admin_panel"))
        return redirect(url_for("panel"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


# ---------- Panel de inicio (por usuario logueado) ----------

@app.route("/panel")
@login_required
def panel():
    """
    Landing page después del login: listado de empresas del usuario
    (para pymes que se facturan a sí mismas, va a ser una sola; para
    contadores/secretarios, puede haber varias), con accesos a crear/editar
    empresas y a la sección de soporte/tutoriales.
    """
    empresas = current_user.empresas.order_by(Empresa.nombre_interno).all()
    return render_template("panel.html", usuario=current_user, empresas=empresas)


# ---------- Empresas (clientes que el usuario representa) ----------

@app.route("/empresas", methods=["GET", "POST"])
@login_required
def empresas():
    if request.method == "POST":
        nueva = Empresa(usuario_id=current_user.id)
        _completar_campos_empresa(nueva, request.form)
        db.session.add(nueva)
        db.session.commit()
        return redirect(url_for("empresas"))

    lista = current_user.empresas.order_by(Empresa.nombre_interno).all()
    fecha_emision_default = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    return render_template("empresas.html", usuario=current_user, empresas=lista, empresa=None, fecha_emision_default=fecha_emision_default)


@app.route("/empresas/<int:empresa_id>/editar", methods=["GET", "POST"])
@login_required
def empresas_editar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("empresas"))

    if request.method == "POST":
        _completar_campos_empresa(empresa, request.form)
        db.session.commit()
        return redirect(url_for("empresas"))

    lista = current_user.empresas.order_by(Empresa.nombre_interno).all()
    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_emision_default = (datetime.now() - timedelta(days=dias_atras)).strftime("%Y-%m-%d")
    return render_template(
        "empresas.html", usuario=current_user, empresas=lista, empresa=empresa,
        error_drive=request.args.get("error_drive"), fecha_emision_default=fecha_emision_default,
    )


@app.route("/empresas/<int:empresa_id>/eliminar", methods=["POST"])
@login_required
def empresas_eliminar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if empresa:
        db.session.delete(empresa)
        db.session.commit()
    return redirect(url_for("empresas"))


def _completar_campos_empresa(empresa, form):
    empresa.nombre_interno = form.get("nombre_interno", "").strip()
    empresa.razon_social_arca = form.get("razon_social_arca", "").strip()

    empresa.cuil_arca = form.get("cuil_arca", "").strip()
    password_nueva = form.get("password_arca", "").strip()
    if password_nueva:  # solo la pisa si escribió algo nuevo (no la borra si la deja vacía)
        empresa.set_password_arca(password_nueva)

    empresa.config_tipo_comprobante = form.get("config_tipo_comprobante", "").strip()
    empresa.puntos_venta_disponibles = form.get("puntos_venta_disponibles", "").strip()
    empresa.config_punto_venta = form.get("config_punto_venta", "").strip()
    empresa.config_concepto = form.get("config_concepto", "").strip()
    empresa.config_condicion_iva = form.get("config_condicion_iva", "").strip()
    empresa.config_tipo_doc_receptor = form.get("config_tipo_doc_receptor", "").strip()
    empresa.config_condicion_venta = ",".join(form.getlist("config_condicion_venta"))
    empresa.config_unidad_medida = form.get("config_unidad_medida", "").strip()

    fecha_emision_str = form.get("config_fecha_emision", "").strip()
    if fecha_emision_str:
        try:
            fecha_elegida = datetime.strptime(fecha_emision_str, "%Y-%m-%d")
            # Se guarda como diferencia de días respecto de HOY, no como fecha
            # fija -- así el default sigue moviéndose solo día a día en vez de
            # quedar pegado al día en que se guardó el formulario. Si el
            # usuario eligió una fecha futura, se redondea a 0 (hoy): la fecha
            # de emisión de un comprobante no puede ser un default a futuro.
            dias_atras = max((datetime.now().date() - fecha_elegida.date()).days, 0)
            empresa.config_dias_atras_fecha_emision = dias_atras
        except ValueError:
            pass  # si vino un formato raro, no se toca lo que ya tenía guardado

    empresa.descripciones_disponibles = form.get("descripciones_disponibles", "").strip()
    lista_descripciones = [d.strip() for d in empresa.descripciones_disponibles.split(",") if d.strip()]
    empresa.config_producto_servicio = lista_descripciones[0] if lista_descripciones else ""
    empresa.config_descripcion_aleatoria = form.get("config_descripcion_aleatoria") == "on"


# ---------- Google Drive por empresa ----------

@app.route("/empresas/<int:empresa_id>/drive/conectar")
@login_required
def empresa_drive_conectar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("empresas"))
    if not google_drive_cliente.esta_configurado():
        return "Google Drive todavía no está configurado en este servidor (faltan las variables de entorno GOOGLE_OAUTH_*).", 400
    url, code_verifier = google_drive_cliente.generar_url_autorizacion(empresa.id)
    session["drive_code_verifier"] = code_verifier
    return redirect(url)


@app.route("/google-drive/callback")
@login_required
def google_drive_callback():
    empresa_id = request.args.get("state")
    empresa = current_user.empresas.filter_by(id=empresa_id).first() if empresa_id else None
    if not empresa:
        return redirect(url_for("empresas"))

    code_verifier = session.pop("drive_code_verifier", None)
    try:
        refresh_token, email = google_drive_cliente.procesar_callback(request.url, code_verifier)
    except Exception as e:
        return redirect(url_for("empresas_editar", empresa_id=empresa.id, error_drive=str(e)))

    empresa.set_google_drive_token(refresh_token)
    empresa.google_drive_email = email
    db.session.commit()
    return redirect(url_for("empresas_editar", empresa_id=empresa.id))


@app.route("/empresas/<int:empresa_id>/drive/desconectar", methods=["POST"])
@login_required
def empresa_drive_desconectar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if empresa:
        google_drive_cliente.desconectar(empresa)
        db.session.commit()
    return redirect(url_for("empresas_editar", empresa_id=empresa_id))


# ---------- Comprobantes (por empresa) ----------

@app.route("/empresas/<int:empresa_id>/comprobantes")
@login_required
def comprobantes(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("panel"))

    filas = (
        Comprobante.query.filter_by(empresa_id=empresa.id)
        .order_by(Comprobante.creado_en.desc())
        .all()
    )

    # La fecha REAL del comprobante (fecha_comprobante) no es necesariamente
    # la que termina en ARCA -- calcular_fecha_facturacion puede ajustarla
    # hacia el default de la empresa. Se calcula acá, por fila, para
    # mostrarla en la tabla sin guardar nada nuevo en la base (es un atributo
    # de Python en memoria, no una columna -- no se persiste ni hace falta).
    for c in filas:
        c.fecha_facturacion_ajustada = None
        if c.fecha_comprobante:
            try:
                c.fecha_facturacion_ajustada = calcular_fecha_facturacion(
                    c.fecha_comprobante, empresa.config_dias_atras_fecha_emision
                )
            except ValueError:
                pass  # fecha con formato inesperado -- se deja en None, la tabla muestra "—"

        # facturado_en se guarda en UTC (datetime.utcnow) -- para mostrarlo hay
        # que pasarlo a hora de Argentina (UTC-3, sin horario de verano) o
        # quedaría 3 horas adelantado en la tabla.
        c.facturado_en_ar = (c.facturado_en - timedelta(hours=3)) if c.facturado_en else None

    datos_revision = [
        {
            "id": c.id,
            "estado": c.estado,
            "tiene_archivo": bool(c.archivo_ruta),
            "extension": (c.archivo_origen or "").rsplit(".", 1)[-1].lower() if c.archivo_origen and "." in c.archivo_origen else "",
            "campos": {
                "punto_venta": c.punto_venta, "tipo_comprobante": c.tipo_comprobante,
                "fecha_comprobante": c.fecha_comprobante, "concepto": c.concepto,
                "fecha_desde": c.fecha_desde, "fecha_hasta": c.fecha_hasta,
                "tipo_documento": c.tipo_documento, "cuit_receptor": c.cuit_receptor,
                "medio_pago_detectado": c.medio_pago_detectado,
                "condicion_iva": c.condicion_iva, "condicion_venta": c.condicion_venta,
                "tipo_pago": c.tipo_pago, "numero_pago": c.numero_pago,
                "descripcion": c.descripcion, "cantidad": c.cantidad,
                "unidad_medida": c.unidad_medida, "precio_unitario": c.precio_unitario,
                "nombre_razon_social": c.nombre_razon_social, "id_transaccion": c.id_transaccion,
            },
        }
        for c in filas
    ]

    return render_template(
        "comprobantes.html", comprobantes=filas, usuario=current_user, empresa=empresa,
        datos_revision=datos_revision, stats=_calcular_estadisticas(empresa.id),
    )


def _calcular_estadisticas(empresa_id):
    """
    Estadísticas ACUMULADAS de toda la historia de la empresa (no solo la
    última subida) -- para eso existe RegistroSubida, que guarda un renglón
    por cada archivo que se intentó subir, se haya convertido en comprobante
    o no.
    """
    registros = RegistroSubida.query.filter_by(empresa_id=empresa_id).all()
    nuevos = [r for r in registros if r.resultado == "nuevo"]
    duplicados = [r for r in registros if r.resultado == "duplicado"]
    bloqueados = [r for r in registros if r.resultado == "ignorado"]
    con_error = [r for r in registros if r.resultado == "error"]
    imagenes = [r for r in registros if (r.extension or "") in ("png", "jpg", "jpeg")]
    pdfs = [r for r in registros if (r.extension or "") == "pdf"]

    comprobantes = Comprobante.query.filter_by(empresa_id=empresa_id).all()
    facturados = [c for c in comprobantes if c.estado == "facturado"]
    pendientes = [c for c in comprobantes if c.estado in ("pendiente", "error")]
    monto_pendiente = sum((c.importe_total or 0) for c in pendientes)
    monto_facturado_total = sum((c.importe_total or 0) for c in facturados)

    return {
        "total_subidos": len(registros),
        "imagenes": len(imagenes),
        "pdfs": len(pdfs),
        "nuevos": len(nuevos),
        "duplicados": len(duplicados),
        "bloqueados": len(bloqueados),
        "con_error": len(con_error),
        "duplicados_detalle": [
            {
                "registro_id": r.id, "nombre_archivo": r.nombre_archivo,
                "comprobante_original_id": r.comprobante_id, "tiene_imagen_intento": bool(r.archivo_ruta or r.archivo_drive_id),
            }
            for r in duplicados
        ],
        "nombres_bloqueados": [r.nombre_archivo for r in bloqueados],
        "nombres_error": [r.nombre_archivo for r in con_error],
        "facturados": len(facturados),
        "pendientes": len(pendientes),
        "monto_pendiente": monto_pendiente,
        "monto_facturado_total": monto_facturado_total,
    }


@app.route("/api/empresas/<int:empresa_id>/subir", methods=["POST"])
@login_required
def api_subir(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify(ok=False, error="No se recibió ningún archivo."), 400

    fecha_hoy = datetime.now().strftime("%d/%m/%Y")
    resumen = {"nuevos": 0, "duplicados": 0, "errores": 0, "ignorados": 0}
    clave = {"nuevo": "nuevos", "duplicado": "duplicados", "error": "errores", "ignorado": "ignorados"}

    with tempfile.TemporaryDirectory() as tmp:
        for archivo in archivos:
            if not archivo.filename:
                continue
            ruta_local = os.path.join(tmp, archivo.filename)
            archivo.save(ruta_local)
            resultado, comprobante_relacionado, (archivo_ruta_intento, archivo_drive_id_intento) = procesar_archivo(ruta_local, archivo.filename, current_user.id, empresa.id, fecha_hoy)
            resumen[clave[resultado]] += 1

            ext = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
            db.session.add(RegistroSubida(
                empresa_id=empresa.id, nombre_archivo=archivo.filename, extension=ext,
                resultado=resultado, comprobante=comprobante_relacionado,
                archivo_ruta=archivo_ruta_intento, archivo_drive_id=archivo_drive_id_intento,
            ))

    db.session.commit()
    return jsonify(ok=True, resumen=resumen)


@app.route("/api/empresas/<int:empresa_id>/sincronizar", methods=["POST"])
@login_required
def api_sincronizar(empresa_id):
    """Herramienta administrativa: sincroniza la carpeta de Drive de prueba con la empresa indicada."""
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404
    if not DRIVE_FOLDER_ID:
        return jsonify(ok=False, error="Falta configurar DRIVE_FOLDER_ID."), 400
    try:
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        resumen = drive_sync.sincronizar_carpeta(DRIVE_FOLDER_ID, fecha_hoy, current_user.id, empresa.id)
        return jsonify(ok=True, resumen=resumen)
    except FileNotFoundError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


def _comprobante_facturado_con_mismo_id_transaccion(comprobante):
    """
    Busca si YA existe otro comprobante de la misma empresa, con el mismo
    id_transaccion, que ya haya sido facturado -- para no facturar dos
    veces la misma transacción por error (dos registros distintos que
    terminan apuntando al mismo movimiento real, por ejemplo si el
    comprobante se subió dos veces por caminos distintos y el chequeo de
    duplicados al subir no lo agarró). Si el comprobante no tiene
    id_transaccion detectado, no hay nada para comparar -- se deja pasar.
    Devuelve el comprobante conflictivo, o None si no hay problema.
    """
    if not comprobante.id_transaccion:
        return None
    return Comprobante.query.filter(
        Comprobante.empresa_id == comprobante.empresa_id,
        Comprobante.id_transaccion == comprobante.id_transaccion,
        Comprobante.estado == "facturado",
        Comprobante.id != comprobante.id,
    ).first()


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/facturar", methods=["POST"])
@login_required
def facturar(empresa_id, comprobante_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado."), 400

    conflicto = _comprobante_facturado_con_mismo_id_transaccion(comprobante)
    if conflicto:
        return jsonify(
            ok=False,
            error=f"Ya existe un comprobante facturado (#{conflicto.id}) con el mismo ID de transacción -- no se factura de nuevo para evitar duplicar.",
        ), 400

    modo_prueba = bool((request.get_json(silent=True) or {}).get("modo_prueba"))

    try:
        resultado = facturar_comprobante(comprobante, modo_prueba=modo_prueba)
        if not modo_prueba:
            comprobante.estado = "facturado"
            comprobante.error_facturacion = None
            comprobante.facturado_en = datetime.utcnow()
            if resultado["fecha_ajustada"]:
                # La fecha real de ARCA quedó DISTINTA a la que tenía el comprobante
                # (por la regla de "no se puede facturar con más de 10 días de atraso").
                # Se actualiza acá para que la tabla no mienta sobre qué fecha quedó
                # escrita de verdad en la factura.
                comprobante.fecha_comprobante = resultado["fecha_usada"]
            db.session.commit()
        return jsonify(
            ok=True, modo_prueba=modo_prueba,
            fecha_usada=resultado["fecha_usada"], fecha_ajustada=resultado["fecha_ajustada"],
        )
    except Exception as e:
        if not modo_prueba:
            comprobante.estado = "error"
            comprobante.error_facturacion = str(e)
            db.session.commit()
        return jsonify(ok=False, error=str(e), modo_prueba=modo_prueba), 500


CAMPOS_EDITABLES_COMPROBANTE = {
    "punto_venta": "texto",
    "tipo_comprobante": "texto",
    "fecha_comprobante": "texto",
    "concepto": "texto",
    "fecha_desde": "texto",
    "fecha_hasta": "texto",
    "tipo_documento": "texto",
    "cuit_receptor": "texto",
    "medio_pago_detectado": "texto",
    "condicion_iva": "texto",
    "condicion_venta": "texto",
    "tipo_pago": "texto",
    "numero_pago": "texto",
    "descripcion": "texto",
    "cantidad": "numero",
    "unidad_medida": "texto",
    "precio_unitario": "numero",
    "nombre_razon_social": "texto",
    "id_transaccion": "texto",
}


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/editar", methods=["POST"])
@login_required
def comprobante_editar_campo(empresa_id, comprobante_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado, no se puede editar."), 400

    datos = request.get_json(silent=True) or {}
    campo = datos.get("campo")
    valor = datos.get("valor")

    tipo = CAMPOS_EDITABLES_COMPROBANTE.get(campo)
    if not tipo:
        return jsonify(ok=False, error=f"El campo '{campo}' no se puede editar."), 400

    if tipo == "numero":
        try:
            valor = float(str(valor).replace(",", "."))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Ese valor no es un número válido."), 400

    setattr(comprobante, campo, valor)
    if campo in ("cantidad", "precio_unitario"):
        comprobante.recalcular_importe()

    db.session.commit()
    return jsonify(ok=True, importe_total=comprobante.importe_total)


@app.route("/empresas/<int:empresa_id>/comprobantes/editar-columna", methods=["POST"])
@login_required
def comprobantes_editar_columna(empresa_id):
    """
    Aplica el mismo valor a TODOS los comprobantes pendientes (no facturados)
    de esta empresa, en un solo campo -- para no tener que editar fila por
    fila cuando todos deberían tener, por ejemplo, la misma Condición de IVA.
    Los comprobantes ya facturados quedan afuera, igual que en la edición
    individual.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    datos = request.get_json(silent=True) or {}
    campo = datos.get("campo")
    valor = datos.get("valor")

    tipo = CAMPOS_EDITABLES_COMPROBANTE.get(campo)
    if not tipo:
        return jsonify(ok=False, error=f"El campo '{campo}' no se puede editar."), 400

    if tipo == "numero":
        try:
            valor = float(str(valor).replace(",", "."))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Ese valor no es un número válido."), 400

    comprobantes_afectados = Comprobante.query.filter(
        Comprobante.empresa_id == empresa.id,
        Comprobante.estado != "facturado",
    ).all()

    for comprobante in comprobantes_afectados:
        setattr(comprobante, campo, valor)
        if campo in ("cantidad", "precio_unitario"):
            comprobante.recalcular_importe()

    db.session.commit()
    return jsonify(ok=True, actualizados=len(comprobantes_afectados))


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/eliminar", methods=["POST"])
@login_required
def comprobante_eliminar(empresa_id, comprobante_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado, no se puede eliminar."), 400

    eliminar_archivo_persistente(empresa, comprobante.archivo_ruta, comprobante.archivo_drive_id)
    db.session.delete(comprobante)
    db.session.commit()
    return jsonify(ok=True)


@app.route("/empresas/<int:empresa_id>/comprobantes/eliminar-todos", methods=["POST"])
@login_required
def comprobantes_eliminar_todos(empresa_id):
    """
    Borra TODOS los comprobantes de esta empresa, sin importar el estado
    (pendiente, error, o facturado). Borrar uno ya facturado solo borra el
    registro local en Facturea -- la factura real ya emitida en ARCA, con
    su CAE, no se ve afectada ni se anula por esto.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobantes = Comprobante.query.filter_by(empresa_id=empresa.id).all()
    cantidad = len(comprobantes)
    for c in comprobantes:
        eliminar_archivo_persistente(empresa, c.archivo_ruta, c.archivo_drive_id)
        db.session.delete(c)

    # Este botón es un reset total ("eliminar TODOS los archivos"), así que
    # también hay que borrar el historial de RegistroSubida completo -- si no,
    # quedan renglones residuales de subidas viejas (algunos apuntando a un
    # archivo_drive_id de un Drive ya desconectado, que rompe la imagen del
    # comparador de duplicados con un ícono roto en vez de mostrar nada
    # coherente) y las estadísticas de "archivos subidos en total" nunca
    # bajan a cero aunque se haya borrado todo.
    registros = RegistroSubida.query.filter_by(empresa_id=empresa.id).all()
    for r in registros:
        eliminar_archivo_persistente(empresa, r.archivo_ruta, r.archivo_drive_id)
        db.session.delete(r)

    db.session.commit()
    return jsonify(ok=True, eliminados=cantidad)


@app.route("/empresas/<int:empresa_id>/comprobantes/eliminar-facturados", methods=["POST"])
@login_required
def comprobantes_eliminar_facturados(empresa_id):
    """
    Borra solo los comprobantes en estado "facturado" -- útil para limpiar la
    tabla de lo que ya está resuelto. Igual que arriba: esto borra el
    registro local, no anula la factura real ya emitida en ARCA.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobantes = Comprobante.query.filter_by(empresa_id=empresa.id, estado="facturado").all()
    cantidad = len(comprobantes)
    ids_borrados = [c.id for c in comprobantes]
    for c in comprobantes:
        eliminar_archivo_persistente(empresa, c.archivo_ruta, c.archivo_drive_id)
        db.session.delete(c)

    # Los RegistroSubida de "duplicado" que apuntaban a alguno de estos
    # comprobantes como "original" quedarían con un comprobante_id fantasma
    # (el ForeignKey no tiene cascade desde este lado) -- se limpia la
    # referencia, no el registro entero, para no perder las estadísticas
    # históricas. El front ya maneja bien un comprobante_id vacío (muestra
    # "No encontré el comprobante original" en vez de romper la imagen).
    if ids_borrados:
        RegistroSubida.query.filter(
            RegistroSubida.empresa_id == empresa.id,
            RegistroSubida.comprobante_id.in_(ids_borrados),
        ).update({"comprobante_id": None}, synchronize_session=False)

    db.session.commit()
    return jsonify(ok=True, eliminados=cantidad)


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/archivo")
@login_required
def comprobante_archivo(empresa_id, comprobante_id):
    """
    Sirve el archivo original (imagen o PDF) de un comprobante, para el
    panel de Revisión Manual -- con chequeo de dueño, a diferencia de un
    archivo estático común, para que un usuario no pueda ver comprobantes
    de otra cuenta adivinando la URL.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return "Esa empresa no existe o no te pertenece.", 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante or not (comprobante.archivo_ruta or comprobante.archivo_drive_id):
        return "No hay un archivo guardado para este comprobante.", 404

    if comprobante.archivo_drive_id:
        try:
            from google_drive_cliente import descargar_archivo
            contenido, mimetype = descargar_archivo(empresa, comprobante.archivo_drive_id)
            return send_file(io.BytesIO(contenido), mimetype=mimetype)
        except Exception as e:
            return f"No se pudo traer el archivo desde Google Drive: {e}", 502

    ruta = ruta_absoluta(comprobante.archivo_ruta)
    if not os.path.exists(ruta):
        return "El archivo ya no está disponible en el servidor.", 404

    return send_file(ruta)


@app.route("/empresas/<int:empresa_id>/registros/<int:registro_id>/archivo")
@login_required
def registro_subida_archivo(empresa_id, registro_id):
    """
    Sirve la imagen de un INTENTO duplicado (la del archivo que se rechazó
    por repetido, no la del comprobante original) -- para poder compararlas
    en la pestaña de Estadísticas.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return "Esa empresa no existe o no te pertenece.", 404

    registro = RegistroSubida.query.filter_by(id=registro_id, empresa_id=empresa.id).first()
    if not registro or not (registro.archivo_ruta or registro.archivo_drive_id):
        return "No hay una imagen guardada para este intento.", 404

    if registro.archivo_drive_id:
        try:
            from google_drive_cliente import descargar_archivo
            contenido, mimetype = descargar_archivo(empresa, registro.archivo_drive_id)
            return send_file(io.BytesIO(contenido), mimetype=mimetype)
        except Exception as e:
            return f"No se pudo traer el archivo desde Google Drive: {e}", 502

    ruta = ruta_absoluta(registro.archivo_ruta)
    if not os.path.exists(ruta):
        return "El archivo ya no está disponible en el servidor.", 404

    return send_file(ruta)


@app.route("/empresas/<int:empresa_id>/comprobantes/facturar-todos", methods=["POST"])
@login_required
def facturar_todos(empresa_id):
    """
    Factura, uno por uno y en orden, todos los comprobantes pendientes (o que
    habían fallado antes) de esta empresa. Si alguno falla, se guarda su
    error y se sigue con el siguiente -- no se corta todo por uno solo.

    Si viene un "limite_monto" en el body, se para de facturar apenas el
    PRÓXIMO comprobante haría que el total acumulado supere ese límite --
    los que queden después quedan sin tocar, tal cual estaban.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    datos = request.get_json(silent=True) or {}
    limite_monto = datos.get("limite_monto")
    try:
        limite_monto = float(limite_monto) if limite_monto not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(ok=False, error="El límite de monto no es un número válido."), 400

    pendientes = (
        Comprobante.query.filter(
            Comprobante.empresa_id == empresa.id,
            Comprobante.estado.in_(["pendiente", "error"]),
        )
        .order_by(Comprobante.creado_en.asc())
        .all()
    )

    resumen = {"facturados": 0, "errores": 0, "detalle": [], "detenido_por_limite": False, "monto_facturado": 0.0}
    acumulado = 0.0
    for comprobante in pendientes:
        importe = comprobante.importe_total or 0.0
        if limite_monto is not None and (acumulado + importe) > limite_monto:
            resumen["detenido_por_limite"] = True
            break

        conflicto = _comprobante_facturado_con_mismo_id_transaccion(comprobante)
        if conflicto:
            comprobante.estado = "error"
            comprobante.error_facturacion = (
                f"Ya existe un comprobante facturado (#{conflicto.id}) con el mismo ID de "
                "transacción -- no se factura de nuevo para evitar duplicar."
            )
            resumen["errores"] += 1
            resumen["detalle"].append({"id": comprobante.id, "error": comprobante.error_facturacion})
            db.session.commit()
            continue

        try:
            resultado = facturar_comprobante(comprobante)
            comprobante.estado = "facturado"
            comprobante.error_facturacion = None
            comprobante.facturado_en = datetime.utcnow()
            if resultado["fecha_ajustada"]:
                comprobante.fecha_comprobante = resultado["fecha_usada"]
            resumen["facturados"] += 1
            acumulado += importe
            resumen["monto_facturado"] = acumulado
        except Exception as e:
            comprobante.estado = "error"
            comprobante.error_facturacion = str(e)
            resumen["errores"] += 1
            resumen["detalle"].append({"id": comprobante.id, "error": str(e)})
        db.session.commit()  # se guarda uno a uno: si se corta a mitad de camino, no se pierde lo ya facturado

    return jsonify(ok=True, resumen=resumen)


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/vista-previa.pdf")
@login_required
def comprobante_vista_previa_pdf(empresa_id, comprobante_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return "Esa empresa no existe o no te pertenece.", 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return "Ese comprobante no existe.", 404

    buffer = generar_pdf_preview(comprobante)
    return send_file(
        buffer, mimetype="application/pdf", as_attachment=False,
        download_name=f"modelo_comprobante_{comprobante.id}.pdf",
    )


# ---------- Soporte / tutoriales ----------

@app.route("/soporte")
@login_required
def soporte():
    """
    Sección con guías de uso, contacto y pedido de revisión técnica.
    Por ahora es contenido estático; más adelante puede sumar un formulario
    real de tickets.
    """
    return render_template("soporte.html", usuario=current_user)


# ---------- Panel de administrador ----------

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    usuarios = Usuario.query.order_by(Usuario.fecha_registro.desc()).all()
    return render_template("admin.html", usuarios=usuarios)


@app.route("/admin/renovar/<int:usuario_id>", methods=["POST"])
@login_required
@admin_required
def admin_renovar(usuario_id):
    usuario = db.session.get(Usuario, usuario_id)
    if usuario:
        usuario.renovar(dias=30)
        db.session.commit()
    return redirect(url_for("admin_panel"))


@app.route("/admin/metodo_pago/<int:usuario_id>", methods=["POST"])
@login_required
@admin_required
def admin_metodo_pago(usuario_id):
    usuario = db.session.get(Usuario, usuario_id)
    if usuario:
        usuario.metodo_pago = request.form.get("metodo_pago", "").strip()
        db.session.commit()
    return redirect(url_for("admin_panel"))


# ---------- Sincronización con el panel de membresías central ----------

@app.route("/api/interno/sincronizar-membresia", methods=["POST"])
def sincronizar_membresia():
    """
    El panel de membresías llama acá cada vez que renueva o cancela la
    suscripción de un cliente, para que el acceso real en Facturea quede
    al día. Protegido con una clave compartida -- no requiere login porque
    quien llama es el otro servidor, no una persona.
    """
    clave_recibida = request.headers.get("X-Webhook-Secret", "")
    if not PANEL_MEMBRESIAS_SECRET or clave_recibida != PANEL_MEMBRESIAS_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario:
        return jsonify({"error": "usuario no encontrado"}), 404

    if data.get("fecha_vencimiento"):
        usuario.fecha_vencimiento = datetime.fromisoformat(data["fecha_vencimiento"])
    if "activo" in data:
        usuario.activo = bool(data["activo"])

    db.session.commit()
    return jsonify({"ok": True, "estado": usuario.estado})


# ---------- Utilidad para crear el primer administrador ----------

@app.cli.command("crear-admin")
def crear_admin():
    """Uso: flask crear-admin (te pide email y contraseña por consola)."""
    import getpass
    email = input("Email del admin: ").strip().lower()
    password = getpass.getpass("Contraseña: ")
    nombre = input("Nombre: ").strip()

    existente = Usuario.query.filter_by(email=email).first()
    if existente:
        existente.es_admin = True
        db.session.commit()
        print(f"'{email}' ya existía, ahora tiene permisos de administrador.")
        return

    admin = Usuario(email=email, nombre_razon_social=nombre, es_admin=True, plan="full")
    admin.set_password(password)
    admin.fecha_vencimiento = datetime.utcnow() + timedelta(days=3650)
    db.session.add(admin)
    db.session.commit()
    print(f"Administrador '{email}' creado correctamente.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)

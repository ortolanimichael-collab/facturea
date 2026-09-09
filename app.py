import os
import io
import random
import re
import tempfile
import threading
import requests
from datetime import datetime, timedelta
from functools import wraps
from sqlalchemy import inspect, text, or_

from dotenv import load_dotenv
load_dotenv()  # lee el .env local si existe (no hace nada si no hay ninguno,
                # así que no rompe el deploy en Docker/Render, que ya reciben
                # las variables de otra forma) -- tiene que ir ANTES de
                # init_db(app) más abajo, que lee DATABASE_URL del entorno.

from flask import Flask, render_template, jsonify, request, redirect, url_for, send_file, session
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)

from models import db, init_db, Usuario, Empresa, Comprobante, ComprobanteLinea, RegistroSubida, DIAS_PRUEBA_GRATIS
import drive_sync
from procesador import procesar_archivo
import procesador
from automatizacion.arca_bot import facturar_comprobante, calcular_fecha_facturacion, concepto_efectivo
from previsualizacion_pdf import generar_pdf_preview, CONCEPTOS
from almacenamiento import ruta_absoluta, eliminar_archivo_persistente
import google_drive_cliente
import mercadopago_cliente

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "clave-de-desarrollo-cambiar-en-produccion")
init_db(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

# --- Estado en memoria del "Facturar todo lo pendiente" ---
#
# IMPORTANTE: todo esto vive en memoria del proceso de Python (un dict y un
# set comunes), NO en la base de datos. Eso solo funciona bien si el
# servidor corre con UN SOLO worker de Gunicorn (varios threads adentro de
# ese único proceso están bien, comparten memoria) -- si corriera con más
# de un worker, cada uno tendría su propia copia y un pedido que cae en un
# worker distinto al que está facturando no vería nada de esto (el botón
# "Detener" fallaría, y la consulta de progreso también). Por eso en Render
# hace falta tener la variable de entorno GUNICORN_WORKERS=1 (ver
# entrypoint.sh) -- para compensar la falta de paralelismo entre procesos
# se puede subir GUNICORN_THREADS en cambio, que sí comparten esta memoria.
#
# IDs de empresa que pidieron detener un "Facturar todo lo pendiente" en
# curso -- se consulta desde adentro del bucle de _facturar_todos_en_segundo_plano
# (ver más abajo) y se limpia solo al terminar.
_detener_facturacion_solicitado = set()

# Progreso del lote de facturación en curso (o del último que corrió) por
# empresa -- lo consulta el frontend con polling para mostrar la barra de
# avance, y así el usuario puede cerrar la pestaña sin perder nada: el
# hilo de fondo sigue corriendo en el servidor de todas formas.
_estado_facturacion_lote = {}  # empresa_id -> dict con el progreso
_lock_facturacion_lote = threading.Lock()  # protege el dict de arriba entre threads


def _estado_inicial_lote(total):
    return {
        "en_curso": True,
        "terminado": False,
        "total": total,
        "procesados": 0,
        "facturados": 0,
        "errores": 0,
        "monto_facturado": 0.0,
        "detenido_por_limite": False,
        "detenido_manualmente": False,
        "detalle": [],
        "error_fatal": None,
    }


def _lote_marcar_progreso(empresa_id, *, facturado=False, error=False, monto_facturado=None, detalle_item=None):
    """Suma un comprobante ya procesado (facturado o con error) al estado del lote."""
    with _lock_facturacion_lote:
        estado = _estado_facturacion_lote.get(empresa_id)
        if estado is None:
            return
        estado["procesados"] += 1
        if facturado:
            estado["facturados"] += 1
        if error:
            estado["errores"] += 1
        if monto_facturado is not None:
            estado["monto_facturado"] = monto_facturado
        if detalle_item is not None:
            estado["detalle"].append(detalle_item)


def _lote_actualizar_flags(empresa_id, **cambios):
    with _lock_facturacion_lote:
        estado = _estado_facturacion_lote.get(empresa_id)
        if estado is None:
            return
        estado.update(cambios)


def _facturar_todos_en_segundo_plano(app, empresa_id, comprobante_ids, limite_monto):
    """
    Corre el lote completo de facturación en un hilo aparte, para que el
    pedido HTTP que lo dispara (la ruta facturar_todos, más abajo) pueda
    responder al toque en vez de tener al navegador esperando los minutos
    que tarde todo el lote. Gracias a esto, el usuario puede cerrar la
    pestaña o el navegador entero apenas arranca: este hilo sigue
    corriendo en el servidor, totalmente independiente del navegador.

    Necesita armar su propio contexto de aplicación (app.app_context())
    porque corre fuera del ciclo normal de un pedido HTTP -- ahí es donde
    Flask arma ese contexto solo, pero acá hay que hacerlo a mano para
    poder usar la base de datos y todo lo demás.
    """
    with app.app_context():
        acumulado = 0.0
        try:
            for comprobante_id in comprobante_ids:
                if empresa_id in _detener_facturacion_solicitado:
                    _lote_actualizar_flags(empresa_id, detenido_manualmente=True)
                    break

                comprobante = db.session.get(Comprobante, comprobante_id)
                if comprobante is None or comprobante.estado not in ("pendiente", "error"):
                    # se borró o ya se facturó a mano entre que se armó la lista y ahora
                    continue

                importe = comprobante.importe_total or 0.0
                if limite_monto is not None and (acumulado + importe) > limite_monto:
                    _lote_actualizar_flags(empresa_id, detenido_por_limite=True)
                    break

                conflicto = _comprobante_facturado_con_mismo_id_transaccion(comprobante)
                if conflicto:
                    comprobante.estado = "error"
                    comprobante.error_facturacion = (
                        f"Ya existe un comprobante facturado (#{conflicto.id}) con el mismo ID de "
                        "transacción -- no se factura de nuevo para evitar duplicar."
                    )
                    db.session.commit()
                    _lote_marcar_progreso(
                        empresa_id, error=True,
                        detalle_item={"id": comprobante.id, "error": comprobante.error_facturacion},
                    )
                    continue

                try:
                    resultado = facturar_comprobante(comprobante)
                    comprobante.estado = "facturado"
                    comprobante.error_facturacion = None
                    comprobante.facturado_en = datetime.utcnow()
                    if resultado["fecha_ajustada"]:
                        comprobante.fecha_comprobante = resultado["fecha_usada"]
                    acumulado += importe
                    db.session.commit()
                    _lote_marcar_progreso(empresa_id, facturado=True, monto_facturado=acumulado)
                except Exception as e:
                    comprobante.estado = "error"
                    comprobante.error_facturacion = str(e)
                    db.session.commit()  # se guarda uno a uno: si se corta a mitad de camino, no se pierde lo ya facturado
                    _lote_marcar_progreso(
                        empresa_id, error=True,
                        detalle_item={"id": comprobante.id, "error": str(e)},
                    )
        except Exception as e:
            # Error inesperado que corta todo el hilo (ej. se cayó la conexión a
            # la base) -- se guarda para poder avisarle al usuario en vez de
            # dejar el estado colgado en "en_curso" para siempre.
            _lote_actualizar_flags(empresa_id, error_fatal=str(e))
        finally:
            _detener_facturacion_solicitado.discard(empresa_id)
            _lote_actualizar_flags(empresa_id, en_curso=False, terminado=True)

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

# --- Panel de membresías central (gestiona clientes/vencimientos de todos los productos) ---
PANEL_MEMBRESIAS_URL = os.environ.get("PANEL_MEMBRESIAS_URL", "")  # ej: http://localhost:5050
PANEL_MEMBRESIAS_SECRET = os.environ.get("PANEL_MEMBRESIAS_SECRET", "")


def _enviar_registro_al_panel(nombre, email):
    """Hace el POST real a panel-membresías avisando el alta -- función
    interna, sin hilo propio, para poder encadenarla en orden con el
    check-in (ver avisar_registro_y_checkin_al_panel más abajo)."""
    if not PANEL_MEMBRESIAS_URL:
        return
    try:
        requests.post(
            f"{PANEL_MEMBRESIAS_URL}/api/registro-externo",
            json={
                "producto": "facturea",
                "nombre": nombre,
                "email": email,
                "dias_prueba": DIAS_PRUEBA_GRATIS,
            },
            timeout=65,  # le da margen a que panel-membresías despierte del reposo
        )
    except requests.exceptions.RequestException as e:
        print(f"[aviso] no se pudo avisar al panel de membresías: {e}")


def _enviar_checkin_al_panel(email):
    """Hace el GET real a panel-membresías avisando el check-in -- función
    interna, sin hilo propio, mismo motivo que la de arriba."""
    if not PANEL_MEMBRESIAS_URL:
        return
    try:
        requests.get(
            f"{PANEL_MEMBRESIAS_URL}/api/validar-licencia",
            params={"producto": "facturea", "email": email, "version": "web"},
            timeout=65,
        )
    except requests.exceptions.RequestException as e:
        print(f"[aviso] no se pudo avisar el check-in al panel de membresías: {e}")


def avisar_registro_al_panel(usuario):
    """
    Le avisa al panel de membresías que se registró un cliente nuevo, para
    que aparezca ahí sin tener que cargarlo a mano. Si el panel no está
    configurado o está apagado, no rompe el registro -- solo queda logueado.

    El aviso se manda en un hilo de FONDO, no en el mismo pedido del
    registro -- panel-membresías está en el plan Free de Render, que se
    duerme por inactividad y puede tardar hasta 50-60 segundos en
    despertar. Si se esperara esa respuesta acá mismo, quien se está
    registrando se queda con la pantalla congelada todo ese tiempo
    (confirmado con un caso real: se perdió un aviso porque el timeout
    de 5 segundos de antes ni siquiera le daba tiempo a despertar).
    Mandándolo de fondo, con más margen de tiempo, el registro responde
    al instante igual, y el aviso tiene una chance real de llegar.
    """
    # Se sacan los valores ACÁ, antes de lanzar el hilo -- el objeto `usuario`
    # viene de SQLAlchemy, y una vez que este pedido termine (que puede pasar
    # antes de que el hilo de fondo llegue a correr), su sesión puede quedar
    # cerrada; tratar de leer sus atributos en ese momento tira error. Pasando
    # strings sueltos al hilo, en vez del objeto entero, se evita el problema.
    nombre = usuario.nombre_razon_social or usuario.email
    email = usuario.email
    threading.Thread(target=_enviar_registro_al_panel, args=(nombre, email), daemon=True).start()


def avisar_checkin_al_panel(email):
    """
    Le avisa al panel de membresías que este usuario se logueó ahora mismo,
    para que "última conexión" en el panel refleje la realidad. No rompe
    el login si el panel no está configurado o está apagado.

    Mismo criterio que avisar_registro_al_panel: se manda de fondo, para
    no hacer esperar el login de nadie a que panel-membresías despierte.
    """
    threading.Thread(target=_enviar_checkin_al_panel, args=(email,), daemon=True).start()


def avisar_registro_y_checkin_al_panel(usuario):
    """
    Para cuando alguien se REGISTRA (que de paso ya lo deja logueado):
    manda el aviso de alta y el de check-in EN ORDEN, dentro del MISMO hilo
    de fondo -- si se lanzaran como dos hilos separados (como pasaba antes),
    no hay ninguna garantía de en qué orden le llegan a panel-membresías, y
    quedó confirmado con un caso real que el check-in podía llegar ANTES de
    que la suscripción nueva terminara de crearse del otro lado -- ahí no
    hay nada todavía que marcar como conectado, y "última conexión" se
    quedaba en blanco aunque el aviso en sí no fallara.
    """
    if not PANEL_MEMBRESIAS_URL:
        return
    nombre = usuario.nombre_razon_social or usuario.email
    email = usuario.email

    def _mandar_en_orden():
        _enviar_registro_al_panel(nombre, email)
        _enviar_checkin_al_panel(email)

    threading.Thread(target=_mandar_en_orden, daemon=True).start()


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
    Si la columna nueva es NOT NULL sin default, solo se agrega así cuando
    la tabla está VACÍA (no hay ninguna fila que pueda violar la
    restricción); si la tabla ya tiene filas, se salta y avisa, para no
    romper datos existentes.
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

                not_null_sin_default = not col.nullable and col.default is None
                if not_null_sin_default:
                    with db.engine.connect() as conn:
                        cantidad_filas = conn.execute(text(f'SELECT COUNT(*) FROM "{table.name}"')).scalar()
                    if cantidad_filas > 0:
                        print(f"[aviso] columna {table.name}.{col.name} es NOT NULL sin default y la tabla tiene {cantidad_filas} filas -- no se puede agregar sola.")
                        continue

                col_type = col.type.compile(db.engine.dialect)
                sufijo_not_null = " NOT NULL" if not_null_sin_default else ""
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}{sufijo_not_null}'))
                print(f"[info] columna agregada automáticamente: {table.name}.{col.name}")


with app.app_context():
    # Crea las tablas que estén en el modelo pero todavía no existan en la
    # base (ej: comprobante_lineas, agregada para las líneas extra de
    # Responsable Inscripto) -- no toca ni borra nada de las que ya existen.
    db.create_all()

_sync_missing_columns()


def _sync_column_widths():
    """
    Si un campo de texto (VARCHAR) se agranda en el modelo (ej: pasó de
    guardar un solo valor a una lista separada por coma, como
    Empresa.config_tipo_comprobante), esto lo agranda también en la base
    real -- _sync_missing_columns() de arriba solo agrega columnas NUEVAS,
    no cambia el tamaño de una que ya existe.

    Agrandar es siempre seguro (nunca se pierde texto ya guardado, algo que
    sí podría pasar si se achicara) -- por eso esto solo agranda, nunca
    achica, y solo toca columnas de texto con largo fijo (VARCHAR), no
    Integer/Boolean/etc.

    Solo corre contra Postgres: SQLite (la base local de prueba) ni
    siquiera hace cumplir el largo de un VARCHAR, así que no hace falta
    tocar nada ahí, y además su ALTER TABLE no soporta cambiar el tipo de
    una columna existente (rompería con un error si lo intentáramos).
    """
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            return
        inspector = inspect(db.engine)
        for table in db.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue
            columnas_reales = {c["name"]: c["type"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                tipo_real = columnas_reales.get(col.name)
                if tipo_real is None:
                    continue  # todavía no existe -- eso lo crea _sync_missing_columns()

                largo_real = getattr(tipo_real, "length", None)
                largo_modelo = getattr(col.type, "length", None)
                if largo_real is None or largo_modelo is None or largo_modelo <= largo_real:
                    continue  # no es VARCHAR de largo fijo, o ya entra tal como está

                col_type = col.type.compile(db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" TYPE {col_type}'))
                print(f"[info] columna agrandada automáticamente: {table.name}.{col.name} ({largo_real} -> {largo_modelo})")


_sync_column_widths()


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

        avisar_registro_y_checkin_al_panel(nuevo)

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
        error = _completar_campos_empresa(nueva, request.form)
        if error:
            lista = current_user.empresas.order_by(Empresa.nombre_interno).all()
            fecha_emision_default = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            return render_template(
                "empresas.html", usuario=current_user, empresas=lista, empresa=None,
                fecha_emision_default=fecha_emision_default, error_formulario=error,
            )
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
        error = _completar_campos_empresa(empresa, request.form)
        if error:
            lista = current_user.empresas.order_by(Empresa.nombre_interno).all()
            dias_atras = empresa.config_dias_atras_fecha_emision or 10
            fecha_emision_default = (datetime.now() - timedelta(days=dias_atras)).strftime("%Y-%m-%d")
            return render_template(
                "empresas.html", usuario=current_user, empresas=lista, empresa=empresa,
                fecha_emision_default=fecha_emision_default, error_formulario=error,
            )
        db.session.commit()
        return redirect(url_for("empresas"))

    lista = current_user.empresas.order_by(Empresa.nombre_interno).all()
    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_emision_default = (datetime.now() - timedelta(days=dias_atras)).strftime("%Y-%m-%d")
    return render_template(
        "empresas.html", usuario=current_user, empresas=lista, empresa=empresa,
        error_drive=request.args.get("error_drive"),
        error_mercadopago=request.args.get("error_mercadopago"),
        fecha_emision_default=fecha_emision_default,
    )


@app.route("/empresas/<int:empresa_id>/eliminar", methods=["POST"])
@login_required
def empresas_eliminar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if empresa:
        db.session.delete(empresa)
        db.session.commit()
    return redirect(url_for("empresas"))


def _validar_cuil(valor):
    """
    El CUIL/CUIT de ARCA tiene que ser 11 dígitos (con o sin guiones) -- se
    coló un caso real donde alguien puso un email en ese campo por error y
    rompió el guardado con un error 500 (la columna solo acepta 20
    caracteres) en vez de un aviso claro, porque nada lo validaba antes de
    mandarlo a la base. Devuelve un mensaje de error, o None si está bien.
    """
    solo_digitos = re.sub(r"\D", "", valor or "")
    if len(solo_digitos) != 11:
        return f'El CUIL/CUIT tiene que tener 11 dígitos (con o sin guiones, ej. 27353876932) -- "{valor}" no es válido.'
    return None


def _completar_campos_empresa(empresa, form):
    """Devuelve un mensaje de error (string) si algo no es válido, o None si
    quedó todo bien. Mientras haya error, el objeto empresa puede quedar con
    cambios a medias en memoria, pero eso no importa -- las rutas que llaman
    a esto no hacen commit si hay error, así que nada se guarda de más."""
    empresa.nombre_interno = form.get("nombre_interno", "").strip()
    empresa.razon_social_arca = form.get("razon_social_arca", "").strip()

    cuil_arca_nuevo = form.get("cuil_arca", "").strip()
    error_cuil = _validar_cuil(cuil_arca_nuevo)
    if error_cuil:
        return error_cuil
    empresa.cuil_arca = cuil_arca_nuevo
    password_nueva = form.get("password_arca", "").strip()
    if password_nueva:  # solo la pisa si escribió algo nuevo (no la borra si la deja vacía)
        empresa.set_password_arca(password_nueva)

    empresa.config_tipo_comprobante = ",".join(form.getlist("config_tipo_comprobante"))
    empresa.tipo_contribuyente = form.get("tipo_contribuyente", "").strip() or "Monotributo"
    empresa.config_alicuota_iva = ",".join(form.getlist("config_alicuota_iva")) or None
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
    empresa.descripciones_alicuotas = form.get("descripciones_alicuotas", "").strip() or None
    lista_descripciones = [d.strip() for d in empresa.descripciones_disponibles.split(",") if d.strip()]
    empresa.config_producto_servicio = lista_descripciones[0] if lista_descripciones else ""
    empresa.config_descripcion_aleatoria = form.get("config_descripcion_aleatoria") == "on"

    return None


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


# ---------- Mercado Pago por empresa ----------

@app.route("/empresas/<int:empresa_id>/mercadopago/conectar")
@login_required
def empresa_mercadopago_conectar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("empresas"))
    if not mercadopago_cliente.esta_configurado():
        return "Mercado Pago todavía no está configurado en este servidor (faltan las variables de entorno MERCADOPAGO_*).", 400
    url, state = mercadopago_cliente.generar_url_autorizacion(empresa.id)
    session["mercadopago_state"] = state
    return redirect(url)


@app.route("/mercadopago/callback")
@login_required
def mercadopago_callback():
    state_recibido = request.args.get("state", "")
    state_guardado = session.pop("mercadopago_state", None)
    # El "state" tiene que ser EXACTAMENTE el que se generó al armar el link
    # de autorización -- si no coincide, alguien está mandando un callback
    # que no salió de acá, y no hay que procesarlo.
    if not state_guardado or state_recibido != state_guardado:
        return redirect(url_for("empresas", error_mercadopago="No se pudo validar el pedido de conexión. Probá de nuevo."))

    empresa_id = state_guardado.split(":", 1)[0]
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("empresas"))

    code = request.args.get("code")
    if not code:
        return redirect(url_for("empresas_editar", empresa_id=empresa.id, error_mercadopago="Mercado Pago no autorizó la conexión."))

    try:
        refresh_token, user_id, email = mercadopago_cliente.procesar_callback(code)
    except Exception as e:
        return redirect(url_for("empresas_editar", empresa_id=empresa.id, error_mercadopago=str(e)))

    empresa.set_mercadopago_token(refresh_token)
    empresa.mercadopago_user_id = user_id
    empresa.mercadopago_email = email
    db.session.commit()
    return redirect(url_for("empresas_editar", empresa_id=empresa.id))


@app.route("/empresas/<int:empresa_id>/mercadopago/desconectar", methods=["POST"])
@login_required
def empresa_mercadopago_desconectar(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if empresa:
        mercadopago_cliente.desconectar(empresa)
        db.session.commit()
    return redirect(url_for("empresas_editar", empresa_id=empresa_id))


@app.route("/empresas/<int:empresa_id>/mercadopago/traer-movimientos", methods=["POST"])
@login_required
def empresa_mercadopago_traer_movimientos(empresa_id):
    """
    Trae los pagos aprobados de Mercado Pago de los últimos N días (30 por
    defecto) y arma un Comprobante "pendiente" por cada uno que todavía no
    se haya traído -- ver procesador.crear_comprobante_desde_pago_mercadopago.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404
    if not empresa.tiene_mercadopago_conectado():
        return jsonify(ok=False, error="Esta empresa no tiene Mercado Pago conectado."), 400

    data = request.get_json(silent=True) or {}
    try:
        dias = max(1, min(int(data.get("dias", 30)), 90))
    except (TypeError, ValueError):
        dias = 30

    ahora = datetime.now()
    fecha_hasta = ahora.strftime("%Y-%m-%dT23:59:59.000-03:00")
    fecha_desde = (ahora - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-03:00")

    try:
        pagos = mercadopago_cliente.buscar_pagos(empresa, fecha_desde, fecha_hasta)
    except Exception as e:
        return jsonify(ok=False, error=f"No se pudo traer los movimientos de Mercado Pago: {e}"), 502

    nuevos = 0
    for pago in pagos:
        comprobante = procesador.crear_comprobante_desde_pago_mercadopago(pago, current_user.id, empresa)
        if comprobante:
            nuevos += 1
    db.session.commit()

    return jsonify(ok=True, encontrados=len(pagos), nuevos=nuevos)


# ---------- Comprobantes (por empresa) ----------

def _parse_fecha_ddmmaaaa(fecha_str):
    """Convierte 'DD/MM/AAAA' a datetime, o None si viene vacía o mal formada."""
    if not fecha_str:
        return None
    try:
        return datetime.strptime(fecha_str, "%d/%m/%Y")
    except ValueError:
        return None


def _ordenar_por_fecha_facturacion(comprobantes, empresa):
    """
    Ordena una lista de Comprobante de más vieja a más nueva -- primero por
    la fecha REAL del comprobante (la de la operación en sí), y a igualdad
    de esa fecha, por la fecha de facturación ajustada (la que realmente se
    va a escribir en ARCA, la misma que calcula calcular_fecha_facturacion)
    como desempate. Así se factura/se muestra primero lo más atrasado según
    cuándo pasó la operación de verdad.

    Ojo: antes se ordenaba al revés (primero por fecha de facturación
    ajustada) -- eso funcionaba mal cuando había una fecha_facturacion_manual
    cargada a mano, porque esa fecha puede no tener nada que ver con la
    fecha real de la operación y desordenaba todo el criterio de "más
    atrasado primero". De paso, esta función deja cargado
    c.fecha_facturacion_ajustada en cada fila (se usa para mostrarla en la
    tabla, no hace falta recalcularla después).

    Las filas sin fecha válida (no debería pasar, pero por las dudas) quedan
    al final, no se pierden.
    """
    for c in comprobantes:
        if c.fecha_facturacion_manual:
            c.fecha_facturacion_ajustada = c.fecha_facturacion_manual
            continue
        c.fecha_facturacion_ajustada = None
        if c.fecha_comprobante:
            try:
                c.fecha_facturacion_ajustada = calcular_fecha_facturacion(
                    c.fecha_comprobante, empresa.config_dias_atras_fecha_emision
                )
            except ValueError:
                pass  # fecha con formato inesperado -- se deja en None, la tabla muestra "—"

    def _clave(c):
        clave_comprobante = _parse_fecha_ddmmaaaa(c.fecha_comprobante) or datetime.max
        clave_facturacion = _parse_fecha_ddmmaaaa(c.fecha_facturacion_ajustada) or datetime.max
        return (clave_comprobante, clave_facturacion)

    return sorted(comprobantes, key=_clave)


@app.route("/empresas/<int:empresa_id>/comprobantes")
@login_required
def comprobantes(empresa_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return redirect(url_for("panel"))

    filas = (
        Comprobante.query.filter_by(empresa_id=empresa.id)
        .all()
    )

    # Se ordena de más vieja a más nueva por fecha REAL del comprobante (y a
    # igualdad, por la fecha de facturación ajustada) -- así en pantalla, y
    # también al facturar todo en lote, se atiende primero lo más atrasado
    # según cuándo pasó la operación de verdad. Esto ya calcula
    # fecha_facturacion_ajustada de paso (ver la función).
    filas = _ordenar_por_fecha_facturacion(filas, empresa)

    for c in filas:
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
                "fecha_no_detectada": bool(c.fecha_no_detectada),
                "fecha_desde": c.fecha_desde, "fecha_hasta": c.fecha_hasta,
                "tipo_documento": c.tipo_documento, "cuit_receptor": c.cuit_receptor,
                "cuit_alternativo": c.cuit_alternativo,
                "medio_pago_detectado": c.medio_pago_detectado,
                "condicion_iva": c.condicion_iva, "condicion_venta": c.condicion_venta,
                "tipo_pago": c.tipo_pago, "tipo_pago_detalle": c.tipo_pago_detalle,
                "numero_pago": c.numero_pago,
                "descripcion": c.descripcion, "cantidad": c.cantidad,
                "unidad_medida": c.unidad_medida, "precio_unitario": c.precio_unitario,
                "nombre_razon_social": c.nombre_razon_social, "id_transaccion": c.id_transaccion,
                "fecha_facturacion_manual": c.fecha_facturacion_manual or c.fecha_facturacion_ajustada,
                "alicuota_iva": c.alicuota_iva,
                "lineas_extra": [
                    {
                        "id": l.id, "descripcion": l.descripcion, "cantidad": l.cantidad,
                        "unidad_medida": l.unidad_medida, "precio_unitario": l.precio_unitario,
                        "alicuota_iva": l.alicuota_iva,
                    }
                    for l in c.lineas_extra
                ],
            },
        }
        for c in filas
    ]

    return render_template(
        "comprobantes.html", comprobantes=filas, usuario=current_user, empresa=empresa,
        datos_revision=datos_revision, stats=_calcular_estadisticas(empresa.id), conceptos=CONCEPTOS,
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
        "errores_detalle": [
            {
                "registro_id": r.id, "nombre_archivo": r.nombre_archivo,
                "tiene_imagen": bool(r.archivo_ruta or r.archivo_drive_id),
            }
            for r in con_error
        ],
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
            # El CUIL/CUIT propio de la empresa (con el que factura en ARCA)
            # se le pasa al lector para que lo descarte si aparece en la
            # imagen -- ahí casi siempre es el emisor (nuestro cliente
            # mandándose a sí mismo el comprobante), no el receptor real. Se
            # limpia a solo dígitos porque cuil_arca puede estar guardado
            # con o sin guiones según cómo lo haya tipeado el usuario, y el
            # lector siempre compara contra dígitos limpios.
            cuit_propio = re.sub(r"\D", "", empresa.cuil_arca or "")
            resultado, comprobante_relacionado, (archivo_ruta_intento, archivo_drive_id_intento) = procesar_archivo(
                ruta_local, archivo.filename, current_user.id, empresa.id, fecha_hoy,
                cuit_propio_cliente=cuit_propio,
            )
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
    "cuit_alternativo": "texto",
    "medio_pago_detectado": "texto",
    "condicion_iva": "texto",
    "condicion_venta": "texto",
    "tipo_pago": "texto",
    "tipo_pago_detalle": "texto",
    "numero_pago": "texto",
    "descripcion": "texto",
    "cantidad": "numero",
    "unidad_medida": "texto",
    "precio_unitario": "numero",
    "nombre_razon_social": "texto",
    "id_transaccion": "texto",
    "fecha_facturacion_manual": "texto",
    "alicuota_iva": "texto",
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

    if campo == "fecha_comprobante":
        # Si el usuario edita la fecha a mano, ya la revisó -- se saca el
        # aviso de "fecha no detectada" aunque el valor que ponga sea
        # parecido al que había (lo importante es que la confirmó él).
        comprobante.fecha_no_detectada = False
        # "Desde" y "Hasta" (el período declarado en ARCA) casi siempre
        # coinciden con la fecha del comprobante -- se actualizan solas.
        comprobante.fecha_desde = valor
        comprobante.fecha_hasta = valor
        # La fecha de facturación también se recalcula con el criterio de
        # siempre (hoy-10 días, salvo que el comprobante sea más reciente),
        # PISANDO cualquier fecha de facturación manual que hubiera antes --
        # si el usuario cambió la fecha del comprobante, lo lógico es que
        # quiera que la facturación se ajuste a la fecha nueva, no que
        # se quede con un valor manual pensado para la fecha vieja. Si hace
        # falta, se puede volver a editar a mano después sin problema.
        try:
            comprobante.fecha_facturacion_manual = calcular_fecha_facturacion(
                valor, empresa.config_dias_atras_fecha_emision
            )
        except ValueError:
            pass  # fecha con formato raro -- se deja la fecha de facturación como estaba

        # Si la empresa factura por defecto en concepto Mixto pero esta
        # fecha nueva cae dentro de los últimos N días, se declara como
        # "Productos" en vez de mixto (ver concepto_efectivo). Se recalcula
        # siempre a partir de la config de la empresa, no del concepto que
        # ya tenía el comprobante -- si el usuario lo había cambiado a mano
        # a otra cosa sin relación con esto, esta regla no debería pisarlo
        # para siempre; se vuelve a partir de la base configurada cada vez.
        comprobante.concepto = concepto_efectivo(
            valor, empresa.config_concepto, empresa.config_dias_atras_fecha_emision
        )

    if campo == "descripcion":
        # Si esta descripción tiene una alícuota propia cargada para la
        # empresa (ej. "Embutidos" -> 10.5%), se aplica sola -- así no hay
        # que acordarse de tocar dos campos cada vez que cambia el producto.
        # Si la descripción no está en la lista de la empresa (o no es
        # Responsable Inscripto), no se toca la alícuota que ya tenía.
        alicuota_de_la_descripcion = empresa.alicuota_para_descripcion(valor)
        if alicuota_de_la_descripcion is not None:
            comprobante.alicuota_iva = alicuota_de_la_descripcion

    db.session.commit()
    return jsonify(
        ok=True, importe_total=comprobante.importe_total,
        fecha_desde=comprobante.fecha_desde, fecha_hasta=comprobante.fecha_hasta,
        fecha_facturacion_manual=comprobante.fecha_facturacion_manual,
        concepto=comprobante.concepto, alicuota_iva=comprobante.alicuota_iva,
    )


CAMPOS_EDITABLES_LINEA = {
    "descripcion": "texto",
    "cantidad": "numero",
    "unidad_medida": "texto",
    "precio_unitario": "numero",
    "alicuota_iva": "texto",
}


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/lineas", methods=["POST"])
@login_required
def comprobante_agregar_linea(empresa_id, comprobante_id):
    """
    Agrega una línea EXTRA de producto/servicio (la 2ª, 3ª, etc. -- la
    primera son los campos de siempre del propio comprobante). Empieza en
    blanco/con defaults mínimos; se completa después editando campo por
    campo, igual que cualquier otro campo de Revisión Manual.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado, no se puede editar."), 400

    siguiente_orden = 2 + len(comprobante.lineas_extra)
    linea = ComprobanteLinea(
        comprobante_id=comprobante.id, orden=siguiente_orden,
        descripcion="", cantidad=1.0, unidad_medida=empresa.config_unidad_medida,
        precio_unitario=0.0, alicuota_iva=None,
    )
    db.session.add(linea)
    comprobante.recalcular_importe()
    db.session.commit()

    return jsonify(
        ok=True, importe_total=comprobante.importe_total,
        linea={
            "id": linea.id, "descripcion": linea.descripcion, "cantidad": linea.cantidad,
            "unidad_medida": linea.unidad_medida, "precio_unitario": linea.precio_unitario,
            "alicuota_iva": linea.alicuota_iva,
        },
    )


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/lineas/<int:linea_id>/editar", methods=["POST"])
@login_required
def comprobante_editar_linea(empresa_id, comprobante_id, linea_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado, no se puede editar."), 400

    linea = ComprobanteLinea.query.filter_by(id=linea_id, comprobante_id=comprobante.id).first()
    if not linea:
        return jsonify(ok=False, error="Esa línea no existe."), 404

    datos = request.get_json(silent=True) or {}
    campo = datos.get("campo")
    valor = datos.get("valor")

    tipo = CAMPOS_EDITABLES_LINEA.get(campo)
    if not tipo:
        return jsonify(ok=False, error=f"El campo '{campo}' no se puede editar."), 400

    if tipo == "numero":
        try:
            valor = float(str(valor).replace(",", "."))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Ese valor no es un número válido."), 400

    setattr(linea, campo, valor)
    if campo in ("cantidad", "precio_unitario"):
        comprobante.recalcular_importe()

    alicuota_de_la_descripcion = None
    if campo == "descripcion":
        # Mismo mecanismo que la descripción principal: si esta descripción
        # tiene una alícuota propia cargada para la empresa, se aplica sola
        # -- si el operador prefiere otra, la puede cambiar después a mano
        # sin problema, esto no la deja bloqueada.
        alicuota_de_la_descripcion = empresa.alicuota_para_descripcion(valor)
        if alicuota_de_la_descripcion is not None:
            linea.alicuota_iva = alicuota_de_la_descripcion

    db.session.commit()
    return jsonify(ok=True, importe_total=comprobante.importe_total, alicuota_iva=linea.alicuota_iva)


@app.route("/empresas/<int:empresa_id>/comprobantes/<int:comprobante_id>/lineas/<int:linea_id>/eliminar", methods=["POST"])
@login_required
def comprobante_eliminar_linea(empresa_id, comprobante_id, linea_id):
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    comprobante = Comprobante.query.filter_by(id=comprobante_id, empresa_id=empresa.id).first()
    if not comprobante:
        return jsonify(ok=False, error="Ese comprobante no existe."), 404
    if comprobante.estado == "facturado":
        return jsonify(ok=False, error="Este comprobante ya fue facturado, no se puede editar."), 400

    linea = ComprobanteLinea.query.filter_by(id=linea_id, comprobante_id=comprobante.id).first()
    if not linea:
        return jsonify(ok=False, error="Esa línea no existe."), 404

    db.session.delete(linea)
    db.session.flush()
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
        if campo == "fecha_comprobante":
            comprobante.fecha_no_detectada = False
            comprobante.fecha_desde = valor
            comprobante.fecha_hasta = valor
            try:
                comprobante.fecha_facturacion_manual = calcular_fecha_facturacion(
                    valor, empresa.config_dias_atras_fecha_emision
                )
            except ValueError:
                pass
            comprobante.concepto = concepto_efectivo(
                valor, empresa.config_concepto, empresa.config_dias_atras_fecha_emision
            )
        if campo == "descripcion":
            alicuota_de_la_descripcion = empresa.alicuota_para_descripcion(valor)
            if alicuota_de_la_descripcion is not None:
                comprobante.alicuota_iva = alicuota_de_la_descripcion

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

    # Mismo motivo que en eliminar-todos/eliminar-facturados: un RegistroSubida
    # puede tener comprobante_id apuntando a este comprobante -- hay que
    # soltar esa referencia antes de borrarlo, o la base rechaza el DELETE
    # por foreign key (en Postgres siempre; en SQLite local puede no notarse).
    RegistroSubida.query.filter_by(comprobante_id=comprobante.id).update(
        {"comprobante_id": None}, synchronize_session=False
    )

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
    ids_comprobantes = [c.id for c in comprobantes]

    # Este botón es un reset total ("eliminar TODOS los archivos"), así que
    # también hay que borrar el historial de RegistroSubida completo -- si no,
    # quedan renglones residuales de subidas viejas (algunos apuntando a un
    # archivo_drive_id de un Drive ya desconectado, que rompe la imagen del
    # comparador de duplicados con un ícono roto en vez de mostrar nada
    # coherente) y las estadísticas de "archivos subidos en total" nunca
    # bajan a cero aunque se haya borrado todo.
    #
    # IMPORTANTE: los RegistroSubida hay que borrarlos ANTES que los
    # Comprobante -- un RegistroSubida puede tener comprobante_id apuntando a
    # uno de estos comprobantes, y esa es una foreign key. Borrar el
    # Comprobante mientras todavía existe un RegistroSubida que lo referencia
    # rompe la integridad referencial (funcionaba "por accidente" en SQLite
    # local porque no siempre chequea foreign keys, pero en Postgres -- como
    # producción, o local apuntando a la base de producción -- siempre las
    # hace cumplir y tira IntegrityError, cortando TODA la operación sin
    # borrar nada y devolviendo una página de error en vez de JSON).
    #
    # El filtro no puede ser SOLO por empresa_id: confirmado con un caso real
    # que un RegistroSubida puede tener comprobante_id apuntando a uno de
    # estos comprobantes con su PROPIO empresa_id desalineado (dato viejo
    # inconsistente) -- si el filtro fuera solo por empresa_id, ese renglón
    # queda afuera, no se borra, y su comprobante_id fantasma sigue
    # rompiendo el DELETE de comprobantes de la misma forma. Por eso se
    # traen por las dos condiciones con OR: todos los de esta empresa, MÁS
    # cualquiera que referencie a uno de estos comprobantes puntuales.
    condiciones = [RegistroSubida.empresa_id == empresa.id]
    if ids_comprobantes:
        condiciones.append(RegistroSubida.comprobante_id.in_(ids_comprobantes))
    registros = RegistroSubida.query.filter(or_(*condiciones)).all()
    for r in registros:
        eliminar_archivo_persistente(empresa, r.archivo_ruta, r.archivo_drive_id)
        db.session.delete(r)

    for c in comprobantes:
        eliminar_archivo_persistente(empresa, c.archivo_ruta, c.archivo_drive_id)
        db.session.delete(c)

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

    # Los RegistroSubida de "duplicado" que apuntaban a alguno de estos
    # comprobantes como "original" quedarían con un comprobante_id fantasma
    # (el ForeignKey no tiene cascade desde este lado) -- se limpia la
    # referencia, no el registro entero, para no perder las estadísticas
    # históricas. El front ya maneja bien un comprobante_id vacío (muestra
    # "No encontré el comprobante original" en vez de romper la imagen).
    #
    # IMPORTANTE: esto tiene que hacerse ANTES de borrar los Comprobante, no
    # después -- mientras el RegistroSubida siga apuntando (comprobante_id)
    # a un Comprobante que se está por borrar, la base rechaza el DELETE por
    # violar la foreign key (pasa siempre en Postgres; en SQLite local puede
    # no notarse porque no siempre la chequea). Si eso pasa, la transacción
    # entera se cancela sin borrar nada y el servidor responde con una
    # página de error en vez de JSON.
    #
    # El filtro es solo por comprobante_id -- NO también por empresa_id.
    # Confirmado con un caso real que un RegistroSubida puede tener
    # comprobante_id apuntando a uno de estos comprobantes con su PROPIO
    # empresa_id desalineado (dato viejo inconsistente); exigir las dos
    # condiciones a la vez dejaba ese renglón afuera y su comprobante_id
    # fantasma seguía rompiendo el DELETE igual. Lo que hay que evitar es
    # la referencia rota, sin importar qué empresa_id tenga guardado.
    if ids_borrados:
        RegistroSubida.query.filter(
            RegistroSubida.comprobante_id.in_(ids_borrados),
        ).update({"comprobante_id": None}, synchronize_session=False)

    for c in comprobantes:
        eliminar_archivo_persistente(empresa, c.archivo_ruta, c.archivo_drive_id)
        db.session.delete(c)

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


@app.route("/empresas/<int:empresa_id>/registros/<int:registro_id>/agregar-manual", methods=["POST"])
@login_required
def registro_agregar_manual(empresa_id, registro_id):
    """
    Carga un comprobante "en blanco" a partir de un archivo que quedó en
    "error de lectura" -- reutiliza la imagen que ya se guardó de ese
    intento (no hace falta volver a subirla) y lo arma con los valores por
    defecto de la empresa, igual que si el OCR lo hubiera podido leer pero
    sin sacar ningún dato real de la imagen. Queda con monto $0 (se marca
    "REVISAR" y no se puede facturar hasta corregirlo) para que se complete
    entero -- monto, fecha, concepto, CUIT, todo -- desde Revisión Manual,
    con el mismo editor que cualquier otro comprobante pendiente.

    El RegistroSubida pasa de "error" a "nuevo" y queda vinculado al
    comprobante recién creado, así desaparece de la lista de errores.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    registro = RegistroSubida.query.filter_by(id=registro_id, empresa_id=empresa.id).first()
    if not registro:
        return jsonify(ok=False, error="No encontramos ese registro."), 404
    if registro.resultado != "error":
        return jsonify(ok=False, error="Este archivo no está en la lista de errores."), 400

    dias_atras = empresa.config_dias_atras_fecha_emision or 10
    fecha_comprobante = (datetime.now() - timedelta(days=dias_atras)).strftime("%d/%m/%Y")

    cantidad = 1.0
    if empresa.config_descripcion_aleatoria:
        opciones_descripcion = [d.strip() for d in (empresa.descripciones_disponibles or "").split(",") if d.strip()]
    else:
        opciones_descripcion = []
    descripcion_elegida = random.choice(opciones_descripcion) if opciones_descripcion else empresa.config_producto_servicio

    comprobante = Comprobante(
        usuario_id=current_user.id,
        empresa_id=empresa.id,
        id_transaccion=None,  # cargado a mano, sin lectura de OCR -- no participa de la detección de duplicados
        punto_venta=empresa.config_punto_venta,
        tipo_comprobante=(empresa.config_tipo_comprobante or "").split(",")[0],
        concepto=concepto_efectivo(fecha_comprobante, empresa.config_concepto, dias_atras),
        alicuota_iva=(empresa.config_alicuota_iva or "").split(",")[0] or None,
        descripcion=descripcion_elegida,
        unidad_medida=empresa.config_unidad_medida,
        precio_unitario=0.0,
        tipo_documento="DNI",
        cuit_receptor="",
        nombre_razon_social="CONSUMIDOR FINAL",
        fecha_comprobante=fecha_comprobante,
        medio_pago_detectado="Transferencia",
        condicion_iva=empresa.config_condicion_iva,
        condicion_venta=(empresa.config_condicion_venta or "").split(",")[0] if empresa.config_condicion_venta else "",
        fecha_desde=fecha_comprobante,
        fecha_hasta=fecha_comprobante,
        importe_total=0.0,
        cantidad=cantidad,
        archivo_origen=registro.nombre_archivo,
        # Reutiliza el mismo archivo que ya se guardó para este intento --
        # no se vuelve a subir ni se duplica en disco/Drive.
        archivo_ruta=registro.archivo_ruta,
        archivo_drive_id=registro.archivo_drive_id,
    )
    db.session.add(comprobante)
    db.session.flush()  # para tener comprobante.id antes de vincularlo

    registro.resultado = "nuevo"
    registro.comprobante = comprobante

    db.session.commit()
    return jsonify(ok=True, comprobante_id=comprobante.id)


@app.route("/empresas/<int:empresa_id>/comprobantes/detener-facturacion", methods=["POST"])
@login_required
def detener_facturacion(empresa_id):
    """
    Pide que un "Facturar todo lo pendiente" en curso se detenga apenas
    termine el comprobante que esté facturando en ese momento -- no corta a
    mitad de uno (eso podría dejarlo a medio facturar en ARCA), pero no
    arranca el siguiente.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404
    _detener_facturacion_solicitado.add(empresa.id)
    return jsonify(ok=True)


@app.route("/empresas/<int:empresa_id>/comprobantes/facturar-todos", methods=["POST"])
@login_required
def facturar_todos(empresa_id):
    """
    Arranca en un hilo de fondo la facturación, uno por uno y en orden, de
    todos los comprobantes pendientes (o que habían fallado antes) de esta
    empresa -- ver _facturar_todos_en_segundo_plano más arriba. Este pedido
    HTTP solo prepara la lista y lanza el hilo; responde al toque, sin
    esperar a que termine nada, así que el usuario puede cerrar la pestaña
    o el navegador entero apenas confirma.

    Si viene un "limite_monto" en el body, el hilo se va a parar de
    facturar apenas el PRÓXIMO comprobante haría que el total acumulado
    supere ese límite -- los que queden después quedan sin tocar.

    El progreso se consulta después con GET a .../facturar-todos/estado.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    with _lock_facturacion_lote:
        estado_actual = _estado_facturacion_lote.get(empresa.id)
        if estado_actual and estado_actual.get("en_curso"):
            return jsonify(ok=False, error="Ya hay una facturación en lote en curso para esta empresa."), 409

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
        .all()
    )
    # Se factura primero lo más atrasado (mismo criterio que en la tabla:
    # fecha de facturación y, a igualdad, fecha del comprobante).
    pendientes = _ordenar_por_fecha_facturacion(pendientes, empresa)
    pendientes_ids = [c.id for c in pendientes]

    if not pendientes_ids:
        return jsonify(ok=False, error="No hay comprobantes pendientes para facturar."), 400

    # Se limpia por las dudas quede pegado en "true" de una corrida anterior
    # que haya terminado sin pasar por el hilo (ej. el servidor se reinició
    # a mitad de camino) -- cada facturación en lote nueva arranca sin la
    # detención ya pedida de antemano.
    _detener_facturacion_solicitado.discard(empresa.id)

    with _lock_facturacion_lote:
        _estado_facturacion_lote[empresa.id] = _estado_inicial_lote(len(pendientes_ids))

    threading.Thread(
        target=_facturar_todos_en_segundo_plano,
        args=(app, empresa.id, pendientes_ids, limite_monto),
        daemon=True,
    ).start()

    return jsonify(ok=True, iniciado=True, total=len(pendientes_ids))


@app.route("/empresas/<int:empresa_id>/comprobantes/facturar-todos/estado")
@login_required
def facturar_todos_estado(empresa_id):
    """
    Progreso del lote de facturación en curso (o del resultado del último
    que corrió) para esta empresa -- lo consulta el frontend con polling
    mientras "en_curso" es true, para mostrar la barra de avance sin
    depender de que el navegador siga conectado al pedido original.
    """
    empresa = current_user.empresas.filter_by(id=empresa_id).first()
    if not empresa:
        return jsonify(ok=False, error="Esa empresa no existe o no te pertenece."), 404

    with _lock_facturacion_lote:
        estado = _estado_facturacion_lote.get(empresa.id)
        estado_copia = dict(estado) if estado else None
        if estado_copia is not None:
            estado_copia["detalle"] = list(estado_copia["detalle"])

    if estado_copia is None:
        # nunca corrió ningún lote para esta empresa desde que el servidor
        # arrancó por última vez
        return jsonify(ok=True, en_curso=False, terminado=False)

    return jsonify(ok=True, **estado_copia)


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


@app.route("/api/interno/registros-nuevos", methods=["GET"])
def registros_nuevos_para_panel():
    """
    Panel de membresías llama ACÁ (con la misma clave compartida de arriba)
    para traer los usuarios que se registraron en Facturea -- pensado para
    que panel-membresías, que casi siempre está apagado/dormido, se ponga
    al día solo en el momento en que alguien lo abre, en vez de depender
    de que Facturea le avise en el instante exacto del registro (que se
    pierde si panel-membresías no está despierto justo entonces -- ver
    avisar_registro_al_panel() más arriba, que sigue existiendo como
    intento inmediato "mejor esfuerzo", pero este endpoint es el que
    garantiza que tarde o temprano se termine poniendo al día).

    Parámetro opcional "desde" (fecha y hora ISO, ej.
    "2026-09-01T00:00:00"): si viene, solo trae los usuarios registrados
    DESPUÉS de esa fecha -- así panel-membresías puede pedir solo lo nuevo
    desde la última vez que se sincronizó, en vez de la lista entera cada
    vez que se abre.
    """
    clave_recibida = request.headers.get("X-Webhook-Secret", "")
    if not PANEL_MEMBRESIAS_SECRET or clave_recibida != PANEL_MEMBRESIAS_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    query = Usuario.query
    desde_str = request.args.get("desde")
    if desde_str:
        try:
            desde = datetime.fromisoformat(desde_str)
            query = query.filter(Usuario.fecha_registro > desde)
        except ValueError:
            return jsonify({"error": "el parámetro 'desde' no es una fecha ISO válida"}), 400

    usuarios = query.order_by(Usuario.fecha_registro.asc()).all()
    return jsonify({
        "ok": True,
        "usuarios": [
            {
                "email": u.email,
                "nombre": u.nombre_razon_social or u.email,
                "fecha_registro": u.fecha_registro.isoformat() if u.fecha_registro else None,
                "fecha_vencimiento": u.fecha_vencimiento.isoformat() if u.fecha_vencimiento else None,
                "dias_prueba": DIAS_PRUEBA_GRATIS,
            }
            for u in usuarios
        ],
    })


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

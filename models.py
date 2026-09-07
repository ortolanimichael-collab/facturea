import os
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet

db = SQLAlchemy()

DIAS_PRUEBA_GRATIS = 15  # al registrarse, arranca con este período antes de que vos le renueves


def _fernet():
    """
    Usa la variable de entorno ENCRYPTION_KEY para cifrar/descifrar la Clave Fiscal
    de los clientes que no tienen certificado WSFE (Grupo B). Si no está configurada,
    usa una clave fija de desarrollo (NUNCA usar esto en producción real).
    """
    clave = os.environ.get("ENCRYPTION_KEY", "z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1z1I=")
    return Fernet(clave.encode())


class Usuario(UserMixin, db.Model):
    """
    Cada cliente de Facturea. También se usa para vos mismo como administrador
    (con es_admin=True).
    """
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    nombre_razon_social = db.Column(db.String(200))
    cuit = db.Column(db.String(20))

    plan = db.Column(db.String(30), default="basico")  # "basico" | "full"
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_vencimiento = db.Column(db.DateTime)
    metodo_pago = db.Column(db.String(150))  # nota libre por ahora: "transferencia", "efectivo", etc.

    es_admin = db.Column(db.Boolean, default=False)
    activo = db.Column(db.Boolean, default=True)  # para suspender manualmente sin borrar la cuenta

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def esta_vencido(self):
        if not self.fecha_vencimiento:
            return True
        return datetime.utcnow() > self.fecha_vencimiento

    @property
    def dias_restantes(self):
        """Días que le quedan de acceso. 0 o negativo si ya venció -- se usa para avisarle en la web."""
        if not self.fecha_vencimiento:
            return 0
        return (self.fecha_vencimiento.date() - datetime.utcnow().date()).days

    @property
    def puede_usar_el_sistema(self):
        """Un admin siempre puede. Un cliente normal, solo si está activo y no vencido."""
        return self.es_admin or (self.activo and not self.esta_vencido)

    @property
    def estado(self):
        if not self.activo:
            return "suspendido"
        if self.esta_vencido:
            return "vencido"
        return "activo"

    def renovar(self, dias=30):
        """Extiende el vencimiento. Si ya venció, cuenta desde hoy; si no, suma sobre lo que le queda."""
        base = self.fecha_vencimiento if (self.fecha_vencimiento and not self.esta_vencido) else datetime.utcnow()
        self.fecha_vencimiento = base + timedelta(days=dias)

    def tiene_configuracion_minima(self):
        """Puede empezar a usar el sistema: tiene al menos una empresa cargada (cada una con su propio acceso a ARCA)."""
        return self.empresas.count() > 0


class Empresa(db.Model):
    """
    Cada cliente/empresa a la que el usuario (contador, secretario, o el
    propio dueño del negocio) factura. Un mismo Usuario puede tener varias,
    cada una con su PROPIO acceso a ARCA -- porque cada empresa/cliente tiene
    su propia Clave Fiscal, no se comparte entre ellas.
    """
    __tablename__ = "empresas"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True)
    usuario = db.relationship("Usuario", backref=db.backref("empresas", lazy="dynamic"))

    nombre_interno = db.Column(db.String(200))  # cómo la identifica el usuario dentro de Facturea (puede ser cualquier cosa)
    razon_social_arca = db.Column(db.String(200))  # el texto EXACTO que aparece en ARCA al elegir "Empresa a representar"

    # "Monotributo" (factura C, sin IVA) o "Responsable Inscripto" (factura
    # A/B, con IVA discriminado). Todavía no hay automatización de ARCA
    # armada para Responsable Inscripto -- facturar_comprobante() lo corta
    # con un aviso claro hasta que se grabe esa pantalla real y se arme el
    # flujo (es una pantalla distinta a la de Monotributo, con más campos).
    tipo_contribuyente = db.Column(db.String(30), default="Monotributo")
    config_alicuota_iva = db.Column(db.String(50))  # solo aplica a Responsable Inscripto -- lista separada por coma (ej. "21,10.5"), la PRIMERA es la que se usa por defecto al cargar un comprobante nuevo

    # --- Acceso a ARCA de ESTA empresa (Grupo B: sin certificado WSFE, factura vía Clave Fiscal) ---
    cuil_arca = db.Column(db.String(20))
    password_arca_cifrada = db.Column(db.LargeBinary)

    config_tipo_comprobante = db.Column(db.String(600))  # lista separada por coma (mismo patrón que config_condicion_venta) -- la PRIMERA es la que se usa por defecto al cargar un comprobante nuevo
    puntos_venta_disponibles = db.Column(db.String(200))
    config_punto_venta = db.Column(db.String(10))
    config_concepto = db.Column(db.String(10))
    config_condicion_iva = db.Column(db.String(80))
    config_tipo_doc_receptor = db.Column(db.String(80))
    config_condicion_venta = db.Column(db.String(300))
    config_producto_servicio = db.Column(db.String(300))  # descripción por defecto (la primera de descripciones_disponibles)
    descripciones_disponibles = db.Column(db.String(600))  # lista separada por comas de todas las descripciones cargadas
    # Solo aplica a Responsable Inscripto -- alícuota de cada descripción de
    # arriba, EN EL MISMO ORDEN Y CANTIDAD que descripciones_disponibles
    # (posición i de una lista corresponde a la posición i de la otra).
    # Ej: descripciones_disponibles="Carne,Embutidos" y
    # descripciones_alicuotas="21,10.5" -> "Carne" es 21%, "Embutidos" 10.5%.
    descripciones_alicuotas = db.Column(db.String(300))
    config_descripcion_aleatoria = db.Column(db.Boolean, default=False)  # si hay varias, elegir una al azar por comprobante en vez de usar siempre la primera
    config_unidad_medida = db.Column(db.String(80))

    # Cuántos días hacia atrás de HOY se usa como fecha de emisión por
    # defecto al facturar (y como respaldo si el lector no pudo leer la
    # fecha real del comprobante en la imagen). Se guarda como un número de
    # días, no como una fecha fija, para que el default se recalcule solo
    # día a día -- si se guardara una fecha absoluta quedaría vieja al toque.
    # Si el comprobante tiene su propia fecha real y esa fecha es POSTERIOR
    # (más reciente) a "hoy - config_dias_atras_fecha_emision", se respeta la
    # fecha real del comprobante en su lugar (ver calcular_fecha_facturacion).
    config_dias_atras_fecha_emision = db.Column(db.Integer, default=10)

    # --- Google Drive de ESTA empresa (opcional) -- si está conectado, los
    # archivos que suba este cliente se guardan en SU PROPIO Drive en vez de
    # ocupar espacio en el servidor de Facturea. ---
    google_drive_token_cifrado = db.Column(db.LargeBinary)  # refresh token de Google, cifrado igual que la Clave Fiscal
    google_drive_email = db.Column(db.String(200))  # solo para mostrar qué cuenta está conectada
    google_drive_carpeta_id = db.Column(db.String(100))  # carpeta "Facturea" creada en el Drive del cliente

    def set_google_drive_token(self, refresh_token):
        self.google_drive_token_cifrado = _fernet().encrypt(refresh_token.encode())

    def get_google_drive_token(self):
        if not self.google_drive_token_cifrado:
            return None
        return _fernet().decrypt(self.google_drive_token_cifrado).decode()

    def tiene_drive_conectado(self):
        return bool(self.google_drive_token_cifrado)

    def set_password_arca(self, password_plana):
        self.password_arca_cifrada = _fernet().encrypt(password_plana.encode())

    def get_password_arca(self):
        if not self.password_arca_cifrada:
            return None
        return _fernet().decrypt(self.password_arca_cifrada).decode()

    def configuracion_completa(self):
        campos = [
            self.razon_social_arca, self.cuil_arca, self.password_arca_cifrada,
            self.config_tipo_comprobante, self.config_punto_venta,
            self.config_concepto, self.config_condicion_iva, self.config_tipo_doc_receptor,
            self.config_condicion_venta, self.config_producto_servicio, self.config_unidad_medida,
        ]
        return all(campos)

    def alicuota_para_descripcion(self, descripcion):
        """
        Busca, entre las descripciones cargadas para esta empresa, la que
        coincide EXACTO con `descripcion` y devuelve la alícuota que se le
        asignó (por posición: descripciones_disponibles[i] <->
        descripciones_alicuotas[i]). None si no hay coincidencia, si la
        empresa no es Responsable Inscripto, o si esa posición no tiene
        alícuota cargada (las dos listas no siempre miden lo mismo si se
        cargó una descripción sin elegirle alícuota).
        """
        if self.tipo_contribuyente != "Responsable Inscripto":
            return None
        if not descripcion or not self.descripciones_disponibles:
            return None

        descripciones = [d.strip() for d in self.descripciones_disponibles.split(",")]
        alicuotas = (self.descripciones_alicuotas or "").split(",")
        try:
            indice = descripciones.index(descripcion.strip())
        except ValueError:
            return None
        if indice >= len(alicuotas):
            return None
        return alicuotas[indice].strip() or None


class Comprobante(db.Model):
    __tablename__ = "comprobantes"

    id = db.Column(db.Integer, primary_key=True)

    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True)
    usuario = db.relationship("Usuario", backref="comprobantes")
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresas.id"), nullable=False, index=True)
    empresa = db.relationship("Empresa", backref=db.backref("comprobantes", cascade="all, delete-orphan"))

    drive_file_id = db.Column(db.String(200), unique=True, nullable=True)
    id_transaccion = db.Column(db.String(120), index=True)

    # --- Se copian de la configuración de la Empresa cuando se crea el
    # comprobante, pero de acá en adelante son propios de ESTA factura --
    # el usuario puede editarlos sin afectar a las demás. ---
    punto_venta = db.Column(db.String(10))
    tipo_comprobante = db.Column(db.String(80))
    concepto = db.Column(db.String(10))
    descripcion = db.Column(db.String(300))
    unidad_medida = db.Column(db.String(80))
    precio_unitario = db.Column(db.Float, default=0.0)

    tipo_documento = db.Column(db.String(30))
    cuit_receptor = db.Column(db.String(20))
    # El OTRO CUIT/CUIL que el lector encontró en la imagen (normalmente el
    # del emisor) -- se ofrece como alternativa en Revisión Manual, por si
    # el lector eligió mal cuál de los dos es el receptor.
    cuit_alternativo = db.Column(db.String(20), nullable=True)
    # Solo se usa (y se muestra) para empresas Responsable Inscripto -- el
    # % de IVA de este comprobante puntual. En Monotributo queda vacío.
    alicuota_iva = db.Column(db.String(10), nullable=True)
    nombre_razon_social = db.Column(db.String(200))
    nombre_remitente = db.Column(db.String(200))
    fecha_comprobante = db.Column(db.String(20))
    # Si el usuario la edita a mano en Revisión Manual, se guarda acá y tiene
    # prioridad sobre la que calcula calcular_fecha_facturacion(). Si queda
    # vacía (caso normal), se sigue calculando sola como siempre.
    fecha_facturacion_manual = db.Column(db.String(20), nullable=True)
    medio_pago_detectado = db.Column(db.String(20), default="Transferencia")  # "Transferencia" | "Débito" | "Crédito" -- lo que el OCR reconoció en la imagen; decide qué rama sigue el bot en ARCA
    condicion_iva = db.Column(db.String(80))
    condicion_venta = db.Column(db.String(80))
    tipo_pago = db.Column(db.String(80))  # ej: "Visa", "Mastercard Débito", "Otra..." -- solo aplica si condicion_venta es una tarjeta
    tipo_pago_detalle = db.Column(db.String(80))  # texto libre cuando tipo_pago es "Otra..." -- ej. "VISA" para Visa Débito, que no es una opción real del desplegable de ARCA (ahí solo existe "Visa Electrón")
    numero_pago = db.Column(db.String(80))  # número de tarjeta que pide ARCA en ese caso
    fecha_desde = db.Column(db.String(20))
    fecha_hasta = db.Column(db.String(20))
    importe_total = db.Column(db.Float, default=0.0)
    cantidad = db.Column(db.Float, default=1.0)  # el lector no la extrae hoy, arranca en 1 por defecto
    archivo_origen = db.Column(db.String(300))
    archivo_ruta = db.Column(db.String(400))  # ruta relativa dentro de la carpeta uploads/ (si se guardó en el servidor)
    archivo_drive_id = db.Column(db.String(100))  # id del archivo en el Drive del cliente (si esa empresa tiene Drive conectado) -- uno de los dos, nunca los dos

    estado = db.Column(db.String(20), default="pendiente")  # pendiente | facturado | error
    error_facturacion = db.Column(db.Text)  # último error, si estado == "error" -- se limpia al facturar bien
    facturado_en = db.Column(db.DateTime)  # cuándo se facturó de verdad -- solo se completa al facturar con éxito
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)

    def recalcular_importe(self):
        total = (self.precio_unitario or 0.0) * (self.cantidad or 1.0)
        for linea in self.lineas_extra:
            total += (linea.precio_unitario or 0.0) * (linea.cantidad or 1.0)
        self.importe_total = total


class ComprobanteLinea(db.Model):
    """
    Línea EXTRA de producto/servicio de un comprobante -- Responsable
    Inscripto puede facturar varios productos con distinta descripción y
    alícuota en un mismo comprobante (ej. "cortes de carne" al 21% y
    "chacinado" al 10.5% juntos, repartiendo entre las dos el monto que
    efectivamente se cobró). La PRIMERA línea sigue siendo los campos de
    siempre en Comprobante (descripcion, cantidad, unidad_medida,
    precio_unitario, alicuota_iva) -- esta tabla solo guarda la 2ª en
    adelante, así ningún comprobante existente ni el lector/procesador
    necesitan cambiar nada.
    """
    __tablename__ = "comprobante_lineas"

    id = db.Column(db.Integer, primary_key=True)
    comprobante_id = db.Column(db.Integer, db.ForeignKey("comprobantes.id"), nullable=False, index=True)
    orden = db.Column(db.Integer, nullable=False, default=2)  # 2, 3, 4... (la 1 es el propio Comprobante)

    descripcion = db.Column(db.String(300))
    cantidad = db.Column(db.Float, default=1.0)
    unidad_medida = db.Column(db.String(80))
    precio_unitario = db.Column(db.Float, default=0.0)  # TOTAL cobrado por esta línea (con IVA incluido, mismo criterio que el de Comprobante)
    alicuota_iva = db.Column(db.String(10))

    comprobante = db.relationship(
        "Comprobante",
        backref=db.backref("lineas_extra", cascade="all, delete-orphan", order_by="ComprobanteLinea.orden"),
    )


class RegistroSubida(db.Model):
    """
    Un renglón por cada archivo que se intentó subir, se haya convertido en
    comprobante o no -- para poder armar estadísticas acumuladas (cuántos
    duplicados, bloqueados, con error, etc.) y saber los nombres de archivo
    de cada caso, cosa que antes se perdía apenas terminaba la subida.
    """
    __tablename__ = "registros_subida"

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresas.id"), nullable=False, index=True)
    empresa = db.relationship("Empresa", backref=db.backref("registros_subida", cascade="all, delete-orphan"))

    comprobante_id = db.Column(db.Integer, db.ForeignKey("comprobantes.id"), nullable=True)
    comprobante = db.relationship("Comprobante")

    nombre_archivo = db.Column(db.String(300))
    extension = db.Column(db.String(10))
    resultado = db.Column(db.String(20))  # "nuevo" | "duplicado" | "ignorado" | "error"
    archivo_ruta = db.Column(db.String(400))  # solo para "duplicado": la imagen del INTENTO, para compararla con la original
    archivo_drive_id = db.Column(db.String(100))  # igual que en Comprobante, si esa empresa tiene Drive conectado
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)


def init_db(app):
    database_url = os.environ.get("DATABASE_URL", "sqlite:///facturea.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    Migrate(app, db)

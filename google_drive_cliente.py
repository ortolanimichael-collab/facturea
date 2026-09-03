"""
Conexión de Google Drive POR EMPRESA (por cliente) -- cada empresa conecta
su PROPIA cuenta de Google, y los archivos que sube quedan guardados en SU
Drive, no en el servidor de Facturea. Usa el permiso "drive.file" de Google,
que solo deja tocar los archivos que la propia app creó -- no puede ver ni
tocar el resto del Drive del cliente.

Requiere 3 variables de entorno (se consiguen en Google Cloud Console):
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GOOGLE_OAUTH_REDIRECT_URI  (tiene que coincidir EXACTO con lo cargado en Google Cloud Console)
"""
import os
import io

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
NOMBRE_CARPETA = "Facturea"


def _client_config():
    return {
        "web": {
            "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _redirect_uri():
    return os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:5000/google-drive/callback")


def esta_configurado():
    """Si no se cargaron las 3 variables de entorno, esta función no está disponible todavía."""
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def _permitir_http_en_localhost():
    """
    OAuth exige HTTPS por norma -- correcto en producción, pero un problema
    real para poder probar en la PC sin certificado. Esto le avisa a la
    librería que no bloquee eso, pero SOLO cuando el redirect configurado
    es localhost/127.0.0.1 -- en producción, con un dominio real, esto no
    se activa nunca, y ahí sí exige HTTPS como corresponde.
    """
    redirect = _redirect_uri()
    if redirect.startswith("http://localhost") or redirect.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def generar_url_autorizacion(empresa_id):
    """
    Arma el link al que hay que mandar al usuario para que autorice el
    acceso. "state" lleva el id de la empresa, para saber a cuál conectar
    cuando Google nos devuelva al callback.

    Devuelve (url, code_verifier) -- Google exige un "code_verifier" (PKCE)
    que se genera acá y tiene que ser EL MISMO cuando se procese el
    callback más adelante. Como son dos pedidos HTTP separados, quien llama
    a esta función tiene que guardar el code_verifier en algún lado (la
    sesión del navegador, por ejemplo) y pasárselo a procesar_callback().
    """
    _permitir_http_en_localhost()
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())
    url, _ = flow.authorization_url(
        access_type="offline",       # para que Google mande un refresh_token, no solo uno temporal
        prompt="consent",            # fuerza a mostrar la pantalla de permisos siempre (si no, a veces Google no manda refresh_token en logins repetidos)
        state=str(empresa_id),
        include_granted_scopes="true",
    )
    return url, flow.code_verifier


def procesar_callback(url_completa_del_callback, code_verifier):
    """
    Se llama desde la ruta /google-drive/callback, con la URL completa que
    mandó el navegador (incluye el código de autorización) y el
    code_verifier que se guardó al armar el link de autorización. Devuelve
    (refresh_token, email_conectado).
    """
    _permitir_http_en_localhost()
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=url_completa_del_callback)
    credenciales = flow.credentials

    servicio_oauth2 = build("oauth2", "v2", credentials=credenciales)
    perfil = servicio_oauth2.userinfo().get().execute()
    email = perfil.get("email", "")

    return credenciales.refresh_token, email


def _credenciales_de(empresa):
    refresh_token = empresa.get_google_drive_token()
    if not refresh_token:
        raise ValueError(f"La empresa '{empresa.nombre_interno}' no tiene Google Drive conectado.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())  # el refresh_token no vence, pero el access_token sí -- se renueva acá
    return creds


def _obtener_o_crear_carpeta(empresa, servicio):
    if empresa.google_drive_carpeta_id:
        return empresa.google_drive_carpeta_id

    resultado = servicio.files().list(
        q=f"name='{NOMBRE_CARPETA}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        spaces="drive", fields="files(id)",
    ).execute()
    encontradas = resultado.get("files", [])
    if encontradas:
        carpeta_id = encontradas[0]["id"]
    else:
        carpeta = servicio.files().create(
            body={"name": NOMBRE_CARPETA, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        carpeta_id = carpeta["id"]

    empresa.google_drive_carpeta_id = carpeta_id
    return carpeta_id


def subir_archivo(empresa, ruta_local, nombre_archivo):
    """Sube un archivo a la carpeta "Facturea" del Drive de esa empresa. Devuelve el id del archivo en Drive."""
    creds = _credenciales_de(empresa)
    servicio = build("drive", "v3", credentials=creds)
    carpeta_id = _obtener_o_crear_carpeta(empresa, servicio)

    media = MediaFileUpload(ruta_local, resumable=False)
    archivo = servicio.files().create(
        body={"name": nombre_archivo, "parents": [carpeta_id]},
        media_body=media,
        fields="id",
    ).execute()
    return archivo["id"]


def descargar_archivo(empresa, drive_file_id):
    """Devuelve (bytes, mimetype) de un archivo guardado en el Drive de esa empresa."""
    creds = _credenciales_de(empresa)
    servicio = build("drive", "v3", credentials=creds)

    metadata = servicio.files().get(fileId=drive_file_id, fields="mimeType").execute()
    mimetype = metadata.get("mimeType", "application/octet-stream")

    buffer = io.BytesIO()
    solicitud = servicio.files().get_media(fileId=drive_file_id)
    downloader = MediaIoBaseDownload(buffer, solicitud)
    terminado = False
    while not terminado:
        _, terminado = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read(), mimetype


def eliminar_archivo(empresa, drive_file_id):
    try:
        creds = _credenciales_de(empresa)
        servicio = build("drive", "v3", credentials=creds)
        servicio.files().delete(fileId=drive_file_id).execute()
    except Exception:
        pass  # si ya no existe o falla, no hace falta cortar el resto de la operación por esto


def desconectar(empresa):
    empresa.google_drive_token_cifrado = None
    empresa.google_drive_email = None
    empresa.google_drive_carpeta_id = None

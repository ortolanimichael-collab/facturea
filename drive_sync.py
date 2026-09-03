import os
import io
import tempfile

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from models import db, Comprobante, RegistroSubida
from procesador import procesar_archivo, EXTENSIONES_VALIDAS

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _ruta_credenciales():
    return os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials/drive_service_account.json")


def get_drive_service():
    ruta = _ruta_credenciales()
    if not os.path.exists(ruta):
        raise FileNotFoundError(
            f"No encuentro la credencial de Google Drive en '{ruta}'. "
            "Revisá la guía de configuración antes de sincronizar."
        )
    creds = service_account.Credentials.from_service_account_file(ruta, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _listar_archivos(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    resultado = service.files().list(q=query, fields="files(id, name, mimeType)", pageSize=100).execute()
    return resultado.get("files", [])


def _descargar_archivo(service, file_id, destino):
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(destino, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        listo = False
        while not listo:
            _, listo = downloader.next_chunk()


def sincronizar_carpeta(folder_id, fecha_interfaz, usuario_id, empresa_id, cuit_propio_cliente=""):
    """
    Herramienta de uso administrativo/pruebas: recorre una carpeta de Drive
    y procesa los archivos nuevos para el usuario y la empresa indicados.
    """
    service = get_drive_service()
    archivos = _listar_archivos(service, folder_id)

    resumen = {"nuevos": 0, "duplicados": 0, "errores": 0, "ignorados": 0}

    for archivo in archivos:
        nombre = archivo["name"]

        ya_existe = Comprobante.query.filter_by(drive_file_id=archivo["id"]).first()
        if ya_existe:
            continue

        with tempfile.TemporaryDirectory() as tmp:
            ruta_local = os.path.join(tmp, nombre)
            try:
                _descargar_archivo(service, archivo["id"], ruta_local)
            except Exception:
                resumen["errores"] += 1
                ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
                db.session.add(RegistroSubida(empresa_id=empresa_id, nombre_archivo=nombre, extension=ext, resultado="error"))
                continue

            resultado, comprobante_relacionado, (archivo_ruta_intento, archivo_drive_id_intento) = procesar_archivo(
                ruta_local, nombre, usuario_id, empresa_id, fecha_interfaz, cuit_propio_cliente,
                drive_file_id=archivo["id"],
            )
        resumen[{"nuevo": "nuevos", "duplicado": "duplicados", "error": "errores", "ignorado": "ignorados"}[resultado]] += 1

        ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
        db.session.add(RegistroSubida(
            empresa_id=empresa_id, nombre_archivo=nombre, extension=ext,
            resultado=resultado, comprobante=comprobante_relacionado,
            archivo_ruta=archivo_ruta_intento, archivo_drive_id=archivo_drive_id_intento,
        ))

    db.session.commit()
    return resumen

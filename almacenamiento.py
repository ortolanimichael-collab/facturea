"""
Guarda una copia permanente de cada comprobante subido, para poder
mostrarla después en la Revisión Manual.

Si la empresa tiene Google Drive conectado, el archivo se sube a SU PROPIO
Drive y no ocupa espacio en el servidor. Si no lo tiene conectado, se guarda
localmente en uploads/empresa_<id>/, como funcionaba antes.
"""
import os
import shutil
import uuid

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


def guardar_archivo_persistente(ruta_origen, nombre_original, empresa):
    """
    Devuelve (archivo_ruta, archivo_drive_id) -- SIEMPRE uno de los dos es
    None y el otro tiene valor, según dónde haya quedado guardado. Si falla
    la subida a Drive (token vencido, sin internet, etc.), cae de respaldo
    al guardado local para no perder el archivo.
    """
    if empresa and empresa.tiene_drive_conectado():
        try:
            from google_drive_cliente import subir_archivo
            drive_id = subir_archivo(empresa, ruta_origen, nombre_original)
            return None, drive_id
        except Exception:
            pass  # sigue de largo y guarda local como respaldo

    carpeta_empresa = os.path.join(UPLOADS_DIR, f"empresa_{empresa.id}")
    os.makedirs(carpeta_empresa, exist_ok=True)

    ext = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else ""
    nombre_final = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    ruta_destino = os.path.join(carpeta_empresa, nombre_final)

    shutil.copyfile(ruta_origen, ruta_destino)
    return f"empresa_{empresa.id}/{nombre_final}", None


def eliminar_archivo_persistente(empresa, archivo_ruta, archivo_drive_id):
    if archivo_drive_id:
        try:
            from google_drive_cliente import eliminar_archivo
            eliminar_archivo(empresa, archivo_drive_id)
        except Exception:
            pass
        return

    if not archivo_ruta:
        return
    ruta_abs = os.path.join(UPLOADS_DIR, archivo_ruta)
    if os.path.exists(ruta_abs):
        try:
            os.remove(ruta_abs)
        except OSError:
            pass


def ruta_absoluta(ruta_relativa):
    return os.path.join(UPLOADS_DIR, ruta_relativa)

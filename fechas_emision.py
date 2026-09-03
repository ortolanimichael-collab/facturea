"""
Toda la lógica de "fecha de emisión por defecto" vive acá, para no tener la
misma regla repetida (y potencialmente desincronizada) en varios archivos.

La regla, siempre la misma en los 3 lugares donde se usa:
    fecha_por_defecto = hoy - <días configurados por la empresa, 10 si no configuró nada>
    se usa esa fecha por defecto, SALVO que la fecha real del comprobante
    sea MÁS RECIENTE -- en ese caso se respeta la real.
"""
from datetime import datetime, timedelta


def dias_configurados(empresa):
    if empresa and empresa.dias_atraso_fecha_emision:
        return empresa.dias_atraso_fecha_emision
    return 10


def fecha_por_defecto_hoy(empresa):
    """La fecha de HOY, con el descuento de días configurado. Objeto datetime."""
    return datetime.now() - timedelta(days=dias_configurados(empresa))


def fecha_por_defecto_str(empresa):
    """Igual que arriba, pero como string DD/MM/AAAA (el formato que usa el resto del sistema)."""
    return fecha_por_defecto_hoy(empresa).strftime("%d/%m/%Y")


def elegir_fecha(empresa, fecha_comprobante_str):
    """
    Compara la fecha por defecto de hoy contra una fecha real de comprobante
    (string DD/MM/AAAA) y devuelve la que corresponda según la regla: gana
    la más reciente de las dos. Si fecha_comprobante_str viene vacío o mal
    formado, devuelve directamente la fecha por defecto.
    """
    fecha_default = fecha_por_defecto_hoy(empresa)
    if not fecha_comprobante_str:
        return fecha_default.strftime("%d/%m/%Y")
    try:
        fecha_real = datetime.strptime(fecha_comprobante_str, "%d/%m/%Y")
    except (ValueError, TypeError):
        return fecha_default.strftime("%d/%m/%Y")

    fecha_elegida = fecha_default if fecha_real < fecha_default else fecha_real
    return fecha_elegida.strftime("%d/%m/%Y")


def actualizar_fechas_pendientes(empresa):
    """
    Recorre los comprobantes PENDIENTES (no facturados) de esta empresa y
    empuja hacia adelante la fecha de emisión de los que quedaron atrás del
    default de HOY -- sin tocar los que ya tienen una fecha más reciente
    que ese default (los deja como están). Se llama cada vez que se entra
    a ver los comprobantes de una empresa, así se mantiene al día sin
    necesitar ninguna tarea programada corriendo en el servidor.

    Devuelve la cantidad de comprobantes que se actualizaron.
    """
    from models import db, Comprobante  # import acá adentro para evitar import circular con models.py

    fecha_default_str = fecha_por_defecto_str(empresa)
    pendientes = Comprobante.query.filter(
        Comprobante.empresa_id == empresa.id,
        Comprobante.estado != "facturado",
    ).all()

    actualizados = 0
    for c in pendientes:
        nueva_fecha = elegir_fecha(empresa, c.fecha_comprobante)
        if nueva_fecha != c.fecha_comprobante:
            c.fecha_comprobante = nueva_fecha
            actualizados += 1

    if actualizados:
        db.session.commit()
    return actualizados

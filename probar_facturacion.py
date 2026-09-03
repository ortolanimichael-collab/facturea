"""
Script de prueba: factura UN comprobante pendiente de una empresa, para
probar el bot en vivo antes de tener el botón "Facturar" armado en la web.

Uso:
    python probar_facturacion.py tu_email@ejemplo.com
    python probar_facturacion.py tu_email@ejemplo.com "Juan Moya (Kiosco)"
    python probar_facturacion.py tu_email@ejemplo.com --prueba
    python probar_facturacion.py tu_email@ejemplo.com "Juan Moya (Kiosco)" --prueba

Si no indicás el nombre interno de la empresa, y el usuario tiene una sola
empresa cargada, se usa esa. Si tiene varias, hay que indicar cuál.

Con --prueba (en cualquier posición), el bot hace TODO el recorrido real
contra ARCA pero se detiene en la pantalla de revisión final SIN confirmar
-- no se emite ninguna factura ni se genera CAE, y el comprobante queda
tal cual estaba (no pasa a "facturado"). Sirve para chequear con tus
propios ojos que todos los campos salen bien antes de facturar de verdad.
"""
import sys

from app import app
from models import db, Comprobante, Usuario
from automatizacion.arca_bot import facturar_comprobante

if __name__ == "__main__":
    argumentos = sys.argv[1:]
    modo_prueba = "--prueba" in argumentos
    argumentos = [a for a in argumentos if a != "--prueba"]

    if len(argumentos) < 1:
        print("Uso: python probar_facturacion.py tu_email@ejemplo.com [\"Nombre interno de la empresa\"] [--prueba]")
        sys.exit(1)

    email = argumentos[0]
    nombre_empresa = argumentos[1] if len(argumentos) > 1 else None

    with app.app_context():
        usuario = Usuario.query.filter_by(email=email).first()
        if not usuario:
            print(f"No existe ningún usuario con el email '{email}'.")
            sys.exit(1)

        empresas = usuario.empresas.all()
        if not empresas:
            print(f"El usuario '{email}' todavía no tiene ninguna empresa cargada.")
            sys.exit(1)

        if nombre_empresa:
            empresa = next((e for e in empresas if e.nombre_interno == nombre_empresa), None)
            if not empresa:
                print(f"No encontré ninguna empresa de '{email}' con nombre interno '{nombre_empresa}'.")
                print("Empresas disponibles: " + ", ".join(e.nombre_interno for e in empresas))
                sys.exit(1)
        elif len(empresas) == 1:
            empresa = empresas[0]
        else:
            print(f"El usuario '{email}' tiene varias empresas cargadas, indicá cuál con el segundo argumento:")
            print("  " + ", ".join(e.nombre_interno for e in empresas))
            sys.exit(1)

        comprobante = Comprobante.query.filter_by(
            empresa_id=empresa.id, estado="pendiente"
        ).first()
        if not comprobante:
            print(f"La empresa '{empresa.nombre_interno}' no tiene ningún comprobante en estado 'pendiente'.")
            sys.exit(1)

        print(f"Empresa: {empresa.nombre_interno} ({empresa.razon_social_arca})")
        print(f"Comprobante #{comprobante.id} — {comprobante.nombre_razon_social} — ${comprobante.importe_total}")
        if modo_prueba:
            print("MODO PRUEBA: va a llegar hasta la pantalla final de ARCA pero NO va a confirmar la")
            print("factura -- no se emite nada real. Se queda 30 segundos en esa pantalla para que la mires.")
        else:
            print("Se va a abrir un navegador. No lo cierres, dejá que termine solo.")
        print()

        try:
            facturar_comprobante(comprobante, modo_prueba=modo_prueba)
        except Exception as e:
            print()
            print(f"Se cortó con un error: {e}")
            sys.exit(1)

        if not modo_prueba:
            comprobante.estado = "facturado"
            comprobante.error_facturacion = None
            db.session.commit()

        print()
        if modo_prueba:
            print("Terminado. Como era modo prueba, NO se emitió ninguna factura y el comprobante")
            print("sigue en estado 'pendiente' -- podés corregir lo que haga falta y probar de nuevo.")
        else:
            print("Terminado. Revisá en ARCA (Mis Comprobantes) si la factura salió bien.")

"""
CLI para ingestar todos los _intermedio_*.xlsx que esten en la raiz del proyecto
a la BD. Tambien permite ingestar uno solo por nombre.

Uso:
    python -m app.cli init                  # crear BD vacia
    python -m app.cli ingestar              # todos los _intermedio_*.xlsx
    python -m app.cli ingestar archivo.xlsx # uno especifico
    python -m app.cli pdf archivo.pdf       # procesa PDF e ingesta
    python -m app.cli stats                 # contar productos/proveedores
"""

import sys
from pathlib import Path

from app.db import get_session_factory, init_db, DB_PATH
from app.ingest import ingestar_xlsx_intermedio
from app.modelos import Proveedor, Producto


PROYECTO_ROOT = Path(__file__).parent.parent
FOTOS_DIR = PROYECTO_ROOT / "data" / "fotos"


def cmd_init():
    init_db()
    print(f"BD inicializada en: {DB_PATH}")


def cmd_ingestar(patron: str | None = None):
    if not DB_PATH.exists():
        init_db()

    SessionFactory = get_session_factory()
    s = SessionFactory()

    if patron:
        archivos = [PROYECTO_ROOT / patron]
    else:
        archivos = sorted(PROYECTO_ROOT.glob("_intermedio_*.xlsx"))

    if not archivos:
        print("No hay _intermedio_*.xlsx para procesar.")
        return

    total_nuevos = 0
    for xlsx in archivos:
        if not xlsx.exists():
            print(f"  No existe: {xlsx}")
            continue
        nombre = xlsx.stem.replace("_intermedio_", "").replace("_", " ")[:60]
        try:
            n = ingestar_xlsx_intermedio(
                session=s,
                xlsx_path=str(xlsx),
                nombre_proveedor=nombre,
                fotos_destino=str(FOTOS_DIR),
            )
            s.commit()
            print(f"  {xlsx.name}: +{n} productos nuevos")
            total_nuevos += n
        except Exception as e:
            s.rollback()
            print(f"  ERROR {xlsx.name}: {e}")

    print(f"\nTotal productos nuevos: {total_nuevos}")
    s.close()


def cmd_pdf(pdf_path: str):
    """Procesa un PDF nuevo y lo ingesta a la BD."""
    import subprocess

    script = PROYECTO_ROOT / "pdf_a_formato_hd.py"
    if not script.exists():
        print(f"No existe el script: {script}")
        return

    result = subprocess.run(
        [sys.executable, str(script), pdf_path],
        cwd=str(PROYECTO_ROOT),
    )
    if result.returncode != 0:
        print("Fallo pdf_a_formato_hd")
        return

    intermedios = sorted(
        PROYECTO_ROOT.glob("_intermedio_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not intermedios:
        print("No se genero _intermedio_*.xlsx")
        return

    cmd_ingestar(intermedios[0].name)


def cmd_stats():
    SessionFactory = get_session_factory()
    s = SessionFactory()
    np = s.query(Proveedor).count()
    nprod = s.query(Producto).count()
    nmarc = s.query(Producto).filter_by(marcado_cotizar=True).count()
    print(f"Proveedores: {np}")
    print(f"Productos: {nprod}")
    print(f"Marcados para cotizar: {nmarc}")
    s.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "ingestar":
        patron = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_ingestar(patron)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "pdf":
        if len(sys.argv) < 3:
            print("Uso: python -m app.cli pdf <archivo.pdf>")
            return
        cmd_pdf(sys.argv[2])
    else:
        print(f"Comando desconocido: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()

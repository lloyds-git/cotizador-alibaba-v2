"""
Post-batch: ingestar todos los _intermedio_*.xlsx generados por dedupe_y_procesar.py
y reclasificar TODOS los productos con app/clasificador.py.

Uso:
    python scripts/ingestar_y_clasificar.py
    python scripts/ingestar_y_clasificar.py --solo-reclasificar  # no toca DB, solo clasifica
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clasificador import clasificar_descripcion
from app.db import get_session_factory, init_db, DB_PATH
from app.ingest import ingestar_xlsx_intermedio
from app.modelos import Producto


ROOT = Path(__file__).resolve().parent.parent
FOTOS_DIR = ROOT / "data" / "fotos"
MANIFEST_PATH = ROOT / "data" / "manifest_archivos.json"


def cargar_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        sys.exit(f"No existe manifest: {MANIFEST_PATH}. Corre primero scripts/dedupe_y_procesar.py inventario")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def ingestar_todos() -> dict:
    """Ingiesta todos los _intermedio_*.xlsx del manifest a la DB.

    Devuelve {procesados, nuevos, errores}.
    """
    if not DB_PATH.exists():
        init_db()

    manifest = cargar_manifest()

    Session = get_session_factory()
    s = Session()

    procesados = 0
    nuevos_total = 0
    errores = []

    for entrada in manifest["entradas"]:
        intermedio_rel = entrada.get("intermedio")
        if not intermedio_rel:
            continue
        intermedio = ROOT / intermedio_rel
        if not intermedio.exists():
            errores.append((intermedio_rel, "intermedio no existe en disco"))
            continue

        # Nombre del proveedor: derivar del canonico (sin prefijo de correo)
        canonico = entrada["canonico"]
        # Quitar prefijo fecha tipo "2026-04-23__..."
        nombre_prov = canonico
        if len(canonico) > 12 and canonico[4] == "-" and canonico[7] == "-":
            # YYYY-MM-DD__Asunto__archivo.ext -> "Asunto archivo"
            partes = canonico.split("__", 1)
            if len(partes) == 2:
                nombre_prov = partes[1]
        # Quitar extension
        nombre_prov = Path(nombre_prov).stem
        # Recortar a 60 chars (cabe en VARCHAR(200) pero queremos mostrarlo bonito)
        nombre_prov = nombre_prov.replace("_", " ")[:80]

        try:
            n_nuevos = ingestar_xlsx_intermedio(
                session=s,
                xlsx_path=str(intermedio),
                nombre_proveedor=nombre_prov,
                fotos_destino=str(FOTOS_DIR),
            )
            s.commit()
            procesados += 1
            nuevos_total += n_nuevos
            print(f"  OK {intermedio.name}: +{n_nuevos} productos nuevos")
        except Exception as e:
            s.rollback()
            errores.append((intermedio_rel, str(e)[:200]))
            print(f"  ERROR {intermedio.name}: {e}")

    s.close()
    return {"procesados": procesados, "nuevos": nuevos_total, "errores": errores}


def reclasificar_todos(forzar: bool = False) -> dict:
    """Recorre todos los productos y asigna categoria via clasificar_descripcion.

    forzar=True: sobreescribe categorias existentes.
    forzar=False: solo asigna a productos con categoria=NULL.
    """
    if not DB_PATH.exists():
        sys.exit(f"No existe DB: {DB_PATH}")

    Session = get_session_factory()
    s = Session()

    cambios = 0
    sin_match = []
    distribucion = {}

    for p in s.query(Producto).all():
        if p.categoria is not None and not forzar:
            distribucion[p.categoria] = distribucion.get(p.categoria, 0) + 1
            continue
        cat = clasificar_descripcion(p.descripcion)
        if cat != p.categoria:
            p.categoria = cat
            cambios += 1
        if cat is None:
            sin_match.append((p.id, (p.descripcion or "")[:60]))
        else:
            distribucion[cat] = distribucion.get(cat, 0) + 1

    s.commit()
    total = s.query(Producto).count()
    s.close()

    return {
        "cambios": cambios,
        "sin_categoria": len(sin_match),
        "total": total,
        "distribucion": distribucion,
        "muestras_sin_match": sin_match[:20],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--solo-reclasificar", action="store_true",
                    help="No ingesta, solo reclasifica productos existentes")
    ap.add_argument("--forzar-reclasificacion", action="store_true",
                    help="Sobreescribe categorias existentes")
    args = ap.parse_args()

    if not args.solo_reclasificar:
        print("=== INGESTANDO INTERMEDIOS ===")
        r = ingestar_todos()
        print(f"\nIntermedios procesados: {r['procesados']}")
        print(f"Productos nuevos totales: {r['nuevos']}")
        if r["errores"]:
            print(f"Errores: {len(r['errores'])}")
            for f, e in r["errores"][:10]:
                print(f"  {f}: {e}")

    print("\n=== RECLASIFICANDO PRODUCTOS ===")
    r = reclasificar_todos(forzar=args.forzar_reclasificacion)
    print(f"Total productos en DB: {r['total']}")
    print(f"Cambios de categoria: {r['cambios']}")
    print(f"Sin categoria: {r['sin_categoria']}")
    print("\nDistribucion por categoria:")
    for cat, n in sorted(r["distribucion"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {cat}")
    if r["muestras_sin_match"]:
        print(f"\nMuestras sin match (max 20):")
        for pid, desc in r["muestras_sin_match"]:
            print(f"  {pid:>4}  {desc.encode('ascii','replace').decode('ascii')}")


if __name__ == "__main__":
    main()

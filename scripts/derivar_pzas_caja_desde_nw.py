"""
Backfill: derivar pzas_caja desde N.W. / peso_unitario para productos
existentes donde pzas_caja esta vacio pero ya tenemos nw_caja_kg.

Caso de uso: tras agregar las columnas nw_caja_kg / gw_caja_kg, este
script recorre los productos en BD y llena pzas_caja con
floor(nw_caja_kg / peso_kg) cuando ambos estan presentes.

Productos sin nw_caja_kg quedan pendientes: requieren re-ingesta del
PDF (para que Claude capture G.W./N.W.) o captura manual en el panel
de detalle.

Tambien recalcula pzas_40hq cuando recien acabamos de llenar pzas_caja
(mismo patron que fix_cbm_y_derivar_piezas.py).

Uso:
    python scripts/derivar_pzas_caja_desde_nw.py             # ejecuta
    python scripts/derivar_pzas_caja_desde_nw.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_factory
from app.ingest import (
    pzas_40hq_desde_cbm_y_caja,
    pzas_caja_desde_nw_y_peso,
)
from app.modelos import Producto


def backfill(dry_run: bool) -> dict:
    Session = get_session_factory()
    s = Session()

    productos = s.query(Producto).all()
    derivados_pzas_caja = 0
    derivados_pzas_40hq = 0
    falta_nw = 0  # tiene pzas_caja=null y no podemos derivar (sin nw_caja_kg)
    sin_cambios = 0

    for p in productos:
        cambios = []

        if (p.pzas_caja is None or p.pzas_caja <= 0):
            derivado = pzas_caja_desde_nw_y_peso(p.nw_caja_kg, p.peso_kg)
            if derivado:
                cambios.append(
                    f"pzas_caja None -> {derivado} "
                    f"(nw={p.nw_caja_kg} kg / peso={p.peso_kg} kg)"
                )
                p.pzas_caja = derivado
                derivados_pzas_caja += 1
            else:
                falta_nw += 1
                # No reportar como cambio: este queda pendiente.

        if (not p.pzas_40hq or p.pzas_40hq <= 0):
            calc = pzas_40hq_desde_cbm_y_caja(p.cbm, p.pzas_caja)
            if calc:
                cambios.append(f"pzas_40hq None -> {calc}")
                p.pzas_40hq = calc
                derivados_pzas_40hq += 1

        if cambios:
            sku_safe = (p.sku or '').encode('ascii', 'replace').decode('ascii')
            print(f"  {sku_safe:<18}  " + " | ".join(cambios))
        else:
            sin_cambios += 1

    if not dry_run:
        s.commit()

    s.close()
    return {
        "total": len(productos),
        "derivados_pzas_caja": derivados_pzas_caja,
        "derivados_pzas_40hq": derivados_pzas_40hq,
        "falta_nw_caja_kg": falta_nw,
        "sin_cambios": sin_cambios,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = backfill(dry_run=args.dry_run)
    print(f"\nTotal productos: {r['total']}")
    print(f"pzas_caja derivados (nw/peso): {r['derivados_pzas_caja']}")
    print(f"pzas_40hq derivados: {r['derivados_pzas_40hq']}")
    print(f"Pendientes (pzas_caja null + sin nw_caja_kg): {r['falta_nw_caja_kg']}")
    print(f"Sin cambios: {r['sin_cambios']}")
    if args.dry_run:
        print("\n(dry-run: no se escribio nada)")


if __name__ == "__main__":
    main()

"""
Dedup de productos por SKU.

Para cada grupo de productos con el mismo SKU, mantiene el de mayor
'completitud' (mas campos no vacios) y borra el resto. Antes de borrar
heredades 'marcado_cotizar=True' y 'notas' del que se va al que queda
(asi no perdemos selecciones del usuario).

Tambien hereda fotos (la primera disponible).

Uso:
    python scripts/dedup_productos.py             # ejecuta
    python scripts/dedup_productos.py --dry-run   # solo reporta
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_factory
from app.modelos import Producto, Foto


CAMPOS_COMPLETITUD = [
    "fob_usd", "material", "medidas", "peso_kg", "color", "moq",
    "packing", "carton_dims", "cbm", "pzas_20ft", "pzas_40hq", "lead_time",
    "categoria",
]


def score_completitud(p: Producto) -> int:
    """Cuenta campos no vacios. Mas alto = mas completo."""
    s = 0
    for campo in CAMPOS_COMPLETITUD:
        v = getattr(p, campo, None)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (int, float)) and v == 0:
            # FOB=0 es un dato valido, no penalizar
            if campo != "fob_usd":
                continue
        s += 1
    # Bonus: tener foto = +2
    if p.fotos:
        s += 2
    return s


def dedupar(dry_run: bool) -> dict:
    Session = get_session_factory()
    s = Session()

    # Encontrar SKUs duplicados
    from sqlalchemy import func
    duplicados = (
        s.query(Producto.sku, func.count(Producto.id).label("n"))
        .filter(Producto.sku.isnot(None))
        .filter(Producto.sku != "")
        .group_by(Producto.sku)
        .having(func.count(Producto.id) > 1)
        .all()
    )

    print(f"SKUs duplicados encontrados: {len(duplicados)}")

    total_borrados = 0
    total_grupos = 0
    for sku, n in duplicados:
        copias = s.query(Producto).filter_by(sku=sku).all()
        if len(copias) < 2:
            continue
        # Ordenar por score desc; en empate, el id mas chico (mas antiguo)
        copias_scored = sorted(copias, key=lambda p: (-score_completitud(p), p.id))
        ganador = copias_scored[0]
        perdedores = copias_scored[1:]

        # Heredar marcado_cotizar (OR) y notas concatenadas
        for p in perdedores:
            if p.marcado_cotizar and not ganador.marcado_cotizar:
                ganador.marcado_cotizar = True
            if p.notas and not ganador.notas:
                ganador.notas = p.notas
            # Heredar fotos si el ganador no tiene
            if not ganador.fotos and p.fotos:
                for f in p.fotos:
                    f.producto_id = ganador.id

        total_grupos += 1
        total_borrados += len(perdedores)

        if not dry_run:
            for p in perdedores:
                s.delete(p)

    if not dry_run:
        s.commit()

    n_total = s.query(Producto).count()
    s.close()

    return {
        "grupos_procesados": total_grupos,
        "productos_borrados": total_borrados,
        "total_en_db": n_total,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    r = dedupar(dry_run=args.dry_run)
    print(f"\nGrupos con duplicados procesados: {r['grupos_procesados']}")
    print(f"Productos {'a borrar' if args.dry_run else 'borrados'}: {r['productos_borrados']}")
    print(f"Total productos en DB ahora: {r['total_en_db']}")


if __name__ == "__main__":
    main()

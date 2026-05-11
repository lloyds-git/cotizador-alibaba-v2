"""
Heredar fotos del SKU base a sus variantes en productos.db.

Caso de uso: PLB002 tiene foto, pero PLB002-10CM, PLB002-L10CM, etc. son
variantes clonadas que se quedaron sin Foto en la BD. Este script:
  1. Agrupa productos por SKU base (PLB002 a partir de PLB002-XXX).
  2. Para cada grupo, identifica un producto "donante" con foto y producto(s)
     "receptor(es)" sin foto.
  3. Crea registros Foto apuntando a la MISMA ruta_relativa que el donante
     (no copia el archivo fisico; las fotos son compartidas entre variantes).

Uso:
    python scripts/heredar_fotos_variantes.py            # dry-run, solo muestra
    python scripts/heredar_fotos_variantes.py --apply    # persiste cambios

Reglas:
  - SKU base = parte alfanumerica antes del primer '-' o '_'. PLB002-10CM -> PLB002.
  - Solo se hereda si la ruta_relativa del donante existe en disco (base_fotos = data/).
  - Si el receptor ya tiene al menos una foto, no se toca.
  - Si hay multiples donantes en el grupo, gana el de menor id (mas viejo).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

# Permitir ejecutar el script desde la raiz del proyecto
PROYECTO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROYECTO))

from app.db import get_session_factory  # noqa: E402
from app.modelos import Producto, Foto  # noqa: E402


BASE_FOTOS = PROYECTO / "data"


# SKUs autogenerados (ej. AUTO-04989f955c) NO son variantes entre si: son
# productos sin SKU real con un hash asignado. Saltar estos prefijos.
PREFIJOS_AUTOGENERADOS = {"AUTO", "GEN", "HASH"}

# Sufijo "razonable" para considerar variantes: corto y debe contener al
# menos una letra (asi descartamos sufijos puramente numericos tipo
# 'YS-10001' que son SKUs secuenciales, no variantes). Acepta:
#   PLB002-10CM, PLB002-L, PLB002-STAINLESS, PB028-3S, PB048-1 (NO -> rechazado)
# Rechaza:
#   YS-10001 (sufijo solo numerico)
#   PLB002-A1B2C3D4E5F (demasiado largo)
SUFIJO_VARIANTE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{1,10}$")


def sku_base(sku: str | None) -> tuple[str, str] | None:
    """Extrae (base, sufijo) recortando despues del primer '-' o '_'.

    Devuelve None si no hay sufijo (no es variante) o si el prefijo esta en
    la lista de autogenerados o el sufijo no parece tag de variante.
    """
    if not sku:
        return None
    s = sku.strip().upper()
    m = re.match(r"^([A-Z0-9]+)[-_](.+)$", s)
    if not m:
        return None
    base, sufijo = m.group(1), m.group(2)
    if base in PREFIJOS_AUTOGENERADOS:
        return None
    if not SUFIJO_VARIANTE_RE.match(sufijo):
        return None
    return base, sufijo


def main(apply: bool) -> int:
    session = get_session_factory()()
    productos = session.query(Producto).all()
    print(f"Total productos: {len(productos)}")

    # Indexar productos por SKU exacto (uppercase) y por SKU base (variantes)
    por_sku_exacto: dict[str, Producto] = {}
    variantes_por_base: dict[str, list[Producto]] = defaultdict(list)
    for p in productos:
        if not p.sku:
            continue
        s = p.sku.strip().upper()
        por_sku_exacto[s] = p
        base_sufijo = sku_base(p.sku)
        if base_sufijo:
            base, _ = base_sufijo
            variantes_por_base[base].append(p)

    print(f"SKUs base con variantes: {len(variantes_por_base)}")

    creadas = 0
    grupos_afectados = 0
    para_aplicar: list[tuple[Producto, str]] = []  # (receptor, ruta_relativa)

    for base, variantes in variantes_por_base.items():
        # Candidatos a donante: el producto con SKU exacto = base, mas las
        # propias variantes que SI tienen foto valida.
        candidatos: list[Producto] = []
        donante_base = por_sku_exacto.get(base)
        if donante_base:
            candidatos.append(donante_base)
        candidatos.extend(variantes)

        # Donante = el de menor id con foto cuya ruta exista en disco
        donante: Producto | None = None
        ruta_donante: str | None = None
        for p in sorted(set(candidatos), key=lambda x: x.id):
            for f in p.fotos:
                if (BASE_FOTOS / f.ruta_relativa).exists():
                    donante = p
                    ruta_donante = f.ruta_relativa
                    break
            if donante:
                break

        if donante is None:
            continue

        # Receptores: variantes sin foto propia (no incluir al donante)
        receptores = [
            p for p in variantes
            if not p.fotos and p.id != donante.id
        ]
        if not receptores:
            continue

        grupos_afectados += 1
        print(f"\n[{base}] donante id={donante.id} sku={donante.sku!r}"
              f" foto={ruta_donante}")
        for r in receptores:
            print(f"  -> heredar a id={r.id} sku={r.sku!r}")
            para_aplicar.append((r, ruta_donante))
            creadas += 1

    print(f"\nResumen: {creadas} fotos a heredar en {grupos_afectados} grupos.")

    if not para_aplicar:
        return 0

    if not apply:
        print("\n(dry-run) Vuelve a correr con --apply para persistir.")
        return 0

    for receptor, ruta in para_aplicar:
        session.add(Foto(
            producto_id=receptor.id,
            ruta_relativa=ruta,
            es_principal=True,
        ))
    session.commit()
    print(f"\nOK: {creadas} registros Foto creados.")
    return 0


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    sys.exit(main(apply))

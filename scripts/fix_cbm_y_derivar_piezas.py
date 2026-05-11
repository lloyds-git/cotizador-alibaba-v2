"""
Fix CBM mal capturado + derivar pzas_40hq.

Problema detectado: Claude Haiku a veces lee mal el CBM del PDF
(typicamente lo infla 10x, lee '0.025' como '0.25'). El dato confiable
es 'carton_dims' = '51*39*12.5 cm' del cual se puede calcular CBM real.

Tambien deriva pzas_40hq desde pzas/caja (extraido de moq='1 pc/box' u
otros patrones) + CBM corregido.

Reglas:
  1. CBM_calc = L * W * H / 1_000_000 desde carton_dims.
  2. Si difiere de cbm DB por mas de 50%, sobrescribe con CBM_calc.
  3. Extrae pzas/caja de moq con regex (ej. '1 pc/box', '90 pcs/carton').
  4. pzas_40hq = floor(67 / CBM_caja) * pzas_caja.

Uso:
    python scripts/fix_cbm_y_derivar_piezas.py             # ejecuta
    python scripts/fix_cbm_y_derivar_piezas.py --dry-run
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_factory
from app.modelos import Producto


CBM_40HQ = 67  # m3 utiles

RE_CARTON = re.compile(r"(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)")
RE_PZAS_CAJA = re.compile(
    r"(\d+)\s*(?:pcs?|pieces?|unidades?|set|pza|pzs?)\s*[/\\]\s*(?:carton|box|ctn|caja)",
    re.IGNORECASE,
)
# fallback: '1 pc/box', '1 set/box' aceptamos '1 X/Y'
RE_PZAS_SIMPLE = re.compile(r"(\d+)\s*(?:pc|pcs|set|pza)\s*[/\\]\s*(?:box|carton|ctn|caja)", re.IGNORECASE)


def parsear_cbm_desde_carton(carton_dims: str | None) -> float | None:
    if not carton_dims:
        return None
    m = RE_CARTON.search(carton_dims)
    if not m:
        return None
    try:
        L, W, H = (float(g) for g in m.groups())
    except ValueError:
        return None
    if L <= 0 or W <= 0 or H <= 0:
        return None
    return round(L * W * H / 1_000_000, 4)


def parsear_pzas_caja(moq: str | None, packing: str | None = None) -> int | None:
    """Busca patrones tipo '1 pc/box', '90 pcs/carton' en moq + packing."""
    for texto in (moq, packing):
        if not texto:
            continue
        m = RE_PZAS_CAJA.search(texto)
        if m:
            try:
                n = int(m.group(1))
                if n > 0:
                    return n
            except ValueError:
                pass
        m = RE_PZAS_SIMPLE.search(texto)
        if m:
            try:
                n = int(m.group(1))
                if n > 0:
                    return n
            except ValueError:
                pass
    return None


def fix(dry_run: bool) -> dict:
    Session = get_session_factory()
    s = Session()

    productos = s.query(Producto).all()
    fixes_cbm = 0
    fixes_pzas = 0
    sin_cambios = 0

    for p in productos:
        cambios = []

        # CBM: recalcular desde carton_dims si difiere >50%
        cbm_calc = parsear_cbm_desde_carton(p.carton_dims)
        if cbm_calc is not None and cbm_calc > 0:
            cbm_db = p.cbm or 0
            if cbm_db <= 0:
                cambios.append(f"cbm None -> {cbm_calc}")
                p.cbm = cbm_calc
                fixes_cbm += 1
            else:
                ratio = cbm_db / cbm_calc
                # 50% para arriba o para abajo
                if ratio > 1.5 or ratio < 0.67:
                    cambios.append(f"cbm {cbm_db} -> {cbm_calc} (ratio {ratio:.1f}x)")
                    p.cbm = cbm_calc
                    fixes_cbm += 1

        # Derivar pzas_40hq si esta vacio y tenemos CBM + pzas/caja
        if not p.pzas_40hq or p.pzas_40hq <= 0:
            pzas_caja = parsear_pzas_caja(p.moq, p.packing)
            cbm_actual = p.cbm or 0
            if pzas_caja and cbm_actual > 0:
                cajas_40hq = math.floor(CBM_40HQ / cbm_actual)
                derivado = cajas_40hq * pzas_caja
                if derivado > 0:
                    cambios.append(f"pzas_40hq None -> {derivado} ({cajas_40hq} cajas x {pzas_caja} pcs)")
                    p.pzas_40hq = derivado
                    fixes_pzas += 1

        if cambios and dry_run:
            sku_safe = (p.sku or '').encode('ascii', 'replace').decode('ascii')
            print(f"  {sku_safe:<18}  " + " | ".join(cambios))
        elif not cambios:
            sin_cambios += 1

    if not dry_run:
        s.commit()

    s.close()
    return {
        "fixes_cbm": fixes_cbm,
        "fixes_pzas_40hq": fixes_pzas,
        "sin_cambios": sin_cambios,
        "total": len(productos),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = fix(dry_run=args.dry_run)
    print(f"\nTotal productos: {r['total']}")
    print(f"CBM corregidos: {r['fixes_cbm']}")
    print(f"pzas_40hq derivados: {r['fixes_pzas_40hq']}")
    print(f"Sin cambios: {r['sin_cambios']}")
    if args.dry_run:
        print("\n(dry-run: no se escribio nada)")


if __name__ == "__main__":
    main()

"""
Genera un formato HD por cada categoria con productos en la DB.

Para cada categoria distinta:
  1. Marca todos los productos de la categoria como cotizar.
  2. Genera _intermedio_<categoria>-YYYYMMDD.xlsx
  3. Corre llenar_formato_hd.py con mapeo de 15 columnas
  4. Salida: formato-hd-<categoria>-YYYYMMDD.xlsx

Uso:
    python scripts/generar_hd_por_categoria.py
    python scripts/generar_hd_por_categoria.py --solo casa-jaula,alimentadores
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_factory
from app.exportar import generar_formato_hd_por_categoria
from app.modelos import Producto


ROOT = Path(__file__).resolve().parent.parent


def generar_para_categoria(session, categoria: str | None) -> dict:
    """Genera intermedio y HD para una categoria, sin tocar marcas existentes."""
    # Contar productos en esta categoria
    if categoria is None:
        n = session.query(Producto).filter(Producto.categoria.is_(None)).count()
    else:
        n = session.query(Producto).filter(Producto.categoria == categoria).count()

    if n == 0:
        return {"categoria": categoria, "n": 0, "intermedio": None, "hd": None, "error": "vacio"}

    slug = categoria if categoria else "sin-categoria"
    fecha = date.today().strftime("%Y%m%d")
    xlsx_int = ROOT / f"_intermedio_{slug}-{fecha}.xlsx"
    hd_out = ROOT / f"formato-hd-{slug}-{fecha}.xlsx"

    # Borrar HD previo para evitar prompt interactivo
    if hd_out.exists():
        try:
            hd_out.unlink()
        except Exception:
            pass

    n_exp = generar_formato_hd_por_categoria(
        session=session,
        xlsx_intermedio=str(xlsx_int),
        base_fotos=str(ROOT / "data"),
        categoria=categoria,
    )

    script = ROOT / "llenar_formato_hd.py"
    formato = ROOT / "Formato HD-Mascotas.xlsb"
    cmd = [
        sys.executable, str(script), str(xlsx_int), str(formato),
        # Col C (Descripcion) -> fila 8 (DESCRIPTION del HD)
        # Col O (FOB USD)     -> fila 11 (DOMESTIC COST del HD)
        "--mapeo", "C=8,O=11", "--yes",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=300)

    if not hd_out.exists():
        # llenar_formato_hd a veces guarda con otro nombre
        candidatos = list(ROOT.glob(f"formato-hd-_intermedio_{slug}-{fecha}.xlsx"))
        if candidatos:
            candidatos[0].rename(hd_out)

    if not hd_out.exists():
        return {"categoria": categoria, "n": n_exp, "intermedio": xlsx_int.name,
                "hd": None, "error": (res.stderr or res.stdout)[-300:]}

    return {"categoria": categoria, "n": n_exp, "intermedio": xlsx_int.name,
            "hd": hd_out.name, "error": None}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--solo", help="Lista comma-separated de categorias a procesar")
    ap.add_argument("--incluir-sin-categoria", action="store_true",
                    help="Generar tambien HD para productos sin categoria")
    ap.add_argument("--incluir-descartar", action="store_true",
                    help="Generar HD para categoria _descartar (fragmentos basura)")
    args = ap.parse_args()

    Session = get_session_factory()
    s = Session()

    # Listar categorias presentes
    cats = [c for (c,) in s.query(Producto.categoria).distinct().all()]
    if args.solo:
        deseadas = {c.strip() for c in args.solo.split(",")}
        cats = [c for c in cats if c in deseadas]

    # Decidir si incluir None
    if None in cats and not args.incluir_sin_categoria:
        cats = [c for c in cats if c is not None]

    # Excluir _descartar por defecto
    if not args.incluir_descartar:
        cats = [c for c in cats if c != "_descartar"]

    cats.sort(key=lambda c: (c is None, c or ""))

    print(f"Generando HDs para {len(cats)} categorias: {cats}")
    print()

    resultados = []
    for c in cats:
        r = generar_para_categoria(s, c)
        resultados.append(r)
        nombre = (c or "(sin categoria)")
        if r["hd"]:
            print(f"  OK  {nombre}: {r['n']} productos -> {r['hd']}")
        else:
            print(f"  ERR {nombre}: {r['error']}")

    s.close()

    print()
    print("=== RESUMEN ===")
    total_n = sum(r["n"] for r in resultados)
    ok = sum(1 for r in resultados if r["hd"])
    err = sum(1 for r in resultados if r["error"] and r["error"] != "vacio")
    vacios = sum(1 for r in resultados if r.get("error") == "vacio")
    print(f"  HDs generados: {ok}/{len(resultados)}")
    print(f"  Productos exportados (total): {total_n}")
    print(f"  Errores: {err}")
    print(f"  Categorias vacias: {vacios}")


if __name__ == "__main__":
    main()

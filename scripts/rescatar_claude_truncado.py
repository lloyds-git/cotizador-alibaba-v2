"""
Rescata productos de respuestas truncadas de Claude (Haiku).

Cuando Claude se queda sin tokens de salida, el JSON queda incompleto y
pdf_a_formato_hd.py cae al parser heuristico, perdiendo TODOS los campos
ricos (material, medidas, CBM, MOQ, carton, peso, color).

Este script:
  1. Encuentra todos los _adobe_extract_*/\_claude_response_debug.txt
  2. Parsea el JSON parcial cerrando balance de llaves/corchetes
  3. Reconstruye un intermedio _intermedio_<base>.xlsx con la estructura
     completa de 15 columnas (igual que construir_xlsx_desde_claude)
  4. Re-ingesta a productos.db (idempotente: actualiza por SKU)

Uso:
    python scripts/rescatar_claude_truncado.py             # rescata todos
    python scripts/rescatar_claude_truncado.py --dry-run   # solo reporta
    python scripts/rescatar_claude_truncado.py --solo PETLEAD  # filtro
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_factory, init_db, DB_PATH
from app.ingest import ingestar_xlsx_intermedio


ROOT = Path(__file__).resolve().parent.parent
FOTOS_DIR = ROOT / "data" / "fotos"


def reparar_json_truncado(texto: str) -> dict:
    """Intenta parsear un JSON posiblemente truncado de Claude.

    Estrategia:
      1. Quita codeblock markers.
      2. Si parsea limpio, devuelve.
      3. Si esta truncado: encuentra el ultimo } que cierra un PRODUCTO
         (objeto dentro del array productos) usando depth tracking.
         Despues de "productos: [" la depth interna del array es 1; cada {
         objeto sube a 2; al cerrar un producto vuelve a 1.
         Truncamos al final del ultimo producto cerrado y agregamos "]}"
         para cerrar el array productos y el objeto raiz.
    """
    s = texto.strip()
    m = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", s, re.DOTALL)
    if m:
        s = m.group(1).strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Encontrar inicio del array productos
    m_array = re.search(r'"productos"\s*:\s*\[', s)
    if not m_array:
        raise ValueError("No se encontro array 'productos'")
    array_start = m_array.end()  # apunta justo despues del '['

    # Recorrer desde ahi y marcar la posicion del ultimo } que cierre un
    # producto completo (depth dentro del array vuelve a 0).
    last_close = -1
    depth = 0  # 0 = dentro del array, 1+ = dentro de un objeto producto
    in_string = False
    escape = False
    for i in range(array_start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_close = i
        elif ch == "]" and depth == 0:
            # Array ya cerrado limpio (raro si llegamos aqui por truncamiento)
            break

    if last_close < 0:
        raise ValueError("No se encontro ningun producto cerrado")

    s_reparado = s[: last_close + 1] + "\n  ]\n}"
    return json.loads(s_reparado)


def reconstruir_intermedio(productos: list[dict], carpeta_extract: str, out_xlsx: Path) -> int:
    """Wrapper sobre construir_xlsx_desde_claude del script principal."""
    sys.path.insert(0, str(ROOT))
    from pdf_a_formato_hd import construir_xlsx_desde_claude
    return construir_xlsx_desde_claude(productos, carpeta_extract, str(out_xlsx))


def derivar_base_de_carpeta(carpeta: Path) -> str:
    """De '_adobe_extract_XXXX' devuelve 'XXXX' para usar como nombre."""
    return carpeta.name.replace("_adobe_extract_", "")


def derivar_nombre_proveedor(carpeta: Path) -> str:
    """De '_adobe_extract_2026-04-22_Fwd_..._Quotation' devuelve algo legible."""
    base = derivar_base_de_carpeta(carpeta)
    return base.replace("_", " ")[:80]


def encontrar_truncados() -> list[Path]:
    """Lista todas las carpetas adobe_extract con respuesta truncada."""
    out = []
    for carpeta in ROOT.glob("_adobe_extract_*"):
        if not carpeta.is_dir():
            continue
        debug = carpeta / "_claude_response_debug.txt"
        if debug.exists():
            out.append(carpeta)
    return sorted(out)


def procesar_uno(carpeta: Path, dry_run: bool, session) -> dict:
    """Rescata productos de una carpeta y opcionalmente ingesta."""
    debug_path = carpeta / "_claude_response_debug.txt"
    texto = debug_path.read_text(encoding="utf-8")

    try:
        parsed = reparar_json_truncado(texto)
    except Exception as e:
        return {"carpeta": carpeta.name, "error": f"parse: {e}", "n_productos": 0}

    productos = parsed.get("productos", [])
    if not productos:
        return {"carpeta": carpeta.name, "error": "0 productos en JSON", "n_productos": 0}

    base = derivar_base_de_carpeta(carpeta)
    out_xlsx = ROOT / f"_intermedio_{base}.xlsx"

    # Si el destino esta lockeado por Excel (o algo similar), escribimos a
    # un nombre alternativo .rescate.xlsx para no fallar.
    target = out_xlsx
    if out_xlsx.exists():
        try:
            # Sondeo: abrir append para verificar permisos
            with out_xlsx.open("ab") as _:
                pass
        except PermissionError:
            target = out_xlsx.with_suffix(".rescate.xlsx")

    n_escritos = reconstruir_intermedio(productos, str(carpeta), target)

    if target != out_xlsx:
        # Intentar reemplazar el original
        try:
            target.replace(out_xlsx)
            target = out_xlsx
        except PermissionError:
            pass  # se queda como .rescate.xlsx

    if dry_run:
        return {
            "carpeta": carpeta.name,
            "n_productos": len(productos),
            "n_escritos": n_escritos,
            "intermedio": target.name,
            "dry_run": True,
        }

    # Ingestar desde donde haya quedado el xlsx
    nombre_prov = derivar_nombre_proveedor(carpeta)
    try:
        n_nuevos = ingestar_xlsx_intermedio(
            session=session,
            xlsx_path=str(target),
            nombre_proveedor=nombre_prov,
            fotos_destino=str(FOTOS_DIR),
        )
        session.commit()
        return {
            "carpeta": carpeta.name,
            "n_productos": len(productos),
            "n_escritos": n_escritos,
            "n_nuevos_en_db": n_nuevos,
            "intermedio": target.name,
        }
    except Exception as e:
        session.rollback()
        return {
            "carpeta": carpeta.name,
            "n_productos": len(productos),
            "n_escritos": n_escritos,
            "error": f"ingest: {str(e)[:200]}",
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="No ingesta, solo reporta")
    ap.add_argument("--solo", help="Filtro substring sobre nombre de carpeta")
    args = ap.parse_args()

    carpetas = encontrar_truncados()
    if args.solo:
        carpetas = [c for c in carpetas if args.solo in c.name]

    if not carpetas:
        print("Sin respuestas truncadas para rescatar.")
        return

    print(f"Rescatando {len(carpetas)} respuestas truncadas:")
    for c in carpetas:
        print(f"  - {c.name}")
    print()

    if not DB_PATH.exists():
        init_db()
    Session = get_session_factory()
    session = Session() if not args.dry_run else None

    resultados = []
    for c in carpetas:
        print(f"\n=== {c.name} ===")
        r = procesar_uno(c, dry_run=args.dry_run, session=session)
        resultados.append(r)
        if r.get("error"):
            print(f"  ERROR: {r['error']}")
        else:
            extra = f" nuevos_db={r.get('n_nuevos_en_db', '?')}" if not args.dry_run else " [DRY RUN]"
            print(f"  productos parseados: {r['n_productos']}  escritos: {r['n_escritos']}{extra}")

    if session:
        session.close()

    print("\n=== RESUMEN ===")
    total_prods = sum(r.get("n_productos", 0) for r in resultados)
    total_nuevos = sum(r.get("n_nuevos_en_db", 0) for r in resultados)
    n_errores = sum(1 for r in resultados if r.get("error"))
    print(f"  Carpetas: {len(resultados)}  Errores: {n_errores}")
    print(f"  Productos parseados: {total_prods}")
    if not args.dry_run:
        print(f"  Productos NUEVOS en DB: {total_nuevos}")
        print(f"  (productos existentes se actualizaron con los datos rescatados)")


if __name__ == "__main__":
    main()

"""
Pipeline batch para procesar TODOS los xlsx/xls/pdf de la raiz del proyecto.

Pasos:
1. Hash MD5 de cada archivo (xlsx/xls/pdf en la raiz, excluyendo _intermedio_,
   formato-hd-, ~$, _adobe_extract_).
2. Detecta duplicados por hash y elige el "canonico" (nombre alfabeticamente
   menor) para cada grupo.
3. Escribe data/manifest_archivos.json con: hash, archivo_canonico, miembros,
   estado_procesamiento (pending/done/error), tipo, intermedio_generado.
4. Devuelve la lista de archivos canonicos pendientes de procesar.

Uso:
    python scripts/dedupe_y_procesar.py inventario   # solo lista
    python scripts/dedupe_y_procesar.py procesar     # corre el pipeline
    python scripts/dedupe_y_procesar.py procesar --solo-cache  # no llama Adobe
    python scripts/dedupe_y_procesar.py procesar --max 3       # smoke test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_PREFIX = ("_intermedio_", "formato-hd-", "~$", "_adobe_extract_")
VALID_EXT = (".xlsx", ".xls", ".pdf")
MANIFEST_PATH = ROOT / "data" / "manifest_archivos.json"


def listar_archivos_origen() -> list[Path]:
    out = []
    for f in ROOT.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith(EXCLUDE_PREFIX):
            continue
        if f.suffix.lower() not in VALID_EXT:
            continue
        out.append(f)
    return sorted(out)


def hash_archivo(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def construir_manifest() -> dict:
    """Construye manifest desde scratch. Si ya existe uno previo, preserva
    el estado de procesamiento de cada hash (para idempotencia)."""
    previo = {}
    if MANIFEST_PATH.exists():
        try:
            previo = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            previo = {}
    previo_por_hash = {e["hash"]: e for e in previo.get("entradas", [])}

    archivos = listar_archivos_origen()
    grupos: dict[str, list[Path]] = {}
    for p in archivos:
        h = hash_archivo(p)
        grupos.setdefault(h, []).append(p)

    entradas = []
    for h, miembros in grupos.items():
        miembros_sorted = sorted(miembros, key=lambda p: p.name)
        canonico = miembros_sorted[0]
        prev = previo_por_hash.get(h, {})
        entrada = {
            "hash": h,
            "canonico": canonico.name,
            "miembros": [p.name for p in miembros_sorted],
            "tipo": canonico.suffix.lower(),
            "estado": prev.get("estado", "pending"),
            "intermedio": prev.get("intermedio"),
            "error": prev.get("error"),
            "tiene_adobe_cache": prev.get("tiene_adobe_cache", False),
        }
        # Recalcular si hay adobe cache (para PDFs)
        if entrada["tipo"] == ".pdf":
            base = canonico.stem
            import re as _re
            base_corto = _re.sub(r"[^\w\-]+", "_", base)
            base_corto = _re.sub(r"_+", "_", base_corto).strip("_")[:60].rstrip("_")
            cache_dir = ROOT / f"_adobe_extract_{base_corto}"
            entrada["tiene_adobe_cache"] = (cache_dir / "structuredData.json").exists()
            entrada["adobe_cache_dir"] = str(cache_dir.name)

        entradas.append(entrada)

    entradas.sort(key=lambda e: (e["tipo"], e["canonico"]))
    manifest = {
        "total_archivos_origen": sum(len(g) for g in grupos.values()),
        "total_unicos": len(grupos),
        "entradas": entradas,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def cmd_inventario():
    m = construir_manifest()
    print(f"Total archivos en raiz: {m['total_archivos_origen']}")
    print(f"Unicos por hash: {m['total_unicos']}")
    por_tipo = {}
    por_estado = {}
    cache_pdf = 0
    for e in m["entradas"]:
        por_tipo[e["tipo"]] = por_tipo.get(e["tipo"], 0) + 1
        por_estado[e["estado"]] = por_estado.get(e["estado"], 0) + 1
        if e["tipo"] == ".pdf" and e.get("tiene_adobe_cache"):
            cache_pdf += 1
    print("Por tipo:")
    for t, n in sorted(por_tipo.items()):
        print(f"  {t}: {n}")
    print("Por estado:")
    for s, n in sorted(por_estado.items()):
        print(f"  {s}: {n}")
    print(f"PDFs con cache Adobe: {cache_pdf}")
    pendientes_pdf_sin_cache = [
        e for e in m["entradas"]
        if e["tipo"] == ".pdf" and e["estado"] == "pending" and not e.get("tiene_adobe_cache")
    ]
    print(f"PDFs pendientes SIN cache Adobe (requieren llamada paga): {len(pendientes_pdf_sin_cache)}")
    print(f"Manifest: {MANIFEST_PATH}")


def guardar_manifest(m: dict):
    MANIFEST_PATH.write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def procesar_pdf(entrada: dict, permitir_adobe: bool) -> dict:
    """Procesa un PDF via pdf_a_formato_hd.py."""
    pdf_path = ROOT / entrada["canonico"]
    if not pdf_path.exists():
        entrada["estado"] = "error"
        entrada["error"] = f"PDF no existe: {pdf_path.name}"
        return entrada

    if not entrada.get("tiene_adobe_cache") and not permitir_adobe:
        entrada["estado"] = "skipped_no_cache"
        entrada["error"] = "Sin cache Adobe y --solo-cache activo"
        return entrada

    script = ROOT / "pdf_a_formato_hd.py"

    # Pre-borrar HD esperado para evitar prompt interactivo de llenar_formato_hd.py
    import re as _re
    base = pdf_path.stem
    base_corto = _re.sub(r"[^\w\-]+", "_", base)
    base_corto = _re.sub(r"_+", "_", base_corto).strip("_")[:60].rstrip("_")
    hd_esperado = ROOT / f"formato-hd-{base_corto.lower()}.xlsx"
    if hd_esperado.exists():
        try:
            hd_esperado.unlink()
        except Exception:
            pass

    # Forzamos Claude: el parser heuristico da 0% en muchos PDFs;
    # Haiku da resultados utiles y es barato (~$0.005/PDF).
    cmd = [sys.executable, str(script), str(pdf_path), "--claude"]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=600,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        entrada["estado"] = "error"
        entrada["error"] = "Timeout (>10min)"
        return entrada

    # llenar_formato_hd corre al final del script; nos importa el intermedio
    intermedio = ROOT / f"_intermedio_{base_corto}.xlsx"

    # Capturamos returncode pero NO fallamos por el; lo que importa es si
    # el intermedio se genero. llenar_formato_hd a veces falla pero el
    # intermedio ya esta listo (que es lo que necesitamos para ingestar).
    if not intermedio.exists():
        entrada["estado"] = "error"
        salida = (res.stderr or res.stdout)[-500:]
        entrada["error"] = f"rc={res.returncode}: {salida}"
        return entrada

    entrada["intermedio"] = intermedio.name
    entrada["estado"] = "done"
    entrada["error"] = None
    if res.returncode != 0:
        entrada["aviso_hd"] = "llenar_formato_hd fallo pero intermedio existe"
    return entrada


def xlsx_a_pdf(xlsx_path: Path) -> Path | None:
    """Convierte xlsx/xls a PDF via Excel COM. Devuelve ruta del PDF generado,
    o None si fallo. El PDF queda en la misma carpeta con el mismo basename + '.pdf'.
    """
    import win32com.client  # type: ignore
    import pythoncom  # type: ignore

    pdf_path = xlsx_path.with_suffix(".pdf")
    if pdf_path.exists():
        return pdf_path

    pythoncom.CoInitialize()
    excel = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(str(xlsx_path.resolve()), ReadOnly=True)
        # 0 = xlTypePDF
        wb.ExportAsFixedFormat(0, str(pdf_path.resolve()))
        wb.Close(False)
        return pdf_path if pdf_path.exists() else None
    except Exception as e:
        print(f"    ERROR convirtiendo xlsx a PDF: {e}")
        return None
    finally:
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass


def procesar_xlsx(entrada: dict, permitir_adobe: bool) -> dict:
    """Procesa un xlsx/xls original: lo convierte a PDF via Excel COM y lo
    manda por el mismo pipeline que los PDFs (Adobe + Haiku).

    Si Excel COM no esta disponible o falla la conversion, marca error.
    """
    xlsx_path = ROOT / entrada["canonico"]
    if not xlsx_path.exists():
        entrada["estado"] = "error"
        entrada["error"] = f"XLSX no existe: {xlsx_path.name}"
        return entrada

    pdf_path = xlsx_a_pdf(xlsx_path)
    if pdf_path is None:
        entrada["estado"] = "error"
        entrada["error"] = "No pude convertir xlsx a PDF (Excel COM)"
        return entrada

    entrada["pdf_convertido"] = pdf_path.name

    # Ahora procesar como PDF (puede llamar Adobe y/o Claude)
    if not permitir_adobe:
        # Verificar si ya hay cache adobe para este pdf
        import re as _re
        base = pdf_path.stem
        base_corto = _re.sub(r"[^\w\-]+", "_", base)
        base_corto = _re.sub(r"_+", "_", base_corto).strip("_")[:60].rstrip("_")
        cache = ROOT / f"_adobe_extract_{base_corto}" / "structuredData.json"
        if not cache.exists():
            entrada["estado"] = "skipped_no_cache"
            entrada["error"] = "PDF generado de xlsx sin cache Adobe y --solo-cache activo"
            return entrada

    # Reusar la misma logica de procesar_pdf pero apuntando al pdf convertido
    script = ROOT / "pdf_a_formato_hd.py"

    import re as _re
    base = pdf_path.stem
    base_corto = _re.sub(r"[^\w\-]+", "_", base)
    base_corto = _re.sub(r"_+", "_", base_corto).strip("_")[:60].rstrip("_")
    intermedio = ROOT / f"_intermedio_{base_corto}.xlsx"
    hd_esperado = ROOT / f"formato-hd-{base_corto.lower()}.xlsx"
    if hd_esperado.exists():
        try:
            hd_esperado.unlink()
        except Exception:
            pass

    cmd = [sys.executable, str(script), str(pdf_path), "--claude"]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=600,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        entrada["estado"] = "error"
        entrada["error"] = "Timeout (>10min)"
        return entrada

    if res.returncode != 0:
        entrada["estado"] = "error"
        entrada["error"] = (res.stderr or res.stdout)[-500:]
        return entrada
    if not intermedio.exists():
        entrada["estado"] = "error"
        entrada["error"] = f"No se genero {intermedio.name}"
        return entrada

    entrada["intermedio"] = intermedio.name
    entrada["estado"] = "done"
    entrada["error"] = None
    return entrada


def cmd_procesar(solo_cache: bool, maxn: int | None):
    m = construir_manifest()
    procesadas = 0
    for e in m["entradas"]:
        if e["estado"] in ("done", "skipped_no_cache"):
            continue
        if maxn is not None and procesadas >= maxn:
            break

        nombre = e["canonico"].encode("ascii", "replace").decode("ascii")
        print(f"\n[{procesadas + 1}] {e['tipo']} {nombre}")
        print(f"    estado previo: {e['estado']}, cache_adobe: {e.get('tiene_adobe_cache')}")

        if e["tipo"] == ".pdf":
            e = procesar_pdf(e, permitir_adobe=not solo_cache)
        elif e["tipo"] in (".xlsx", ".xls"):
            e = procesar_xlsx(e, permitir_adobe=not solo_cache)
        else:
            e["estado"] = "skipped_unknown_ext"

        print(f"    -> {e['estado']}" + (f"  {(e.get('error') or '')[:80]}" if e.get("error") else ""))
        # Persistir despues de cada uno (resiliente a interrupcion)
        # reemplazar la entrada en el manifest por hash
        for i, ent in enumerate(m["entradas"]):
            if ent["hash"] == e["hash"]:
                m["entradas"][i] = e
                break
        guardar_manifest(m)
        procesadas += 1

    # Resumen
    print("\n=== RESUMEN ===")
    estados = {}
    for ent in m["entradas"]:
        estados[ent["estado"]] = estados.get(ent["estado"], 0) + 1
    for s, n in sorted(estados.items()):
        print(f"  {s}: {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventario", help="Lista archivos y construye manifest")
    p_proc = sub.add_parser("procesar", help="Procesa archivos pendientes")
    p_proc.add_argument("--solo-cache", action="store_true",
                        help="No llamar Adobe; saltar PDFs sin cache")
    p_proc.add_argument("--max", type=int, default=None,
                        help="Procesar a lo mas N archivos (smoke test)")
    args = ap.parse_args()

    if args.cmd == "inventario":
        cmd_inventario()
    elif args.cmd == "procesar":
        cmd_procesar(solo_cache=args.solo_cache, maxn=args.max)


if __name__ == "__main__":
    main()

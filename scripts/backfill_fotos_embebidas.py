#!/usr/bin/env python3
"""Backfill de fotos para PDFs tipo-tabla cuyas imagenes viven DENTRO de celdas
y que Adobe Extract NO emitio como figuras (carpeta figures/ casi vacia).

Caso original: "Guangzhou Yiyi Crafts" en el proyecto hd-flores. El ingest metio
los 134 productos con descripcion pero solo 2 con foto, porque el pipeline solo
adjunta fotos desde figures/ de Adobe y ese PDF es una tabla de 16 paginas con
las fotos embebidas en las celdas.

Estrategia (validada visualmente contra el PDF):
  1. PyMuPDF saca las imagenes-producto embebidas (columna 2, ~60x74pt) con su
     posicion. Se renderiza la REGION (bbox), no extract_image(xref): en tablas
     PyMuPDF suele reportar un xref equivocado (varias celdas comparten uno),
     pero la posicion siempre es correcta.
  2. Se anclan a cada producto por su codigo "Y-N" que Adobe OCR-eo con
     coordenadas (structuredData.json). Coord Adobe = origen abajo-izq, se
     voltea a top-left para comparar con PyMuPDF: y_tl = alto_pagina - y_adobe.
  3. Los codigos que el OCR no leyo (aqui Y-22..26 y Y-79) se interpolan: dentro
     de cada pagina, numero-de-fila vs Y es lineal (filas equiespaciadas).

Idempotente: reemplaza TODAS las fotos del proveedor (borra las viejas, que
pueden estar mal ubicadas, y reescribe las 134). Hace backup de la BD antes.

Uso:
    python scripts/backfill_fotos_embebidas.py            # dry-run (no escribe)
    python scripts/backfill_fotos_embebidas.py --apply    # aplica los cambios

    Opciones:
      --slug hd-flores           proyecto (default: hd-flores)
      --proveedor Yiyi           substring del nombre del proveedor (default: Yiyi)
      --pdf "indigest-pdf/..."   PDF origen (default: autodetecta por proveedor)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("Falta PyMuPDF. Instala:  pip install pymupdf")

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
INGEST_DIR = ROOT / (__import__("os").environ.get("PDF_INGEST_DIR") or "indigest-pdf")

RE_CODE = re.compile(r"^Y[\-\s]?(\d{1,3})$", re.I)
RE_SKU = re.compile(r"^Y-(\d+)$", re.I)


# --------------------------------------------------------------------------- #
# Matcheo imagen -> codigo Y-N por posicion
# --------------------------------------------------------------------------- #
def _codigos_adobe(structured_json: Path):
    """[(num, page, y_topleft)] de cada codigo Y-N OCR-eado, en coords top-left."""
    d = json.loads(structured_json.read_text(encoding="utf-8"))
    alturas = {p["page_number"]: p["height"] for p in (d.get("pages") or [])}
    out = []
    for e in d.get("elements", []):
        t = (e.get("Text") or "").strip().replace(" ", "")
        m = RE_CODE.match(t)
        b = e.get("Bounds")
        if m and b:
            pg = e.get("Page", 0)
            H = alturas.get(pg, 841.92)
            y_tl = H - (b[1] + b[3]) / 2
            out.append((int(m.group(1)), pg, y_tl))
    out.sort(key=lambda c: c[0])
    return out


def _imagenes_producto(pg):
    """Imagenes candidatas a foto de producto (columna 2 de la tabla)."""
    r = []
    for im in pg.get_image_info(xrefs=True):
        b = im["bbox"]; w = b[2] - b[0]; h = b[3] - b[1]
        if 45 <= w <= 95 and 66 <= h <= 82 and 55 <= b[0] <= 150 and im.get("xref"):
            r.append({"bbox": tuple(b[:4]), "yc": (b[1] + b[3]) / 2})
    r.sort(key=lambda d: d["yc"])
    return r


def matchear(pdf_path: Path, structured_json: Path):
    """Devuelve {num: {'page','bbox','via','d'}} para Y-1..Y-N."""
    doc = fitz.open(pdf_path)
    codigos = _codigos_adobe(structured_json)
    cod_por_pag: dict[int, list] = {}
    for num, pg, y in codigos:
        cod_por_pag.setdefault(pg, []).append((num, y))
    for pg in cod_por_pag:
        cod_por_pag[pg].sort(key=lambda c: c[1])

    # rango esperado de numeros por pagina, segun la secuencia global
    paginas = sorted(cod_por_pag)
    max_hasta, acc = {}, 0
    for pi in range(doc.page_count):
        if pi in cod_por_pag:
            acc = max(acc, max(n for n, _ in cod_por_pag[pi]))
        max_hasta[pi] = acc
    esperado = {}
    for pi in paginas:
        lo = (max_hasta[pi - 1] + 1) if max_hasta.get(pi - 1) else min(n for n, _ in cod_por_pag[pi])
        post = [n for p2 in paginas if p2 > pi for n, _ in cod_por_pag[p2]]
        hi = (min(post) - 1) if post else max(n for n, _ in cod_por_pag[pi])
        esperado[pi] = (lo, hi)

    asign: dict[int, dict] = {}
    for pi in range(doc.page_count):
        pg = doc[pi]
        imgs = _imagenes_producto(pg)
        codes = cod_por_pag.get(pi, [])
        usadas: set[int] = set()
        # 1) cada codigo leido toma la imagen mas cercana en Y
        for num, ycode in codes:
            mejor, md = None, 1e9
            for i, im in enumerate(imgs):
                if i in usadas:
                    continue
                dd = abs(im["yc"] - ycode)
                if dd < md:
                    md, mejor = dd, i
            if mejor is not None and md < 60:
                usadas.add(mejor)
                asign[num] = {"page": pi, "bbox": imgs[mejor]["bbox"], "via": "ocr", "d": round(md)}
        # 2) interpolar codigos que el OCR no leyo (num vs Y lineal en la pagina)
        if len(codes) >= 2:
            xs = [n for n, _ in codes]; ys = [y for _, y in codes]
            n = len(xs); sx = sum(xs); sy = sum(ys)
            sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
            den = n * sxx - sx * sx
            if den:
                slope = (n * sxy - sx * sy) / den
                inter = (sy - slope * sx) / n
                lo, hi = esperado.get(pi, (min(xs), max(xs)))
                for num in range(lo, hi + 1):
                    if num in asign:
                        continue
                    ypred = slope * num + inter
                    mejor, md = None, 1e9
                    for i in range(len(imgs)):
                        if i in usadas:
                            continue
                        dd = abs(imgs[i]["yc"] - ypred)
                        if dd < md:
                            md, mejor = dd, i
                    if mejor is not None and md < 70:
                        usadas.add(mejor)
                        asign[num] = {"page": pi, "bbox": imgs[mejor]["bbox"], "via": "interp", "d": round(md)}
    doc.close()
    return asign


def _render_png(pg, bbox, escala=4.0) -> bytes:
    pix = pg.get_pixmap(matrix=fitz.Matrix(escala, escala), clip=fitz.Rect(*bbox))
    return pix.tobytes("png")


# --------------------------------------------------------------------------- #
# BD
# --------------------------------------------------------------------------- #
def _autodetectar_pdf(nombre_prov: str) -> Path | None:
    """Busca en indigest-pdf/ el _adobe_extract_* cuyo structuredData mencione
    al proveedor, o cae al PDF mas reciente."""
    pdfs = sorted(INGEST_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="aplica (sin esto: dry-run)")
    ap.add_argument("--slug", default="hd-flores")
    ap.add_argument("--proveedor", default="Yiyi", help="substring del nombre del proveedor")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--escala", type=float, default=4.0)
    args = ap.parse_args()

    db_path = DATA_DIR / "proyectos" / args.slug / "productos.db"
    if not db_path.exists():
        print(f"No existe la BD del proyecto: {db_path}", file=sys.stderr)
        return 1

    # resolver proveedor
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    provs = con.execute(
        "SELECT id, nombre, archivo_pdf FROM proveedores WHERE nombre LIKE ?",
        (f"%{args.proveedor}%",),
    ).fetchall()
    con.close()
    if not provs:
        print(f"Ningun proveedor coincide con '{args.proveedor}' en {args.slug}", file=sys.stderr)
        return 1
    if len(provs) > 1:
        print(f"Multiples proveedores coinciden: {[p[1] for p in provs]}. Afina --proveedor.", file=sys.stderr)
        return 1
    prov_id, prov_nombre, prov_pdf = provs[0]
    print(f"Proveedor: [{prov_id}] {prov_nombre}")

    # resolver PDF + extract
    pdf_path = Path(args.pdf) if args.pdf else (INGEST_DIR / prov_pdf if prov_pdf and (INGEST_DIR / prov_pdf).exists() else _autodetectar_pdf(prov_nombre))
    if not pdf_path or not pdf_path.exists():
        print(f"No encuentro el PDF origen (prov.archivo_pdf={prov_pdf!r}). Pasa --pdf.", file=sys.stderr)
        return 1
    base = re.sub(r"_+", "_", re.sub(r"[^\w\-]+", "_", pdf_path.stem)).strip("_")[:60].rstrip("_")
    extract = INGEST_DIR / f"_adobe_extract_{base}"
    structured = extract / "structuredData.json"
    if not structured.exists():
        print(f"No existe {structured}. Necesito el extract de Adobe del PDF.", file=sys.stderr)
        return 1
    print(f"PDF:     {pdf_path.name}")
    print(f"Extract: {structured.parent.name}")

    # matcheo
    asign = matchear(pdf_path, structured)
    print(f"Imagenes matcheadas: {len(asign)}  (ocr={sum(1 for a in asign.values() if a['via']=='ocr')}, "
          f"interp={sum(1 for a in asign.values() if a['via']=='interp')})")

    # productos del proveedor
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    prods = con.execute(
        "SELECT id, sku FROM productos WHERE proveedor_id=? ORDER BY id", (prov_id,)
    ).fetchall()
    con.close()

    plan = []          # (prod_id, sku, num)
    sin_match = []
    for pid, sku in prods:
        m = RE_SKU.match(sku or "")
        num = int(m.group(1)) if m else None
        if num and num in asign:
            plan.append((pid, sku, num))
        else:
            sin_match.append(sku)
    print(f"Productos: {len(prods)}  con foto a asignar: {len(plan)}  sin match: {len(sin_match)}")
    if sin_match:
        print(f"  (sin match: {sin_match[:15]}{' ...' if len(sin_match) > 15 else ''})")

    if not args.apply:
        print("\n[DRY-RUN] No se escribio nada. Corre con --apply para aplicar.")
        print("Ejemplos del plan:")
        for pid, sku, num in plan[:5]:
            a = asign[num]
            print(f"  {sku} (prod {pid}) <- pag{a['page']+1} bbox {tuple(round(v) for v in a['bbox'])} via={a['via']}")
        return 0

    # ---- APLICAR ----
    fotos_dir = db_path.parent / "fotos"
    fotos_dir.mkdir(parents=True, exist_ok=True)

    # backup de la BD
    backup = db_path.with_suffix(f".db.bak-{int(time.time())}")
    shutil.copy2(db_path, backup)
    print(f"\nBackup BD: {backup.name}")

    doc = fitz.open(pdf_path)
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    try:
        cur = con.cursor()
        # borrar fotos viejas del proveedor (pueden estar mal ubicadas)
        viejas = cur.execute(
            "SELECT f.id, f.ruta_relativa FROM fotos f "
            "JOIN productos p ON p.id=f.producto_id WHERE p.proveedor_id=?", (prov_id,)
        ).fetchall()
        for fid, ruta in viejas:
            rel = (ruta or "").replace("\\", "/")
            if rel.startswith("fotos/"):
                fp = fotos_dir / rel.split("/", 1)[1]
                try:
                    fp.unlink()
                except OSError:
                    pass
        cur.execute(
            "DELETE FROM fotos WHERE producto_id IN "
            "(SELECT id FROM productos WHERE proveedor_id=?)", (prov_id,)
        )
        print(f"Fotos previas borradas: {len(viejas)}")

        creadas = 0
        for pid, sku, num in plan:
            a = asign[num]
            png = _render_png(doc[a["page"]], a["bbox"], args.escala)
            nombre = f"{prov_id}_{pid}_{sku}.png".replace("/", "_").replace("\\", "_")
            (fotos_dir / nombre).write_bytes(png)
            cur.execute(
                "INSERT INTO fotos (producto_id, ruta_relativa, es_principal) VALUES (?,?,1)",
                (pid, f"fotos/{nombre}"),
            )
            creadas += 1
        con.commit()
        print(f"Fotos creadas: {creadas}")
    except Exception as e:
        con.rollback()
        print(f"ERROR, rollback: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()
        doc.close()

    print("Listo. Refresca la vista de productos para ver las fotos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

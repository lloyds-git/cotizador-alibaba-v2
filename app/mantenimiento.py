"""Mantenimiento de la carpeta de ingesta de PDFs (PDF_INGEST_DIR).

Cada ingesta deja artefactos de TRABAJO que no se usan en runtime:
  - `_adobe_extract_<stem>/`  (structuredData.json, tables/, figures/, y el
    `_resultado.zip` crudo de Adobe ~20 MB).
  - `_intermedio_<stem>.*`    (xlsx + .meta.json).
El PDF subido SI se conserva, pero solo si algun producto lo referencia como
"Cotizacion original" (productos/proveedores.archivo_pdf); los PDFs de intentos
FALLIDOS quedan huerfanos.

Este modulo analiza y purga esos artefactos. Lo usan el endpoint de UI
(POST /api/mantenimiento/indigest/...) y scripts/limpiar_indigest.py.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROYECTOS_DIR = ROOT / "data" / "proyectos"


def ingest_dir() -> Path:
    return ROOT / (os.environ.get("PDF_INGEST_DIR") or "indigest-pdf")


def _tam(path: Path) -> int:
    """Bytes de un archivo, o suma recursiva de un directorio."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def pdfs_referenciados() -> set[str]:
    """Basenames de PDF referenciados por CUALQUIER BD de proyecto (la carpeta
    de ingesta es compartida entre proyectos, asi que se miran todas)."""
    refs: set[str] = set()
    for db in sorted(PROYECTOS_DIR.glob("*/productos.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for tabla in ("productos", "proveedores"):
                try:
                    rows = con.execute(
                        f"SELECT DISTINCT archivo_pdf FROM {tabla} "
                        "WHERE archivo_pdf IS NOT NULL AND archivo_pdf <> ''"
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for (v,) in rows:
                    refs.add(Path(str(v)).name)
        finally:
            con.close()
    return refs


def analizar() -> dict:
    """Devuelve que se borraria (sin borrar): items + tamanos + PDFs vivos."""
    ing = ingest_dir()
    vivos = pdfs_referenciados()
    items: list[dict] = []
    if ing.is_dir():
        for p in sorted(ing.glob("_adobe_extract_*")):
            if p.is_dir():
                items.append({"nombre": p.name, "tipo": "extract", "bytes": _tam(p)})
        for p in sorted(ing.glob("_intermedio_*")):
            if p.is_file():
                items.append({"nombre": p.name, "tipo": "intermedio", "bytes": _tam(p)})
        for p in sorted(ing.glob("*.pdf")):
            if p.is_file() and p.name not in vivos:
                items.append({"nombre": p.name, "tipo": "pdf_huerfano", "bytes": _tam(p)})
    total = sum(i["bytes"] for i in items)
    return {
        "carpeta": str(ing),
        "vivos": sorted(vivos),
        "items": items,
        "total_bytes": total,
        "total_mb": round(total / 1_048_576, 1),
        "n_items": len(items),
        "n_extracts": sum(1 for i in items if i["tipo"] == "extract"),
        "n_intermedios": sum(1 for i in items if i["tipo"] == "intermedio"),
        "n_huerfanos": sum(1 for i in items if i["tipo"] == "pdf_huerfano"),
    }


def limpiar() -> dict:
    """Aplica el borrado de lo que reporta analizar(). No toca PDFs vivos."""
    ing = ingest_dir()
    info = analizar()
    borrados, errores, liberado = 0, 0, 0
    detalle_err: list[str] = []
    for it in info["items"]:
        p = ing / it["nombre"]
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            else:
                continue
            borrados += 1
            liberado += it["bytes"]
        except OSError as e:
            errores += 1
            detalle_err.append(f"{it['nombre']}: {e}")
    return {
        "borrados": borrados,
        "errores": errores,
        "detalle_errores": detalle_err,
        "liberado_bytes": liberado,
        "liberado_mb": round(liberado / 1_048_576, 1),
    }

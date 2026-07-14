#!/usr/bin/env python3
"""Limpia la carpeta de ingesta de PDFs (PDF_INGEST_DIR, default indigest-pdf/).

Borra artefactos de TRABAJO (_adobe_extract_*, _intermedio_*) y PDFs huerfanos
de intentos fallidos (no referenciados por ningun producto). Conserva los PDFs
vivos ("Cotizacion original"). Misma logica que el boton de la UI
(POST /api/mantenimiento/indigest/limpiar), via app.mantenimiento.

Uso:
    python scripts/limpiar_indigest.py            # dry-run (no borra)
    python scripts/limpiar_indigest.py --apply    # aplica
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import mantenimiento  # noqa: E402


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="borra (sin esto: dry-run)")
    args = ap.parse_args()

    info = mantenimiento.analizar()
    print(f"Carpeta: {info['carpeta']}")
    print(f"PDFs referenciados (se conservan): {len(info['vivos'])}")
    for v in info["vivos"]:
        print(f"   [vivo] {v}")

    print("\nA borrar:")
    for it in info["items"]:
        print(f"   [{it['tipo']:<13}] {it['nombre']}  ({_mb(it['bytes'])})")
    print(
        f"\nEspacio a liberar: {info['total_mb']} MB  ({info['n_items']} elementos: "
        f"{info['n_extracts']} extracts, {info['n_intermedios']} intermedios, "
        f"{info['n_huerfanos']} PDF huerfanos)"
    )

    if not args.apply:
        print("\n[DRY-RUN] No se borro nada. Corre con --apply para aplicar.")
        return 0

    res = mantenimiento.limpiar()
    print(f"\nBorrados: {res['borrados']}  errores: {res['errores']}  "
          f"liberado: ~{res['liberado_mb']} MB")
    for e in res.get("detalle_errores", []):
        print(f"   ERROR: {e}", file=sys.stderr)
    return 0 if res["errores"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

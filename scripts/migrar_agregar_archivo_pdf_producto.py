#!/usr/bin/env python3
"""Migracion: agrega la columna `archivo_pdf` a `productos` (PDF de origen por
producto) y hace backfill desde `proveedores.archivo_pdf` para los existentes.

Permite que un mismo proveedor tenga varios PDFs y cada producto enlace a su
"Cotizacion original" correcta. Idempotente.

Uso:
    python scripts/migrar_agregar_archivo_pdf_producto.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TEMPLATE_PATH = DATA_DIR / "_template.db"
PROYECTOS_DIR = DATA_DIR / "proyectos"
LEGACY_PATH = DATA_DIR / "productos.db"


def tabla_existe(con, tabla):
    return con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
    ).fetchone() is not None


def columnas(con, tabla):
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()}


def migrar_bd(db_path: Path) -> str:
    con = sqlite3.connect(db_path)
    try:
        if not tabla_existe(con, "productos"):
            return f"  - {db_path}: sin tabla 'productos', se omite."
        agregada = False
        if "archivo_pdf" not in columnas(con, "productos"):
            with con:
                con.execute("ALTER TABLE productos ADD COLUMN archivo_pdf VARCHAR(500)")
            agregada = True
        # Backfill: productos sin archivo_pdf heredan el de su proveedor.
        with con:
            n = con.execute(
                "UPDATE productos SET archivo_pdf = ("
                "  SELECT archivo_pdf FROM proveedores WHERE proveedores.id = productos.proveedor_id"
                ") WHERE (archivo_pdf IS NULL OR archivo_pdf = '')"
            ).rowcount
        estado = "+columna 'archivo_pdf'" if agregada else "columna ya presente"
        return f"  - {db_path}: {estado}; backfill {n} productos"
    finally:
        con.close()


def recolectar_bds():
    bds = []
    if TEMPLATE_PATH.exists():
        bds.append(TEMPLATE_PATH)
    if PROYECTOS_DIR.exists():
        bds.extend(sorted(PROYECTOS_DIR.glob("*/productos.db")))
    if LEGACY_PATH.exists():
        bds.append(LEGACY_PATH)
    return bds


def main() -> int:
    bds = recolectar_bds()
    if not bds:
        print("No se encontro ninguna BD. Nada que migrar.", file=sys.stderr)
        return 1
    print(f"Migrando {len(bds)} BD(s):")
    for db_path in bds:
        try:
            print(migrar_bd(db_path))
        except Exception as e:
            print(f"  - {db_path}: ERROR {e}", file=sys.stderr)
            return 2
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

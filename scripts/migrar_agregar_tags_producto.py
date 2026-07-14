#!/usr/bin/env python3
"""Migracion: agrega la columna `tags` a la tabla `productos`.

Feature: tags/keywords por producto (csv en minusculas) para busqueda. Se
generan junto a la descripcion por vision cuando el PDF no trae texto
(app/catalogo_ia.describir_fotos).

Idempotente: ADD COLUMN solo si falta. Corre sobre la plantilla, todos los
proyectos y la BD legacy. No toca datos.

Uso:
    python scripts/migrar_agregar_tags_producto.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TEMPLATE_PATH = DATA_DIR / "_template.db"
PROYECTOS_DIR = DATA_DIR / "proyectos"
LEGACY_PATH = DATA_DIR / "productos.db"


def tabla_existe(con: sqlite3.Connection, tabla: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
    ).fetchone()
    return row is not None


def columnas(con: sqlite3.Connection, tabla: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()}


def migrar_bd(db_path: Path) -> str:
    con = sqlite3.connect(db_path)
    try:
        if not tabla_existe(con, "productos"):
            return f"  - {db_path}: sin tabla 'productos', se omite."
        if "tags" in columnas(con, "productos"):
            return f"  - {db_path}: columna 'tags' ya presente."
        with con:
            con.execute("ALTER TABLE productos ADD COLUMN tags TEXT")
        return f"  - {db_path}: +columna 'tags'"
    finally:
        con.close()


def recolectar_bds() -> list[Path]:
    bds: list[Path] = []
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

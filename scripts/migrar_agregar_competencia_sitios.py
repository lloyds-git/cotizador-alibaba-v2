#!/usr/bin/env python3
"""Migracion: agrega columnas de sitios de competencia a la tabla `categorias`.

Contexto (feature: sitios de competencia por tipo de producto):
- La busqueda de competencia consultaba 3 dominios fijos (Amazon/ML/Petco MX).
  Ahora cada categoria puede definir en que sitios buscar (columna
  `competencia_sitios`, CSV de dominios). NULL/vacio => se usan los 3 default.
- Cada proyecto tiene su propia BD (data/proyectos/<slug>/productos.db), clonada
  de data/_template.db. Necesitamos las columnas en `categorias` en todas ellas.

Idempotente: ADD COLUMN solo si falta. No vacia categorias ni toca los productos.

Uso:
    python scripts/migrar_agregar_competencia_sitios.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TEMPLATE_PATH = DATA_DIR / "_template.db"
PROYECTOS_DIR = DATA_DIR / "proyectos"
LEGACY_PATH = DATA_DIR / "productos.db"  # instalacion pre-multiproyecto

# (nombre, tipo SQL) de las columnas a agregar en `categorias`.
COLUMNAS = [
    ("competencia_sitios", "TEXT"),
    ("competencia_actualizado_en", "DATETIME"),
]


def tabla_existe(con: sqlite3.Connection, tabla: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
    ).fetchone()
    return row is not None


def columnas_actuales(con: sqlite3.Connection, tabla: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()}


def migrar_bd(db_path: Path) -> str:
    """Agrega columnas faltantes en `categorias`. Devuelve resumen."""
    con = sqlite3.connect(db_path)
    try:
        if not tabla_existe(con, "categorias"):
            return f"  - {db_path}: sin tabla 'categorias', se omite."

        existentes = columnas_actuales(con, "categorias")
        agregadas = []
        with con:
            for nombre, tipo in COLUMNAS:
                if nombre not in existentes:
                    con.execute(
                        f"ALTER TABLE categorias ADD COLUMN {nombre} {tipo}"
                    )
                    agregadas.append(nombre)

        detalle = (
            f"+{len(agregadas)} columnas ({', '.join(agregadas)})"
            if agregadas else "columnas ya presentes"
        )
        return f"  - {db_path}: {detalle}"
    finally:
        con.close()


def recolectar_bds() -> list[Path]:
    """Devuelve las rutas de todas las BDs a migrar."""
    bds: list[Path] = []
    if TEMPLATE_PATH.exists():
        bds.append(TEMPLATE_PATH)
    if PROYECTOS_DIR.exists():
        for db in sorted(PROYECTOS_DIR.glob("*/productos.db")):
            bds.append(db)
    if LEGACY_PATH.exists():
        bds.append(LEGACY_PATH)
    return bds


def main() -> int:
    bds = recolectar_bds()
    if not bds:
        print(
            "No se encontro ninguna BD (_template.db / proyectos / productos.db). "
            "Nada que migrar.",
            file=sys.stderr,
        )
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

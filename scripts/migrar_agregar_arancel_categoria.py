#!/usr/bin/env python3
"""Migracion: agrega columnas de arancel a la tabla `categorias` y vacia las
categorias de la plantilla `_template.db`.

Contexto (feature de bootstrapping de catalogo asistido por IA):
- Cada proyecto tiene su propia BD (data/proyectos/<slug>/productos.db), clonada
  de data/_template.db. Necesitamos las columnas fraccion/tasa_pct/arancel_estado/
  arancel_nota/arancel_fuente_url/arancel_actualizado_en en `categorias`.
- Ademas, los proyectos NUEVOS deben arrancar sin categorias (la IA las propone
  desde las ingestas), asi que vaciamos categorias/categoria_keywords SOLO en la
  plantilla. Las BDs de proyectos existentes conservan sus categorias (pet).

Idempotente: ADD COLUMN solo si falta; el DELETE de la plantilla es seguro de
re-correr (deja la plantilla vacia). No toca los productos.db de proyectos.

Uso:
    python scripts/migrar_agregar_arancel_categoria.py
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
    ("fraccion", "VARCHAR(20)"),
    ("tasa_pct", "FLOAT"),
    ("arancel_estado", "VARCHAR(20)"),
    ("arancel_nota", "TEXT"),
    ("arancel_fuente_url", "TEXT"),
    ("arancel_actualizado_en", "DATETIME"),
]


def tabla_existe(con: sqlite3.Connection, tabla: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
    ).fetchone()
    return row is not None


def columnas_actuales(con: sqlite3.Connection, tabla: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()}


def migrar_bd(db_path: Path, vaciar_categorias: bool) -> str:
    """Agrega columnas faltantes; opcionalmente vacia categorias. Devuelve resumen."""
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

        if vaciar_categorias:
            n_cats = con.execute("SELECT count(*) FROM categorias").fetchone()[0]
            with con:
                if tabla_existe(con, "categoria_keywords"):
                    con.execute("DELETE FROM categoria_keywords")
                con.execute("DELETE FROM categorias")
            detalle += f"; vaciadas {n_cats} categorias (plantilla)"

        return f"  - {db_path}: {detalle}"
    finally:
        con.close()


def recolectar_bds() -> list[tuple[Path, bool]]:
    """Devuelve [(ruta, vaciar_categorias)]. Solo la plantilla se vacia."""
    bds: list[tuple[Path, bool]] = []
    if TEMPLATE_PATH.exists():
        bds.append((TEMPLATE_PATH, True))
    if PROYECTOS_DIR.exists():
        for db in sorted(PROYECTOS_DIR.glob("*/productos.db")):
            bds.append((db, False))
    if LEGACY_PATH.exists():
        bds.append((LEGACY_PATH, False))
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
    for db_path, vaciar in bds:
        try:
            print(migrar_bd(db_path, vaciar))
        except Exception as e:
            print(f"  - {db_path}: ERROR {e}", file=sys.stderr)
            return 2
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

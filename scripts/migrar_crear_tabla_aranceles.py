#!/usr/bin/env python3
"""Migracion: crea la tabla `aranceles` (estandar) si no existe.

Idempotente. La tabla queda vacia; correr seed_aranceles.py despues para
poblarla desde config/aranceles.yml.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"


def tabla_existe(con: sqlite3.Connection, tabla: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
    ).fetchone()
    return row is not None


SQL_CREATE = """
CREATE TABLE aranceles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    categoria VARCHAR(50) NOT NULL,
    subcategoria VARCHAR(50) NOT NULL,
    fraccion VARCHAR(20) NOT NULL,
    tasa_pct FLOAT NOT NULL,
    nota TEXT,
    creado_en DATETIME,
    actualizado_en DATETIME,
    CONSTRAINT uq_arancel_cat_subcat UNIQUE (categoria, subcategoria)
)
"""


def main() -> int:
    if not DB_PATH.exists():
        print(f"BD no existe: {DB_PATH}", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB_PATH)
    if tabla_existe(con, "aranceles"):
        print("La tabla aranceles ya existe. Nada que hacer.")
        con.close()
        return 0
    try:
        with con:
            con.execute(SQL_CREATE)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        con.close()
        return 2
    print("Tabla aranceles creada. Corre 'python scripts/seed_aranceles.py' para poblarla.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

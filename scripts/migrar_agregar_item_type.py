#!/usr/bin/env python3
"""Migracion: agregar columna item_type a productos.

SQLite soporta ALTER TABLE ADD COLUMN sin recrear la tabla, asi que el
AUTOINCREMENT y todos los datos quedan intactos. Idempotente: si la
columna ya existe, sale 0 sin hacer nada.

Default 'Primary' para registros existentes. El selector en la UI
permite cambiarlo a 'Special Buy'.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"


def columna_existe(con: sqlite3.Connection, tabla: str, columna: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({tabla})").fetchall()
    return any(r[1] == columna for r in rows)


def main() -> int:
    if not DB_PATH.exists():
        print(f"BD no existe: {DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB_PATH)

    if columna_existe(con, "productos", "item_type"):
        print("La columna item_type ya existe. Nada que hacer.")
        con.close()
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"productos.pre-item_type-{ts}.db")
    shutil.copy2(DB_PATH, backup)
    print(f"Backup: {backup}")

    try:
        with con:
            con.execute(
                "ALTER TABLE productos ADD COLUMN item_type VARCHAR(20) DEFAULT 'Primary'"
            )
    except Exception as e:
        print(f"ERROR en migracion: {e}", file=sys.stderr)
        print(f"Restaura desde el backup: {backup}", file=sys.stderr)
        con.close()
        return 2

    if not columna_existe(con, "productos", "item_type"):
        print("ERROR: la columna no aparece tras el ALTER.", file=sys.stderr)
        con.close()
        return 3

    n = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    con.close()
    print(f"Columna item_type agregada. Productos en tabla: {n}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Migracion: agregar columnas nw_caja_kg y gw_caja_kg a productos.

SQLite soporta ALTER TABLE ADD COLUMN sin recrear la tabla, asi que el
AUTOINCREMENT y todos los datos quedan intactos. Idempotente: si la
columna ya existe, no la vuelve a crear.

Sin default: queda NULL para registros existentes. Los ingestados nuevos
o re-ingestados llenan estas columnas desde el PDF (G.W./N.W. de
Alibaba). Permite derivar pzas_caja = floor(nw_caja_kg / peso_kg)
cuando el PI no trae 'Pcs/Ctn' explicito.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"

COLUMNAS_NUEVAS = [
    ("nw_caja_kg", "FLOAT"),
    ("gw_caja_kg", "FLOAT"),
]


def columna_existe(con: sqlite3.Connection, tabla: str, columna: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({tabla})").fetchall()
    return any(r[1] == columna for r in rows)


def main() -> int:
    if not DB_PATH.exists():
        print(f"BD no existe: {DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB_PATH)

    pendientes = [
        (col, tipo) for col, tipo in COLUMNAS_NUEVAS
        if not columna_existe(con, "productos", col)
    ]

    if not pendientes:
        print("Las columnas nw_caja_kg y gw_caja_kg ya existen. Nada que hacer.")
        con.close()
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"productos.pre-peso_caja-{ts}.db")
    shutil.copy2(DB_PATH, backup)
    print(f"Backup: {backup}")

    try:
        with con:
            for col, tipo in pendientes:
                con.execute(f"ALTER TABLE productos ADD COLUMN {col} {tipo}")
                print(f"  + Agregada columna {col} ({tipo})")
    except Exception as e:
        print(f"ERROR en migracion: {e}", file=sys.stderr)
        print(f"Restaura desde el backup: {backup}", file=sys.stderr)
        con.close()
        return 2

    for col, _ in COLUMNAS_NUEVAS:
        if not columna_existe(con, "productos", col):
            print(f"ERROR: la columna {col} no aparece tras el ALTER.", file=sys.stderr)
            con.close()
            return 3

    n = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    con.close()
    print(f"Migracion OK. Productos en tabla: {n}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

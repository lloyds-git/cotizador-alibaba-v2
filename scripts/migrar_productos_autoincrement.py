#!/usr/bin/env python3
"""Migracion: agregar AUTOINCREMENT a productos.id

Por que: SQLite reusa ROWIDs liberados por DELETE. Si un usuario borra
productos desde la UI y luego se re-ingesta, los nuevos productos pueden
tomar ids antes ocupados, lo que rompe referencias externas (exports xlsx
ya enviados, cotizaciones internas guardadas, notas con id=X, etc).
Con AUTOINCREMENT SQLite mantiene un contador en sqlite_sequence y
nunca reusa un id.

Idempotente: si la tabla ya tiene AUTOINCREMENT no hace nada.
Backup automatico de la BD junto a productos.db con sufijo de timestamp.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "productos.db"

# Columnas en el orden EXACTO en que existen en la tabla actual.
# (Se obtuvo con PRAGMA table_info(productos) en la BD viva.)
COLS = [
    "id", "proveedor_id", "sku", "descripcion", "fob_usd", "material",
    "medidas", "peso_kg", "color", "moq", "packing", "carton_dims",
    "cbm", "pzas_20ft", "pzas_40hq", "lead_time",
    "marcado_cotizar", "notas", "creado_en", "actualizado_en",
    "categoria", "subcategoria",
]

CREATE_NEW = """
CREATE TABLE productos_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proveedor_id INTEGER NOT NULL,
    sku VARCHAR(50),
    descripcion TEXT NOT NULL,
    fob_usd FLOAT,
    material VARCHAR(100),
    medidas VARCHAR(200),
    peso_kg FLOAT,
    color VARCHAR(200),
    moq VARCHAR(50),
    packing VARCHAR(200),
    carton_dims VARCHAR(200),
    cbm FLOAT,
    pzas_20ft INTEGER,
    pzas_40hq INTEGER,
    lead_time VARCHAR(100),
    marcado_cotizar BOOLEAN NOT NULL,
    notas TEXT,
    creado_en DATETIME,
    actualizado_en DATETIME,
    categoria VARCHAR(50),
    subcategoria VARCHAR(100),
    CONSTRAINT uq_proveedor_sku UNIQUE (proveedor_id, sku),
    FOREIGN KEY (proveedor_id) REFERENCES proveedores(id)
)
"""


def main() -> int:
    if not DB_PATH.exists():
        print(f"BD no existe: {DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT sql FROM sqlite_master WHERE name='productos'")
    row = cur.fetchone()
    if row is None:
        print("Tabla productos no existe.", file=sys.stderr)
        con.close()
        return 1
    sql_actual = row[0] or ""

    if "AUTOINCREMENT" in sql_actual.upper():
        print("La tabla productos ya tiene AUTOINCREMENT. Nada que hacer.")
        con.close()
        return 0

    # Backup
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"productos.pre-autoincrement-{ts}.db")
    shutil.copy2(DB_PATH, backup)
    print(f"Backup: {backup}")

    n_antes = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    max_id_antes = con.execute("SELECT COALESCE(MAX(id), 0) FROM productos").fetchone()[0]
    print(f"Productos antes: {n_antes} (max id = {max_id_antes})")

    # FKs OFF mientras hacemos el swap; las re-validara al final.
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        with con:
            con.execute(CREATE_NEW)
            col_list = ", ".join(COLS)
            con.execute(
                f"INSERT INTO productos_new ({col_list}) "
                f"SELECT {col_list} FROM productos"
            )
            con.execute("DROP TABLE productos")
            con.execute("ALTER TABLE productos_new RENAME TO productos")

            # Asegurar que el contador AUTOINCREMENT arranque despues del max_id real.
            # Despues del INSERT con ids explicitos, sqlite_sequence ya quedo en max_id,
            # pero el RENAME puede dejar la fila con name='productos_new' en versiones
            # viejas. Limpiamos y dejamos solo 'productos'.
            con.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('productos', 'productos_new')"
            )
            con.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                ("productos", max_id_antes),
            )
    except Exception as e:
        print(f"ERROR en migracion: {e}", file=sys.stderr)
        print(f"Restaura desde el backup: {backup}", file=sys.stderr)
        con.execute("PRAGMA foreign_keys=ON")
        con.close()
        return 2

    # Verificacion
    con.execute("PRAGMA foreign_keys=ON")
    sql_nuevo = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='productos'"
    ).fetchone()[0]
    n_despues = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    seq = con.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='productos'"
    ).fetchone()
    fk_issues = con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()

    if "AUTOINCREMENT" not in sql_nuevo.upper():
        print("ERROR: la tabla nueva no tiene AUTOINCREMENT.", file=sys.stderr)
        return 3
    if n_despues != n_antes:
        print(
            f"ERROR: conteo cambio ({n_antes} -> {n_despues}). Restaura backup.",
            file=sys.stderr,
        )
        return 4
    if fk_issues:
        print(f"AVISO: foreign_key_check reporta issues: {fk_issues}")

    print(f"Productos despues: {n_despues}")
    print(f"sqlite_sequence.productos = {seq[0] if seq else 0}")
    print(f"Proximo id asignado por SQLite: {(seq[0] if seq else 0) + 1}")
    print("Migracion OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

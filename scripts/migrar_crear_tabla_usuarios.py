#!/usr/bin/env python3
"""Migracion: crea la tabla `usuarios_autorizados` si no existe.

Idempotente. Para BDs creadas antes de OAuth. En arranques nuevos,
`init_db()` (Base.metadata.create_all) tambien la crea automaticamente.

Despues de correr esto, configura ADMIN_INICIAL en .env y reinicia la
app: el auto-seed inserta el primer correo autorizado al detectar la
tabla vacia.
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
CREATE TABLE usuarios_autorizados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email VARCHAR(200) NOT NULL UNIQUE,
    nombre VARCHAR(200),
    activo BOOLEAN NOT NULL DEFAULT 1,
    creado_en DATETIME,
    ultimo_login DATETIME
)
"""

SQL_INDEX = "CREATE INDEX ix_usuarios_autorizados_email ON usuarios_autorizados(email)"


def main() -> int:
    if not DB_PATH.exists():
        print(f"BD no existe: {DB_PATH}", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB_PATH)
    if tabla_existe(con, "usuarios_autorizados"):
        print("La tabla usuarios_autorizados ya existe. Nada que hacer.")
        con.close()
        return 0
    try:
        with con:
            con.execute(SQL_CREATE)
            con.execute(SQL_INDEX)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        con.close()
        return 2
    print("Tabla usuarios_autorizados creada.")
    print("Pon ADMIN_INICIAL en .env y reinicia la app para sembrar el primer usuario.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

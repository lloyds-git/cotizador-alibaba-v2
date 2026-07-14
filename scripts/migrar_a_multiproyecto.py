"""Migracion one-off: monolito de una sola BD -> modelo multi-proyecto.

Convierte el estado actual (data/productos.db + data/fotos/) en:
  - data/sistema.db                         -> usuarios_autorizados + registro `proyectos`
  - data/proyectos/principal/productos.db   -> los datos de negocio actuales
  - data/proyectos/principal/fotos/         -> las fotos actuales
  - data/_template.db                       -> plantilla limpia (config, sin productos)

Idempotente: si data/proyectos/principal/ ya existe, no re-migra. Hace backups
antes de mover nada.

Uso:
    python scripts/migrar_a_multiproyecto.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

PROYECTO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROYECTO_ROOT))

from app import db as db_module
from app.modelos import UsuarioAutorizado, Proyecto

SLUG_INICIAL = "principal"
NOMBRE_INICIAL = "Principal"

# Tablas de negocio que se VACIAN en la plantilla (se conserva la config:
# categorias, categoria_keywords, patrones_descarte, aranceles, aranceles_override).
TABLAS_NEGOCIO = [
    "competidor_listings",
    "cotizacion_snapshots",
    "costos_adicionales",
    "fotos",
    "productos",
    "proveedores",
]


def _tabla_existe(con: sqlite3.Connection, nombre: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)
    ).fetchone()
    return row is not None


def _migrar_usuarios(old_db: Path) -> int:
    """Copia usuarios_autorizados del productos.db viejo a sistema.db."""
    db_module.init_sistema_db()
    con = sqlite3.connect(str(old_db))
    con.row_factory = sqlite3.Row
    try:
        if not _tabla_existe(con, "usuarios_autorizados"):
            return 0
        filas = con.execute(
            "SELECT email, nombre, activo, creado_en, ultimo_login "
            "FROM usuarios_autorizados"
        ).fetchall()
    finally:
        con.close()

    Session = db_module.get_sistema_session_factory()
    n = 0
    with Session() as ses:
        for f in filas:
            existe = (
                ses.query(UsuarioAutorizado)
                .filter(UsuarioAutorizado.email == f["email"])
                .first()
            )
            if existe:
                continue
            ses.add(UsuarioAutorizado(
                email=f["email"],
                nombre=f["nombre"],
                activo=bool(f["activo"]),
            ))
            n += 1
        ses.commit()
    return n


def _registrar_proyecto() -> None:
    Session = db_module.get_sistema_session_factory()
    with Session() as ses:
        if ses.query(Proyecto).filter_by(slug=SLUG_INICIAL).first():
            return
        ses.add(Proyecto(slug=SLUG_INICIAL, nombre=NOMBRE_INICIAL, activo=True))
        ses.commit()


def _construir_template(fuente: Path) -> None:
    """Crea data/_template.db copiando `fuente` y vaciando las tablas de negocio."""
    tpl = db_module.TEMPLATE_PATH
    if tpl.exists():
        print(f"  Plantilla ya existe: {tpl} (no se toca)")
        return
    shutil.copy2(fuente, tpl)
    con = sqlite3.connect(str(tpl))
    try:
        for tabla in TABLAS_NEGOCIO:
            if _tabla_existe(con, tabla):
                con.execute(f"DELETE FROM {tabla}")
        # La plantilla es solo de negocio: la whitelist vive en sistema.db.
        con.execute("DROP TABLE IF EXISTS usuarios_autorizados")
        # Reinicia los contadores AUTOINCREMENT para que cada proyecto empiece en 1.
        if _tabla_existe(con, "sqlite_sequence"):
            con.execute("DELETE FROM sqlite_sequence")
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    print(f"  Plantilla construida: {tpl}")


def main() -> None:
    data = db_module.DATA_DIR
    old_db = data / "productos.db"
    old_fotos = data / "fotos"
    dest_db = db_module.ruta_bd_proyecto(SLUG_INICIAL)
    dest_fotos = db_module.fotos_dir_proyecto(SLUG_INICIAL)

    if dest_db.exists():
        print(f"Ya existe {dest_db}. Nada que migrar (idempotente).")
        # Aun asi, garantiza sistema.db y plantilla.
        db_module.init_sistema_db()
        _registrar_proyecto()
        if not db_module.TEMPLATE_PATH.exists():
            _construir_template(dest_db)
        return

    if not old_db.exists():
        print(f"No existe {old_db}: instalacion nueva sin datos previos.")
        db_module.init_sistema_db()
        db_module.asegurar_template()
        print("Listo. Crea proyectos desde la UI (/proyectos).")
        return

    print(f"Migrando {old_db} -> modelo multi-proyecto...")

    # 1. Sistema: usuarios + registro de proyecto.
    n_users = _migrar_usuarios(old_db)
    _registrar_proyecto()
    print(f"  sistema.db: {n_users} usuarios copiados; proyecto '{SLUG_INICIAL}' registrado.")

    # 2. Mover la BD de negocio al proyecto 'principal'.
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_db, dest_db)  # copia (no move) para conservar el original como backup
    print(f"  BD copiada -> {dest_db}")

    # 3. Mover fotos.
    dest_fotos.mkdir(parents=True, exist_ok=True)
    n_fotos = 0
    if old_fotos.exists():
        for item in old_fotos.iterdir():
            destino = dest_fotos / item.name
            if destino.exists():
                continue
            if item.is_file():
                shutil.copy2(item, destino)
                n_fotos += 1
    print(f"  fotos copiadas -> {dest_fotos} ({n_fotos} archivos)")

    # 4. Construir la plantilla limpia desde la BD de negocio.
    _construir_template(dest_db)

    print()
    print("Migracion completada. Verifica la app y luego puedes archivar/borrar")
    print(f"el original {old_db} y {old_fotos} (se copiaron, no se movieron).")


if __name__ == "__main__":
    main()

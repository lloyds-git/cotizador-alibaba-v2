"""Resolucion de bases de datos: una BD por proyecto + una BD de sistema.

Modelo multi-proyecto:
- Sistema  : data/sistema.db          -> auth global (usuarios_autorizados) +
             registro de proyectos. No depende del proyecto activo.
- Plantilla: data/_template.db        -> BD "limpia" (esquema + categorias +
             aranceles, sin productos). Se clona al crear un proyecto.
- Proyecto : data/proyectos/<slug>/productos.db -> datos de negocio. La BD
             activa se resuelve por request desde session['proyecto'].

El resto de la app resuelve la sesion de forma tardia via get_session_factory(slug):
get_db() en routes.py pasa el slug del proyecto activo. Los engines se cachean
por ruta (antes se recreaban en cada llamada).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.modelos import Base, SistemaBase

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
PROYECTOS_DIR = DATA_DIR / "proyectos"
TEMPLATE_PATH = DATA_DIR / "_template.db"
SISTEMA_PATH = DATA_DIR / "sistema.db"

# Proyecto usado por CLI/scripts y como fallback cuando no hay sesion (no aplica
# a requests web, que siempre pasan el slug de session['proyecto']).
PROYECTO_POR_DEFECTO = os.environ.get("PROYECTO", "principal")

# Slug seguro como nombre de carpeta/archivo (mismo criterio que routes._validar_slug).
_SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Cache de engines keyed by ruta absoluta. Evita recrear engine+pool por request.
_engines: dict[str, Engine] = {}


def slug_valido(slug: str | None) -> bool:
    return bool(slug) and bool(_SLUG_VALIDO.match(slug))


def ruta_bd_proyecto(slug: str) -> Path:
    if not slug_valido(slug):
        raise ValueError(f"slug de proyecto invalido: {slug!r}")
    return PROYECTOS_DIR / slug / "productos.db"


def fotos_dir_proyecto(slug: str) -> Path:
    if not slug_valido(slug):
        raise ValueError(f"slug de proyecto invalido: {slug!r}")
    return PROYECTOS_DIR / slug / "fotos"


def exports_dir_proyecto(slug: str) -> Path:
    if not slug_valido(slug):
        raise ValueError(f"slug de proyecto invalido: {slug!r}")
    return PROYECTOS_DIR / slug / "exports"


def get_engine(db_path: Path) -> Engine:
    """Engine cacheado por ruta. check_same_thread=False: el mismo engine se
    comparte entre el threadpool de requests y el worker async de ingesta; cada
    hilo saca su propia conexion del pool (patron recomendado para SQLite)."""
    key = str(db_path)
    engine = _engines.get(key)
    if engine is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        _engines[key] = engine
    return engine


def get_session_factory(slug: str | None = None):
    """Factory de sesiones de la BD de negocio del proyecto `slug`.

    slug=None -> PROYECTO_POR_DEFECTO (CLI/scripts). En requests web, get_db()
    pasa siempre el slug de session['proyecto'].
    """
    slug = slug or PROYECTO_POR_DEFECTO
    return sessionmaker(bind=get_engine(ruta_bd_proyecto(slug)))


def get_sistema_session_factory():
    """Factory de sesiones de la BD de sistema (auth + registro de proyectos)."""
    return sessionmaker(bind=get_engine(SISTEMA_PATH))


def init_proyecto_db(slug: str | None = None) -> None:
    """Crea las tablas de negocio (si faltan) en la BD del proyecto `slug`."""
    slug = slug or PROYECTO_POR_DEFECTO
    Base.metadata.create_all(get_engine(ruta_bd_proyecto(slug)))


def _migrar_columnas_proyecto(engine) -> None:
    """Migracion ligera (no hay Alembic): agrega columnas nuevas a 'proyectos'.

    create_all NO altera tablas ya existentes, asi que las columnas agregadas
    despues del primer arranque hay que anadirlas a mano. SQLite aplica el
    DEFAULT a las filas existentes al hacer ADD COLUMN.
    """
    nuevas = {
        "vendor_hd": "VARCHAR(200) DEFAULT 'Totikay Pets SA de CV'",
        "vendor_num_hd": "VARCHAR(100) DEFAULT 'TBD'",
    }
    with engine.begin() as conn:
        existentes = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(proyectos)")
        }
        for col, ddl in nuevas.items():
            if col not in existentes:
                conn.exec_driver_sql(f"ALTER TABLE proyectos ADD COLUMN {col} {ddl}")


def init_sistema_db() -> None:
    """Crea las tablas de sistema (usuarios_autorizados, proyectos) si faltan."""
    SISTEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(SISTEMA_PATH)
    SistemaBase.metadata.create_all(engine)
    _migrar_columnas_proyecto(engine)


# Compat: mantiene funcionando a los scripts/tests que hacen
# `from app.db import init_db` o `from app.db import DB_PATH`. init_db() crea el
# esquema de negocio del proyecto por defecto.
def init_db(slug: str | None = None) -> None:
    init_proyecto_db(slug)


DB_PATH = ruta_bd_proyecto(PROYECTO_POR_DEFECTO)


def asegurar_template() -> None:
    """Garantiza que exista data/_template.db (plantilla limpia).

    Si no existe, la construye: esquema de negocio + seed de patrones de
    descarte y aranceles desde config/*.yml, SIN productos/proveedores y SIN
    categorias (los proyectos nuevos arrancan con catalogo vacio; la IA lo
    propone desde las ingestas). Idempotente: si ya existe (p. ej. derivada por
    scripts/migrar_a_multiproyecto.py) no hace nada.
    """
    if TEMPLATE_PATH.exists():
        return
    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(TEMPLATE_PATH)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        from scripts.seed_categorias import seed as seed_categorias
        from scripts.seed_aranceles import seed as seed_aranceles
        seed_categorias(session_factory=factory, solo_patrones=True)
        seed_aranceles(session_factory=factory)
    except Exception as e:  # no tumbar el arranque si el YAML falla
        log.warning("asegurar_template: seed de la plantilla fallo: %s", e)


def crear_proyecto(slug: str) -> Path:
    """Crea la BD de un proyecto nuevo clonando la plantilla y sus carpetas.

    Devuelve la ruta de la BD creada. No registra el proyecto en sistema.db
    (eso lo hace el router de proyectos). Lanza si el slug es invalido o la BD
    ya existe.
    """
    if not slug_valido(slug):
        raise ValueError(f"slug de proyecto invalido: {slug!r}")
    destino = ruta_bd_proyecto(slug)
    if destino.exists():
        raise FileExistsError(f"El proyecto '{slug}' ya tiene BD en {destino}")
    asegurar_template()
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE_PATH, destino)
    fotos_dir_proyecto(slug).mkdir(parents=True, exist_ok=True)
    exports_dir_proyecto(slug).mkdir(parents=True, exist_ok=True)
    return destino
